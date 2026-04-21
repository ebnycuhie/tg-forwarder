#!/usr/bin/env python3
"""
Telegram Auto Media Forwarder — Railway Edition
- Session persisted via SESSION_STRING env var (survives redeploys)
- Phase 1: scrapes all past media from all joined groups
- Phase 2: live monitoring for new media
- No caption, no sender name — pure media only
- Skips GIFs and stickers
"""

import os
import sys
import asyncio
import logging
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


def load_config() -> dict:
    required = ["API_ID", "API_HASH", "DESTINATION_CHAT", "SESSION_STRING"]
    missing  = [k for k in required if not os.environ.get(k)]
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

    monitor_all     = os.environ.get("MONITOR_ALL_GROUPS", "true").strip().lower() == "true"
    specific_raw    = os.environ.get("SPECIFIC_GROUPS", "")
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


_done_groups: set = set()


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


async def send_media(client: TelegramClient, message, destination) -> None:
    await client.send_file(
        destination,
        message.media,
        caption="",
    )


async def scrape_history(client: TelegramClient, cfg: dict) -> None:
    logger.info("=" * 60)
    logger.info("PHASE 1 — Scraping historical media from groups")
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
        group_id   = str(dialog.id)
        group_name = dialog.name or group_id

        if group_id in _done_groups:
            logger.info(f"[{idx}/{len(dialogs)}] Skipping (done): {group_name}")
            continue

        logger.info(f"[{idx}/{len(dialogs)}] Scraping: {group_name}")
        forwarded = 0
        skipped   = 0

        try:
            limit = cfg["history_limit"] if cfg["history_limit"] > 0 else None
            async for message in client.iter_messages(dialog.entity, limit=limit):
                media_type = get_media_type(message)
                if not media_type or media_type not in cfg["media_types"]:
                    skipped += 1
                    continue
                try:
                    await send_media(client, message, cfg["destination_chat"])
                    forwarded += 1
                    await asyncio.sleep(0.6)
                except FloodWaitError as e:
                    logger.warning(f"FloodWait {e.seconds}s — waiting...")
                    await asyncio.sleep(e.seconds + 3)
                except Exception as e:
                    logger.error(f"Failed msg {message.id} in '{group_name}': {e}")

        except (ChatAdminRequiredError, ChannelPrivateError, UserBannedInChannelError) as e:
            logger.warning(f"No access to '{group_name}': {e}")
        except Exception as e:
            logger.error(f"Error scraping '{group_name}': {e}")
        else:
            _done_groups.add(group_id)
            logger.info(f"  ✓ {group_name}: {forwarded} forwarded, {skipped} skipped")

    logger.info("PHASE 1 complete.")


async def live_monitor(client: TelegramClient, cfg: dict) -> None:
    logger.info("=" * 60)
    logger.info("PHASE 2 — Live monitoring for new media")
    logger.info("=" * 60)

    monitored_ids: set = set()

    if not cfg["monitor_all_groups"]:
        for group_id in cfg["specific_groups"]:
            try:
                entity = await client.get_entity(group_id)
                monitored_ids.add(entity.id)
                name = entity.title if hasattr(entity, "title") else str(group_id)
                logger.info(f"Monitoring: {name}")
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

            await send_media(client, event.message, cfg["destination_chat"])

            source_chat  = await event.get_chat()
            source_title = source_chat.title if hasattr(source_chat, "title") else "Unknown"
            logger.info(f"✓ [{media_type}] from '{source_title}'")

        except FloodWaitError as e:
            logger.warning(f"FloodWait {e.seconds}s — waiting...")
            await asyncio.sleep(e.seconds + 3)
        except Exception as e:
            logger.error(f"Error forwarding: {e}")

    logger.info("Live forwarder active.")
    logger.info("-" * 60)
    await client.run_until_disconnected()


async def main() -> None:
    logger.info("Starting Telegram Auto Media Forwarder (Railway Edition)...")
    cfg = load_config()
    logger.info("Configuration loaded from environment.")

    client = TelegramClient(
        StringSession(cfg["session_string"]),
        cfg["api_id"],
        cfg["api_hash"],
    )

    try:
        await client.start()
        logger.info("Connected to Telegram.")

        me = await client.get_me()
        logger.info(f"Logged in as: {me.first_name} (@{me.username})")

        try:
            dest      = await client.get_entity(cfg["destination_chat"])
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
