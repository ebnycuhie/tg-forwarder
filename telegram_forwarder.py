#!/usr/bin/env python3
"""
Telegram Auto Media Forwarder
Forwards media from groups to a personal destination group/channel
Works on Windows and Linux/Termux
"""

import json
import os
import sys
import asyncio
import logging
from pathlib import Path
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, InputPeerChannel

# Setup logging
logging.basicConfig(
    format='[%(levelname)s %(asctime)s] %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration file path
CONFIG_FILE = Path(__file__).parent / 'config.json'
SESSION_FILE = Path(__file__).parent / 'forwarder_session'


def load_config():
    """Load configuration from config.json"""
    if not CONFIG_FILE.exists():
        logger.error(f"Configuration file not found: {CONFIG_FILE}")
        logger.error("Please create config.json with your settings")
        sys.exit(1)
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Validate required fields
        required_fields = ['api_id', 'api_hash', 'destination_chat']
        for field in required_fields:
            if field not in config:
                logger.error(f"Missing required field in config.json: {field}")
                sys.exit(1)
        
        # Set defaults for optional fields
        config.setdefault('monitor_all_groups', True)
        config.setdefault('specific_groups', [])
        config.setdefault('media_types', ['photo', 'video', 'document', 'audio', 'voice'])
        config.setdefault('forward_with_caption', True)
        config.setdefault('add_source_info', True)
        
        return config
    
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing config.json: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        sys.exit(1)


def should_forward_message(message, config):
    """Determine if a message should be forwarded based on config"""
    # Check if message has media
    if not message.media:
        return False
    
    # Determine media type
    media_type = None
    if isinstance(message.media, MessageMediaPhoto):
        media_type = 'photo'
    elif isinstance(message.media, MessageMediaDocument):
        if message.media.document:
            mime = message.media.document.mime_type or ''
            if mime.startswith('video/'):
                media_type = 'video'
            elif mime.startswith('audio/'):
                if message.voice:
                    media_type = 'voice'
                else:
                    media_type = 'audio'
            else:
                media_type = 'document'
    
    # Check if media type is in allowed types
    if media_type not in config['media_types']:
        return False
    
    return True


async def main():
    """Main function to run the forwarder"""
    logger.info("Starting Telegram Auto Media Forwarder...")
    
    # Load configuration
    config = load_config()
    logger.info("Configuration loaded successfully")
    
    # Create Telegram client
    client = TelegramClient(
        str(SESSION_FILE),
        config['api_id'],
        config['api_hash']
    )
    
    try:
        # Start the client
        await client.start()
        logger.info("Connected to Telegram successfully")
        
        # Get user info
        me = await client.get_me()
        logger.info(f"Logged in as: {me.first_name} (@{me.username})")
        
        # Verify destination chat exists
        try:
            dest_entity = await client.get_entity(config['destination_chat'])
            logger.info(f"Destination chat verified: {dest_entity.title if hasattr(dest_entity, 'title') else config['destination_chat']}")
        except Exception as e:
            logger.error(f"Cannot access destination chat '{config['destination_chat']}': {e}")
            logger.error("Make sure the chat ID/username is correct and you have access to it")
            return
        
        # Get list of groups to monitor
        monitored_groups = set()
        if config['monitor_all_groups']:
            logger.info("Monitoring ALL groups (mode: monitor_all_groups=true)")
            # We'll check group membership dynamically for each message
        else:
            if not config['specific_groups']:
                logger.warning("No groups specified in config! Set monitor_all_groups=true or add groups to specific_groups")
                return
            
            for group_id in config['specific_groups']:
                try:
                    entity = await client.get_entity(group_id)
                    monitored_groups.add(entity.id)
                    logger.info(f"Monitoring group: {entity.title if hasattr(entity, 'title') else group_id}")
                except Exception as e:
                    logger.warning(f"Cannot access group '{group_id}': {e}")
        
        logger.info(f"Media types to forward: {', '.join(config['media_types'])}")
        logger.info("Forwarder is now active. Press Ctrl+C to stop.")
        logger.info("-" * 60)
        
        # Event handler for new messages
        @client.on(events.NewMessage)
        async def handler(event):
            try:
                # Check if message is from a group/channel
                if not event.is_group and not event.is_channel:
                    return
                
                # Check if we should monitor this group
                if not config['monitor_all_groups']:
                    if event.chat_id not in monitored_groups:
                        return
                
                # Check if message should be forwarded
                if not should_forward_message(event.message, config):
                    return
                
                # Get source chat info
                source_chat = await event.get_chat()
                source_title = source_chat.title if hasattr(source_chat, 'title') else 'Unknown'
                
                # Forward the message
                if config['add_source_info']:
                    # Forward with source information
                    caption = event.message.message if event.message.message else ""
                    source_info = f"\n\n📤 From: {source_title}"
                    
                    await client.send_message(
                        config['destination_chat'],
                        event.message,
                        caption=caption + source_info if config['forward_with_caption'] else source_info
                    )
                else:
                    # Forward as-is
                    await client.forward_messages(
                        config['destination_chat'],
                        event.message
                    )
                
                logger.info(f"✓ Forwarded media from '{source_title}'")
            
            except Exception as e:
                logger.error(f"Error forwarding message: {e}")
        
        # Keep the client running
        await client.run_until_disconnected()
    
    except KeyboardInterrupt:
        logger.info("\nStopping forwarder...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        if client.is_connected():
            await client.disconnect()
        logger.info("Forwarder stopped")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
