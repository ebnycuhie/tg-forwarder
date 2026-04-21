#!/usr/bin/env python3
"""
Telegram Auto Media Forwarder — Anti-Spam Edition
- Session persisted via SESSION_STRING env var (survives redeploys)
- Phase 1: scrapes all past media from all joined groups
- Phase 2: live monitoring for new media
- No caption, no sender name — pure media only
- Skips GIFs and stickers
- Anti-spam: intelligent rate limiting, adaptive backoff, daily caps,
  per-group pacing, jitter, and live-event debouncing to prevent
  account/group bans from Telegram's spam detection systems.
"""

import os
import sys
import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeAnimated,
    DocumentAttributeSticker,
)
from telethon.errors import (
    FloodWaitError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    UserBannedInChannelError,
    SlowModeWaitError,
    PeerFloodError,
    ChatWriteForbiddenError,
)

logging.basicConfig(
    format="[%(levelname)s %(asctime)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Anti-spam constants — tuned to stay well below Telegram's internal limits.
# Telegram's documented limits: ~20 messages/min to groups, ~30/sec overall.
# We intentionally stay far below these to avoid heuristic detection.
# ---------------------------------------------------------------------------

# History scrape: seconds between consecutive sends (base)
SCRAPE_BASE_DELAY_S: float = 4.0
# Additional random jitter range added to base delay
SCRAPE_JITTER_S: float = 3.0
# After sending this many messages within one scrape session, take a long rest
SCRAPE_BURST_LIMIT: int = 10
# Long rest duration after a burst (seconds)
SCRAPE_BURST_REST_S: float = 60.0

# Live monitor: min seconds between sends to the destination group
LIVE_MIN_INTERVAL_S: float = 5.0
# Max number of live-forwarded messages allowed per hour
LIVE_HOURLY_CAP: int = 60
# Max messages forwarded per day across all sources (0 = unlimited)
DAILY_CAP: int = int(os.environ.get("DAILY_CAP", "500"))
# Pause duration (seconds) when hourly cap is approached
LIVE_CAP_PAUSE_S: float = 120.0

# When a FloodWaitError occurs, multiply the required wait by this factor
FLOOD_WAIT_MULTIPLIER: float = 1.5
# Maximum additional seconds to sleep after any single send
MAX_EXTRA_BACKOFF_S: float = 10.0

# Group-to-group scraping rest: pause between groups (seconds)
INTER_GROUP_PAUSE_S: float = 15.0


# ---------------------------------------------------------------------------
# Rate-limiting state (shared across coroutines)
# ---------------------------------------------------------------------------

@dataclass
class RateLimiter:
    """Tracks send history and enforces anti-spam constraints."""

    # Timestamps of recent sends (rolling 60-second window for hourly rate)
    _hourly_window: deque = field(default_factory=lambda: deque(maxlen=10_000))
    # Total messages sent today
    _daily_count: int = 0
    # Epoch timestamp of the start of the current day
    _day_start: float = field(default_factory=time.time)
    # Timestamp of the last send
    _last_send_ts: float = 0.0
    # Consecutive flood errors
    _consecutive_floods: int = 0

    def _reset_day_if_needed(self) -> None:
        now = time.time()
        if now - self._day_start >= 86_400:
            self._daily_count = 0
            self._day_start = now
            logger.info("Daily counter reset.")

    def record_send(self) -> None:
        now = time.time()
        self._hourly_window.append(now)
        self._daily_count += 1
        self._last_send_ts = now
        self._consecutive_floods = 0
        self._reset_day_if_needed()

    def daily_limit_reached(self) -> bool:
        self._reset_day_if_needed()
        return DAILY_CAP > 0 and self._daily_count >= DAILY_CAP

    def hourly_count(self) -> int:
        now = time.time()
        cutoff = now - 3_600
        while self._hourly_window and self._hourly_window[0] < cutoff:
            self._hourly_window.popleft()
        return len(self._hourly_window)

    def hourly_limit_reached(self) -> bool:
        return self.hourly_count() >= LIVE_HOURLY_CAP

    def seconds_since_last_send(self) -> float:
        return time.time() - self._last_send_ts

    def record_flood(self) -> None:
        self._consecutive_floods += 1

    @property
    def consecutive_floods(self) -> int:
        return self._consecutive_floods


_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    required = ["API_ID", "API_HASH", "DESTINATION_CHAT", "SESSION_STRING"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    raw_dest = os.environ["DESTINATION_CHAT"].strip()
    try:
        destination = int(raw_dest)
    except ValueError:
        destination = raw_dest

    media_types = [
        m.strip()
        for m in os.environ.get("MEDIA_TYPES", "photo,video,document,audio,voice").split(",")
        if m.strip()
    ]

    try:
        history_limit = int(os.environ.get("HISTORY_LIMIT", "0"))
    except ValueError:
        history_limit = 0

    monitor_all = os.environ.get("MONITOR_ALL_GROUPS", "true").strip().lower() == "true"
    specific_raw = os.environ.get("SPECIFIC_GROUPS", "")
    specific_groups = [g.strip() for g in specific_raw.split(",") if g.strip()]

    return {
        "api_id":             int(os.environ["API_ID"].strip()),
        "api_hash":           os.environ["API_HASH"].strip(),
        "session_string":     os.environ["SESSION_STRING"].strip(),
        "destination_chat":   destination,
        "media_types":        media_types,
        "history_limit":      history_limit,
        "monitor_all_groups": monitor_all,
        "specific_groups":    specific_groups,
    }


# ---------------------------------------------------------------------------
# Media type detection
# ---------------------------------------------------------------------------

def is_gif(message) -> bool:
    if not isinstance(message.media, MessageMediaDocument):
        return False
    doc = message.media.document
    if not doc:
        return False
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeAnimated):
            return True
    return doc.mime_type == "image/gif"


def is_sticker(message) -> bool:
    if not isinstance(message.media, MessageMediaDocument):
        return False
    doc = message.media.document
    if not doc:
        return False
    return any(isinstance(a, DocumentAttributeSticker) for a in doc.attributes)


def get_media_type(message) -> Optional[str]:
    if not message.media:
        return None
    if is_gif(message) or is_sticker(message):
        return None
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    if isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        if not doc:
            return None
        mime = doc.mime_type or ""
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "voice" if message.voice else "audio"
        return "document"
    return None


# ---------------------------------------------------------------------------
# Core send with full anti-spam protection
# ---------------------------------------------------------------------------

async def _compute_scrape_delay(burst_count: int) -> float:
    """
    Return the delay to sleep before the next scrape send.
    Triggers a long rest after every SCRAPE_BURST_LIMIT sends.
    """
    if burst_count > 0 and burst_count % SCRAPE_BURST_LIMIT == 0:
        rest = SCRAPE_BURST_REST_S + random.uniform(0, 15.0)
        logger.info(
            f"  ⏸  Burst limit reached ({SCRAPE_BURST_LIMIT} msgs). "
            f"Resting {rest:.1f}s to avoid spam detection..."
        )
        return rest
    return SCRAPE_BASE_DELAY_S + random.uniform(0, SCRAPE_JITTER_S)


async def send_media(
    client: TelegramClient,
    message,
    destination,
    *,
    is_scrape: bool = False,
    burst_count: int = 0,
) -> bool:
    """
    Send one media file to the destination with anti-spam safeguards.
    Returns True on success, False on permanent/skip errors.
    Raises on fatal errors so the caller can handle them.
    """
    global _rate_limiter

    # --- Daily cap guard ---
    if _rate_limiter.daily_limit_reached():
        logger.warning(
            f"Daily cap of {DAILY_CAP} messages reached. "
            "Pausing until next day reset."
        )
        # Sleep until midnight-ish then let the reset happen naturally
        await asyncio.sleep(3_600)
        return False

    # --- Hourly cap guard (live mode only) ---
    if not is_scrape and _rate_limiter.hourly_limit_reached():
        logger.warning(
            f"Hourly cap of {LIVE_HOURLY_CAP} messages reached. "
            f"Pausing {LIVE_CAP_PAUSE_S}s..."
        )
        await asyncio.sleep(LIVE_CAP_PAUSE_S)

    # --- Minimum interval guard (live mode) ---
    if not is_scrape:
        elapsed = _rate_limiter.seconds_since_last_send()
        if elapsed < LIVE_MIN_INTERVAL_S:
            wait = LIVE_MIN_INTERVAL_S - elapsed + random.uniform(0, 2.0)
            await asyncio.sleep(wait)

    # --- Pre-send scrape delay ---
    if is_scrape:
        delay = await _compute_scrape_delay(burst_count)
        await asyncio.sleep(delay)

    # --- Attempt send with retry on FloodWait ---
    max_retries = 5
    for attempt in range(max_retries):
        try:
            await client.send_file(
                destination,
                message.media,
                caption="",
            )
            _rate_limiter.record_send()
            return True

        except FloodWaitError as e:
            _rate_limiter.record_flood()
            wait_s = int(e.seconds * FLOOD_WAIT_MULTIPLIER) + random.randint(5, 30)
            logger.warning(
                f"FloodWaitError: Telegram says wait {e.seconds}s. "
                f"Sleeping {wait_s}s (attempt {attempt + 1}/{max_retries})..."
            )
            await asyncio.sleep(wait_s)

            # After multiple floods, add an exponential backoff
            if _rate_limiter.consecutive_floods >= 3:
                extra = min(300, 30 * (2 ** (_rate_limiter.consecutive_floods - 2)))
                logger.warning(
                    f"Repeated floods ({_rate_limiter.consecutive_floods}x). "
                    f"Extra cooldown: {extra}s"
                )
                await asyncio.sleep(extra)

        except SlowModeWaitError as e:
            logger.warning(f"SlowMode on destination: waiting {e.seconds}s...")
            await asyncio.sleep(e.seconds + random.uniform(2, 8))

        except PeerFloodError:
            logger.error(
                "PeerFloodError: Telegram flagged this account for sending "
                "too many messages. Cooling down for 30 minutes."
            )
            await asyncio.sleep(1_800)
            return False

        except ChatWriteForbiddenError:
            logger.error("Cannot write to destination chat. Check permissions.")
            return False

        except (ChatAdminRequiredError, ChannelPrivateError, UserBannedInChannelError) as e:
            logger.warning(f"Access error (skip): {e}")
            return False

        except Exception as e:
            logger.error(f"Unexpected error sending media (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                backoff = random.uniform(5, MAX_EXTRA_BACKOFF_S * (attempt + 1))
                await asyncio.sleep(backoff)
            else:
                raise

    return False


# ---------------------------------------------------------------------------
# Phase 1: Historical scrape
# ---------------------------------------------------------------------------

_done_groups: set = set()


async def scrape_history(client: TelegramClient, cfg: dict) -> None:
    logger.info("=" * 60)
    logger.info("PHASE 1 — Scraping historical media from groups")
    logger.info(
        f"Anti-spam settings: base_delay={SCRAPE_BASE_DELAY_S}s, "
        f"burst_limit={SCRAPE_BURST_LIMIT}, burst_rest={SCRAPE_BURST_REST_S}s, "
        f"inter_group_pause={INTER_GROUP_PAUSE_S}s, daily_cap={DAILY_CAP}"
    )
    logger.info("=" * 60)

    dialogs = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            dialogs.append(dialog)

    if not cfg["monitor_all_groups"]:
        allowed = set(cfg["specific_groups"])
        dialogs = [d for d in dialogs if str(d.id) in allowed or d.name in allowed]

    logger.info(f"Found {len(dialogs)} group(s) to scrape.")

    for idx, dialog in enumerate(dialogs, 1):
        group_id = str(dialog.id)
        group_name = dialog.name or group_id

        if group_id in _done_groups:
            logger.info(f"[{idx}/{len(dialogs)}] Skipping (already done): {group_name}")
            continue

        if _rate_limiter.daily_limit_reached():
            logger.warning("Daily cap reached mid-scrape. Halting Phase 1.")
            break

        logger.info(f"[{idx}/{len(dialogs)}] Scraping: {group_name}")
        forwarded = 0
        skipped = 0
        burst_count = 0

        try:
            limit = cfg["history_limit"] if cfg["history_limit"] > 0 else None
            async for message in client.iter_messages(dialog.entity, limit=limit):
                if _rate_limiter.daily_limit_reached():
                    logger.warning(
                        f"Daily cap reached while scraping '{group_name}'. Stopping."
                    )
                    break

                media_type = get_media_type(message)
                if not media_type or media_type not in cfg["media_types"]:
                    skipped += 1
                    continue

                try:
                    success = await send_media(
                        client,
                        message,
                        cfg["destination_chat"],
                        is_scrape=True,
                        burst_count=burst_count,
                    )
                    if success:
                        forwarded += 1
                        burst_count += 1
                    else:
                        skipped += 1
                except Exception as e:
                    logger.error(f"Unrecoverable error on msg {message.id} in '{group_name}': {e}")
                    skipped += 1

        except (ChatAdminRequiredError, ChannelPrivateError, UserBannedInChannelError) as e:
            logger.warning(f"No access to '{group_name}': {e}")
        except Exception as e:
            logger.error(f"Error scraping '{group_name}': {e}")
        else:
            _done_groups.add(group_id)

        logger.info(
            f"  ✓ {group_name}: {forwarded} forwarded, {skipped} skipped | "
            f"daily total: {_rate_limiter._daily_count}"
        )

        # Rest between groups to avoid hammering the destination
        if idx < len(dialogs):
            rest = INTER_GROUP_PAUSE_S + random.uniform(0, 10.0)
            logger.info(f"  ⏸  Inter-group pause: {rest:.1f}s...")
            await asyncio.sleep(rest)

    logger.info("PHASE 1 complete.")


# ---------------------------------------------------------------------------
# Phase 2: Live monitor with deduplication and rate limiting
# ---------------------------------------------------------------------------

async def live_monitor(client: TelegramClient, cfg: dict) -> None:
    logger.info("=" * 60)
    logger.info("PHASE 2 — Live monitoring for new media")
    logger.info(
        f"Anti-spam settings: min_interval={LIVE_MIN_INTERVAL_S}s, "
        f"hourly_cap={LIVE_HOURLY_CAP}, daily_cap={DAILY_CAP}"
    )
    logger.info("=" * 60)

    monitored_ids: set = set()

    if not cfg["monitor_all_groups"]:
        for group_id in cfg["specific_groups"]:
            try:
                entity = await client.get_entity(group_id)
                monitored_ids.add(entity.id)
                name = entity.title if hasattr(entity, "title") else str(group_id)
                logger.info(f"  Monitoring: {name}")
            except Exception as e:
                logger.warning(f"Cannot access group '{group_id}': {e}")

    # Deduplicate: track message IDs we have already forwarded
    # (keyed by (chat_id, message_id) to handle restarts gracefully)
    _forwarded_ids: set = set()
    # Limit memory: keep only the last 10 000 forwarded IDs
    _forwarded_ring: deque = deque(maxlen=10_000)

    def already_forwarded(chat_id: int, msg_id: int) -> bool:
        return (chat_id, msg_id) in _forwarded_ids

    def mark_forwarded(chat_id: int, msg_id: int) -> None:
        key = (chat_id, msg_id)
        if key in _forwarded_ids:
            return
        if len(_forwarded_ring) == _forwarded_ring.maxlen:
            evicted = _forwarded_ring[0]
            _forwarded_ids.discard(evicted)
        _forwarded_ring.append(key)
        _forwarded_ids.add(key)

    # Serialise all live sends through a single asyncio lock so that
    # concurrent events cannot pile up and burst the destination.
    _send_lock = asyncio.Lock()

    @client.on(events.NewMessage)
    async def handler(event):
        try:
            if not event.is_group and not event.is_channel:
                return
            if not cfg["monitor_all_groups"] and event.chat_id not in monitored_ids:
                return

            media_type = get_media_type(event.message)
            if not media_type or media_type not in cfg["media_types"]:
                return

            if already_forwarded(event.chat_id, event.message.id):
                return

            async with _send_lock:
                # Re-check inside the lock (another coroutine may have
                # processed this by the time we acquire the lock)
                if already_forwarded(event.chat_id, event.message.id):
                    return

                if _rate_limiter.daily_limit_reached():
                    logger.warning(
                        "Daily cap reached in live mode. Skipping message."
                    )
                    return

                success = await send_media(
                    client,
                    event.message,
                    cfg["destination_chat"],
                    is_scrape=False,
                )
                if success:
                    mark_forwarded(event.chat_id, event.message.id)
                    source_chat = await event.get_chat()
                    source_title = (
                        source_chat.title if hasattr(source_chat, "title") else "Unknown"
                    )
                    logger.info(
                        f"✓ [{media_type}] from '{source_title}' | "
                        f"hourly: {_rate_limiter.hourly_count()}/{LIVE_HOURLY_CAP} | "
                        f"daily: {_rate_limiter._daily_count}/{DAILY_CAP}"
                    )

        except FloodWaitError as e:
            logger.warning(f"FloodWait in live handler: {e.seconds}s — backing off...")
            await asyncio.sleep(int(e.seconds * FLOOD_WAIT_MULTIPLIER) + 10)
        except Exception as e:
            logger.error(f"Error in live handler: {e}")

    logger.info("Live forwarder active.")
    logger.info("-" * 60)
    await client.run_until_disconnected()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("Starting Telegram Auto Media Forwarder (Anti-Spam Edition)...")
    cfg = load_config()
    logger.info("Configuration loaded from environment.")
    logger.info(
        f"Daily cap: {DAILY_CAP} | "
        f"Hourly cap (live): {LIVE_HOURLY_CAP} | "
        f"Scrape delay: {SCRAPE_BASE_DELAY_S}–{SCRAPE_BASE_DELAY_S + SCRAPE_JITTER_S}s"
    )

    client = TelegramClient(
        StringSession(cfg["session_string"]),
        cfg["api_id"],
        cfg["api_hash"],
        # Connection settings that reduce flood risk
        connection_retries=5,
        retry_delay=10,
        auto_reconnect=True,
        # Limit how many requests can be in-flight at once
        request_retries=3,
    )

    try:
        await client.start()
        logger.info("Connected to Telegram.")

        me = await client.get_me()
        logger.info(f"Logged in as: {me.first_name} (@{me.username})")

        try:
            dest = await client.get_entity(cfg["destination_chat"])
            dest_name = dest.title if hasattr(dest, "title") else str(cfg["destination_chat"])
            logger.info(f"Destination verified: {dest_name}")
        except Exception as e:
            logger.error(f"Cannot access destination chat: {e}")
            return

        await scrape_history(client, cfg)
        await live_monitor(client, cfg)

    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        if client.is_connected():
            await client.disconnect()
        logger.info("Forwarder stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
