#!/usr/bin/env python3
"""
Telegram Auto Media Forwarder — Railway Edition (Anti-Ban)

Safety features added:
- Randomized delays (human-like)
- Daily + hourly message caps
- Long pauses between groups
- Batch cooldowns
- Strict FloodWait handling with abort threshold
- Persistent state tracking
"""

import os
import sys
import json
import random
import asyncio
import logging
from datetime import datetime, timedelta
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
)

logging.basicConfig(
    format="[%(levelname)s %(asctime)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# SAFETY / RATE-LIMIT CONFIG  (tune via env vars if needed)
# ──────────────────────────────────────────────────────────────
MIN_DELAY            = float(os.environ.get("MIN_DELAY",            "3.0"))   # min seconds between sends
MAX_DELAY            = float(os.environ.get("MAX_DELAY",            "6.0"))   # max seconds between sends
GROUP_PAUSE_MIN      = int(os.environ.get("GROUP_PAUSE_MIN",        "30"))    # pause between groups
GROUP_PAUSE_MAX      = int(os.environ.get("GROUP_PAUSE_MAX",        "60"))
HOURLY_LIMIT         = int(os.environ.get("HOURLY_LIMIT",           "80"))    # max msgs / hour
DAILY_LIMIT          = int(os.environ.get("DAILY_LIMIT",            "500"))   # max msgs / day
BATCH_SIZE           = int(os.environ.get("BATCH_SIZE",             "50"))    # cooldown after N sends
BATCH_COOLDOWN_MIN   = int(os.environ.get("BATCH_COOLDOWN_MIN",     "120"))   # 2 min
BATCH_COOLDOWN_MAX   = int(os.environ.get("BATCH_COOLDOWN_MAX",     "300"))   # 5 min
FLOODWAIT_ABORT_SEC  = int(os.environ.get("FLOODWAIT_ABORT_SEC",    "300"))   # > 5 min FW => long pause
FLOODWAIT_ABORT_PAUSE= int(os.environ.get("FLOODWAIT_ABORT_PAUSE",  "3600"))  # 1 hour pause
STATE_FILE           = os.environ.get("STATE_FILE", "forwarder_state.json")
# ──────────────────────────────────────────────────────────────


def load_config() -> dict:
    required = ["API_ID", "API_HASH", "DESTINATION_CHAT", "SESSION_STRING"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    session_string = os.environ["SESSION_STRING"].strip()
    if len(session_string) < 50:
        logger.error("SESSION_STRING looks invalid (too short). Regenerate it locally.")
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
        "api_id": int(os.environ["API_ID"].strip()),
        "api_hash": os.environ["API_HASH"].strip(),
        "session_string": session_string,
        "destination_chat": destination,
        "media_types": media_types,
        "history_limit": history_limit,
        "monitor_all_groups": monitor_all,
        "specific_groups": specific_groups,
    }


# ──────────────────────────────────────────────────────────────
# RATE LIMITER
# ──────────────────────────────────────────────────────────────
class RateLimiter:
    """Tracks hourly & daily send counts; sleeps if limits hit."""

    def __init__(self):
        self.hourly_count = 0
        self.daily_count  = 0
        self.batch_count  = 0
        self.hour_start   = datetime.utcnow()
        self.day_start    = datetime.utcnow()
        self.total_sent   = 0
        self._load_state()

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    s = json.load(f)
                day_start = datetime.fromisoformat(s.get("day_start"))
                # Only restore daily counter if still within the same UTC day
                if datetime.utcnow() - day_start < timedelta(days=1):
                    self.daily_count = s.get("daily_count", 0)
                    self.day_start   = day_start
                    self.total_sent  = s.get("total_sent", 0)
                    logger.info(f"State restored: {self.daily_count} sent today.")
        except Exception as e:
            logger.warning(f"Could not load state: {e}")

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "daily_count": self.daily_count,
                    "day_start":   self.day_start.isoformat(),
                    "total_sent":  self.total_sent,
                }, f)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

    async def wait_if_needed(self):
        now = datetime.utcnow()

        # Reset hour
        if now - self.hour_start >= timedelta(hours=1):
            self.hourly_count = 0
            self.hour_start   = now

        # Reset day
        if now - self.day_start >= timedelta(days=1):
            self.daily_count = 0
            self.day_start   = now

        # Daily cap
        if self.daily_count >= DAILY_LIMIT:
            wake = self.day_start + timedelta(days=1)
            sleep_s = max(60, int((wake - now).total_seconds()))
            logger.warning(
                f"🛑 Daily limit ({DAILY_LIMIT}) reached. Sleeping {sleep_s//60} min "
                f"until {wake.isoformat()} UTC."
            )
            await asyncio.sleep(sleep_s)
            self.daily_count = 0
            self.day_start   = datetime.utcnow()

        # Hourly cap
        if self.hourly_count >= HOURLY_LIMIT:
            wake = self.hour_start + timedelta(hours=1)
            sleep_s = max(30, int((wake - now).total_seconds()))
            logger.warning(
                f"⏸ Hourly limit ({HOURLY_LIMIT}) reached. Sleeping {sleep_s//60} min."
            )
            await asyncio.sleep(sleep_s)
            self.hourly_count = 0
            self.hour_start   = datetime.utcnow()

        # Batch cooldown
        if self.batch_count >= BATCH_SIZE:
            cool = random.randint(BATCH_COOLDOWN_MIN, BATCH_COOLDOWN_MAX)
            logger.info(f"💤 Batch of {BATCH_SIZE} sent — cooldown {cool}s.")
            await asyncio.sleep(cool)
            self.batch_count = 0

    def record_send(self):
        self.hourly_count += 1
        self.daily_count  += 1
        self.batch_count  += 1
        self.total_sent   += 1
        self._save_state()

    async def human_delay(self):
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


limiter = RateLimiter()
_done_groups: set = set()


# ──────────────────────────────────────────────────────────────
# MEDIA HELPERS
# ──────────────────────────────────────────────────────────────
def is_gif(message) -> bool:
    if not isinstance(message.media, MessageMediaDocument):
        return False
    doc = message.media.document
    if not doc:
        return False
    if any(isinstance(a, DocumentAttributeAnimated) for a in doc.attributes):
        return True
    return doc.mime_type == "image/gif"


def is_sticker(message) -> bool:
    if not isinstance(message.media, MessageMediaDocument):
        return False
    doc = message.media.document
    if not doc:
        return False
    return any(isinstance(a, DocumentAttributeSticker) for a in doc.attributes)


def get_media_type(message) -> str | None:
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


async def ensure_connected(client: TelegramClient) -> None:
    if not client.is_connected():
        logger.warning("Disconnected — reconnecting...")
        await client.connect()
        logger.info("Reconnected.")


async def safe_send(client: TelegramClient, message, destination) -> bool:
    """Send media with full rate-limit + flood-wait protection. Returns True on success."""
    await ensure_connected(client)
    await limiter.wait_if_needed()

    try:
        await client.send_file(destination, message.media, caption="")
        limiter.record_send()
        await limiter.human_delay()
        return True

    except FloodWaitError as e:
        # Telegram is explicitly telling us to slow down
        if e.seconds >= FLOODWAIT_ABORT_SEC:
            logger.error(
                f"🚨 Large FloodWait ({e.seconds}s) — likely close to ban risk. "
                f"Pausing for {FLOODWAIT_ABORT_PAUSE}s ({FLOODWAIT_ABORT_PAUSE//60} min)."
            )
            await asyncio.sleep(FLOODWAIT_ABORT_PAUSE)
        else:
            wait = e.seconds + 10  # buffer
            logger.warning(f"⚠ FloodWait {e.seconds}s — sleeping {wait}s.")
            await asyncio.sleep(wait)
        return False

    except ConnectionError:
        logger.warning("Connection error — reconnecting...")
        await asyncio.sleep(5)
        await ensure_connected(client)
        return False

    except Exception as e:
        logger.error(f"Send failed: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# PHASE 1 — HISTORY SCRAPE
# ──────────────────────────────────────────────────────────────
async def scrape_history(client: TelegramClient, cfg: dict) -> None:
    logger.info("=" * 60)
    logger.info("PHASE 1 — Scraping historical media (rate-limited)")
    logger.info(
        f"Limits: {HOURLY_LIMIT}/hr, {DAILY_LIMIT}/day, "
        f"{MIN_DELAY}-{MAX_DELAY}s per msg"
    )
    logger.info("=" * 60)

    await ensure_connected(client)

    dialogs = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            dialogs.append(dialog)

    if not cfg["monitor_all_groups"]:
        allowed = set(cfg["specific_groups"])
        dialogs = [d for d in dialogs if str(d.id) in allowed or d.name in allowed]

    logger.info(f"Found {len(dialogs)} group(s) to scrape.")

    for idx, dialog in enumerate(dialogs, 1):
        group_id   = str(dialog.id)
        group_name = dialog.name or group_id

        if group_id in _done_groups:
            logger.info(f"[{idx}/{len(dialogs)}] Skipping (done): {group_name}")
            continue

        logger.info(f"[{idx}/{len(dialogs)}] Scraping: {group_name}")
        forwarded = skipped = failed = 0

        try:
            limit = cfg["history_limit"] if cfg["history_limit"] > 0 else None

            async for message in client.iter_messages(dialog.entity, limit=limit):
                media_type = get_media_type(message)
                if not media_type or media_type not in cfg["media_types"]:
                    skipped += 1
                    continue

                ok = await safe_send(client, message, cfg["destination_chat"])
                if ok:
                    forwarded += 1
                else:
                    failed += 1

        except (ChatAdminRequiredError, ChannelPrivateError, UserBannedInChannelError) as e:
            logger.warning(f"No access to '{group_name}': {e}")
        except Exception as e:
            logger.error(f"Error scraping '{group_name}': {e}")

        _done_groups.add(group_id)
        logger.info(
            f"  ✓ {group_name}: {forwarded} sent, {skipped} skipped, {failed} failed "
            f"| Daily total: {limiter.daily_count}/{DAILY_LIMIT}"
        )

        # Long pause between groups
        pause = random.randint(GROUP_PAUSE_MIN, GROUP_PAUSE_MAX)
        logger.info(f"  Pausing {pause}s before next group...")
        await asyncio.sleep(pause)

    logger.info("PHASE 1 complete.")


# ──────────────────────────────────────────────────────────────
# PHASE 2 — LIVE MONITOR
# ──────────────────────────────────────────────────────────────
async def live_monitor(client: TelegramClient, cfg: dict) -> None:
    logger.info("=" * 60)
    logger.info("PHASE 2 — Live monitoring (rate-limited)")
    logger.info("=" * 60)

    monitored_ids: set = set()
    if not cfg["monitor_all_groups"]:
        for group_id in cfg["specific_groups"]:
            try:
                entity = await client.get_entity(group_id)
                monitored_ids.add(entity.id)
            except Exception as e:
                logger.warning(f"Cannot access group '{group_id}': {e}")

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

            ok = await safe_send(client, event.message, cfg["destination_chat"])
            if ok:
                source = await event.get_chat()
                title = getattr(source, "title", "Unknown")
                logger.info(
                    f"✓ [{media_type}] from '{title}' "
                    f"| {limiter.daily_count}/{DAILY_LIMIT} today"
                )
        except Exception as e:
            logger.error(f"Live handler error: {e}")

    logger.info("Live forwarder active.")

    while True:
        try:
            await ensure_connected(client)
            await client.run_until_disconnected()
        except Exception as e:
            logger.error(f"Connection lost: {e} — reconnecting in 10s...")
            await asyncio.sleep(10)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
async def main() -> None:
    logger.info("Starting Telegram Auto Media Forwarder (Anti-Ban Edition)...")
    cfg = load_config()

    client = TelegramClient(
        StringSession(cfg["session_string"]),
        cfg["api_id"],
        cfg["api_hash"],
        connection_retries=10,
        retry_delay=5,
        auto_reconnect=True,
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("Session not authorized. Regenerate SESSION_STRING.")
            await client.disconnect()
            sys.exit(1)

        me = await client.get_me()
        logger.info(f"Logged in as: {me.first_name} (@{me.username})")

        try:
            dest = await client.get_entity(cfg["destination_chat"])
            dest_name = getattr(dest, "title", str(cfg["destination_chat"]))
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
        sys.exit(0)
