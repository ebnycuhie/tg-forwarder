#!/usr/bin/env python3
"""
Telegram Auto Media Forwarder — Railway Edition (Folder + Anti-Ban)

Features:
- Scrapes & monitors ONLY chats inside a specific Telegram folder (FOLDER_NAME)
- Randomized human-like delays
- Hourly + daily message caps
- Batch cooldowns
- Strict FloodWait handling
- Persistent state across restarts
- Graceful exit on AuthKeyDuplicatedError
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
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeAnimated,
    DocumentAttributeSticker,
    DialogFilter,
    DialogFilterChatlist,
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
)
from telethon.errors import (
    FloodWaitError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    UserBannedInChannelError,
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
)

logging.basicConfig(
    format="[%(levelname)s %(asctime)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# SAFETY / RATE-LIMIT CONFIG
# ──────────────────────────────────────────────────────────────
MIN_DELAY             = float(os.environ.get("MIN_DELAY",             "3.0"))
MAX_DELAY             = float(os.environ.get("MAX_DELAY",             "6.0"))
GROUP_PAUSE_MIN       = int(os.environ.get("GROUP_PAUSE_MIN",         "30"))
GROUP_PAUSE_MAX       = int(os.environ.get("GROUP_PAUSE_MAX",         "60"))
HOURLY_LIMIT          = int(os.environ.get("HOURLY_LIMIT",            "80"))
DAILY_LIMIT           = int(os.environ.get("DAILY_LIMIT",             "500"))
BATCH_SIZE            = int(os.environ.get("BATCH_SIZE",              "50"))
BATCH_COOLDOWN_MIN    = int(os.environ.get("BATCH_COOLDOWN_MIN",      "120"))
BATCH_COOLDOWN_MAX    = int(os.environ.get("BATCH_COOLDOWN_MAX",      "300"))
FLOODWAIT_ABORT_SEC   = int(os.environ.get("FLOODWAIT_ABORT_SEC",     "300"))
FLOODWAIT_ABORT_PAUSE = int(os.environ.get("FLOODWAIT_ABORT_PAUSE",   "3600"))
STATE_FILE            = os.environ.get("STATE_FILE", "forwarder_state.json")
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
    folder_name = os.environ.get("FOLDER_NAME", "").strip()

    return {
        "api_id": int(os.environ["API_ID"].strip()),
        "api_hash": os.environ["API_HASH"].strip(),
        "session_string": session_string,
        "destination_chat": destination,
        "media_types": media_types,
        "history_limit": history_limit,
        "monitor_all_groups": monitor_all,
        "specific_groups": specific_groups,
        "folder_name": folder_name,
    }


# ──────────────────────────────────────────────────────────────
# RATE LIMITER
# ──────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self.hourly_count = 0
        self.daily_count = 0
        self.batch_count = 0
        self.hour_start = datetime.utcnow()
        self.day_start = datetime.utcnow()
        self.total_sent = 0
        self._load_state()

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    s = json.load(f)
                day_start = datetime.fromisoformat(s.get("day_start"))
                if datetime.utcnow() - day_start < timedelta(days=1):
                    self.daily_count = s.get("daily_count", 0)
                    self.day_start = day_start
                    self.total_sent = s.get("total_sent", 0)
                    logger.info(f"State restored: {self.daily_count} sent today.")
        except Exception as e:
            logger.warning(f"Could not load state: {e}")

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "daily_count": self.daily_count,
                    "day_start": self.day_start.isoformat(),
                    "total_sent": self.total_sent,
                }, f)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

    async def wait_if_needed(self):
        now = datetime.utcnow()

        if now - self.hour_start >= timedelta(hours=1):
            self.hourly_count = 0
            self.hour_start = now

        if now - self.day_start >= timedelta(days=1):
            self.daily_count = 0
            self.day_start = now

        if self.daily_count >= DAILY_LIMIT:
            wake = self.day_start + timedelta(days=1)
            sleep_s = max(60, int((wake - now).total_seconds()))
            logger.warning(
                f"🛑 Daily limit ({DAILY_LIMIT}) reached. Sleeping {sleep_s // 60} min."
            )
            await asyncio.sleep(sleep_s)
            self.daily_count = 0
            self.day_start = datetime.utcnow()

        if self.hourly_count >= HOURLY_LIMIT:
            wake = self.hour_start + timedelta(hours=1)
            sleep_s = max(30, int((wake - now).total_seconds()))
            logger.warning(
                f"⏸ Hourly limit ({HOURLY_LIMIT}) reached. Sleeping {sleep_s // 60} min."
            )
            await asyncio.sleep(sleep_s)
            self.hourly_count = 0
            self.hour_start = datetime.utcnow()

        if self.batch_count >= BATCH_SIZE:
            cool = random.randint(BATCH_COOLDOWN_MIN, BATCH_COOLDOWN_MAX)
            logger.info(f"💤 Batch of {BATCH_SIZE} sent — cooldown {cool}s.")
            await asyncio.sleep(cool)
            self.batch_count = 0

    def record_send(self):
        self.hourly_count += 1
        self.daily_count += 1
        self.batch_count += 1
        self.total_sent += 1
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


# ──────────────────────────────────────────────────────────────
# FOLDER LOOKUP
# ──────────────────────────────────────────────────────────────
def _filter_title(f) -> str:
    """Return folder title as plain string (handles new TextWithEntities format)."""
    t = getattr(f, "title", "")
    if hasattr(t, "text"):
        return t.text
    return t or ""


async def get_folder_dialogs(client: TelegramClient, folder_name: str) -> list:
    """Return only the dialogs that belong to a specific Telegram folder."""
    logger.info(f"Looking up folder: '{folder_name}'")

    result = await client(GetDialogFiltersRequest())
    filters = getattr(result, "filters", result)

    target = None
    available = []
    for f in filters:
        if isinstance(f, (DialogFilter, DialogFilterChatlist)):
            title = _filter_title(f)
            available.append(title)
            if title.strip().lower() == folder_name.strip().lower():
                target = f
                break

    if not target:
        logger.error(f"Folder '{folder_name}' not found. Available folders: {available}")
        return []

    folder_peer_ids = set()
    for peer in target.include_peers:
        if isinstance(peer, InputPeerChannel):
            folder_peer_ids.add(peer.channel_id)
        elif isinstance(peer, InputPeerChat):
            folder_peer_ids.add(peer.chat_id)
        elif isinstance(peer, InputPeerUser):
            folder_peer_ids.add(peer.user_id)

    logger.info(f"Folder '{folder_name}' contains {len(folder_peer_ids)} chats.")

    dialogs = []
    async for dialog in client.iter_dialogs():
        entity_id = getattr(dialog.entity, "id", None)
        if entity_id in folder_peer_ids:
            dialogs.append(dialog)

    logger.info(f"Matched {len(dialogs)} accessible dialogs from folder.")
    return dialogs


# ──────────────────────────────────────────────────────────────
# SAFE SEND
# ──────────────────────────────────────────────────────────────
async def safe_send(client: TelegramClient, message, destination) -> bool:
    await ensure_connected(client)
    await limiter.wait_if_needed()

    try:
        await client.send_file(destination, message.media, caption="")
        limiter.record_send()
        await limiter.human_delay()
        return True

    except FloodWaitError as e:
        if e.seconds >= FLOODWAIT_ABORT_SEC:
            logger.error(
                f"🚨 Large FloodWait ({e.seconds}s) — pausing {FLOODWAIT_ABORT_PAUSE}s "
                f"({FLOODWAIT_ABORT_PAUSE // 60} min)."
            )
            await asyncio.sleep(FLOODWAIT_ABORT_PAUSE)
        else:
            wait = e.seconds + 10
            logger.warning(f"⚠ FloodWait {e.seconds}s — sleeping {wait}s.")
            await asyncio.sleep(wait)
        return False

    except (AuthKeyDuplicatedError, AuthKeyUnregisteredError):
        logger.error(
            "🚨 Auth key invalid/duplicated. Session used elsewhere or revoked. Exiting."
        )
        sys.exit(1)

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

    if cfg.get("folder_name"):
        dialogs = await get_folder_dialogs(client, cfg["folder_name"])
        if not dialogs:
            logger.error("No dialogs found in folder. Aborting Phase 1.")
            return
    else:
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
        except (AuthKeyDuplicatedError, AuthKeyUnregisteredError):
            logger.error("🚨 Auth key invalid/duplicated. Exiting.")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Error scraping '{group_name}': {e}")

        _done_groups.add(group_id)
        logger.info(
            f"  ✓ {group_name}: {forwarded} sent, {skipped} skipped, {failed} failed "
            f"| Daily total: {limiter.daily_count}/{DAILY_LIMIT}"
        )

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
    folder_mode = False

    if cfg.get("folder_name"):
        folder_dialogs = await get_folder_dialogs(client, cfg["folder_name"])
        for d in folder_dialogs:
            entity_id = getattr(d.entity, "id", None)
            if entity_id is not None:
                monitored_ids.add(entity_id)
        logger.info(
            f"Live-monitoring {len(monitored_ids)} chats from folder "
            f"'{cfg['folder_name']}'."
        )
        folder_mode = True
    elif not cfg["monitor_all_groups"]:
        for group_id in cfg["specific_groups"]:
            try:
                entity = await client.get_entity(group_id)
                monitored_ids.add(entity.id)
            except Exception as e:
                logger.warning(f"Cannot access group '{group_id}': {e}")
        folder_mode = True

    @client.on(events.NewMessage)
    async def handler(event):
        try:
            if not event.is_group and not event.is_channel:
                return

            if folder_mode:
                # event.chat_id is negative for channels; compare against abs() too
                if (event.chat_id not in monitored_ids
                        and abs(event.chat_id) not in monitored_ids):
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
        except (AuthKeyDuplicatedError, AuthKeyUnregisteredError):
            logger.error("🚨 Auth key invalid/duplicated. Exiting.")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Live handler error: {e}")

    logger.info("Live forwarder active.")

    while True:
        try:
            await ensure_connected(client)
            await client.run_until_disconnected()
        except (AuthKeyDuplicatedError, AuthKeyUnregisteredError):
            logger.error("🚨 Auth key invalid/duplicated. Exiting.")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Connection lost: {e} — reconnecting in 10s...")
            await asyncio.sleep(10)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
async def main() -> None:
    logger.info("Starting Telegram Auto Media Forwarder (Folder + Anti-Ban)...")
    cfg = load_config()

    if cfg["folder_name"]:
        logger.info(f"📁 Folder mode: '{cfg['folder_name']}'")
    elif not cfg["monitor_all_groups"]:
        logger.info(f"🎯 Specific groups: {cfg['specific_groups']}")
    else:
        logger.info("🌐 Monitoring ALL groups (no folder filter)")

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

    except (AuthKeyDuplicatedError, AuthKeyUnregisteredError):
        logger.error(
            "🚨 SESSION_STRING is being used elsewhere or has been revoked. "
            "Terminate other sessions and regenerate SESSION_STRING."
        )
        sys.exit(1)
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
