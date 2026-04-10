import asyncio
import json
import logging
import os
from pathlib import Path
from datetime import datetime

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from dotenv import load_dotenv

# Load environment variables
# Look for .env file in parent directory as well
load_dotenv()
load_dotenv('../.env')  # Load from parent directory if exists

# Global reference to spam manager if running from main bot
spam_manager_ref = None

def set_spam_manager(manager):
    """Set reference to spam manager from main bot"""
    global spam_manager_ref
    spam_manager_ref = manager


def log_event(message):
    """Log event both locally and to main bot if available"""
    import os
    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)
    
    # Local logging
    logger = logging.getLogger('TelegramBot')
    
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        # File handler
        file_handler = logging.FileHandler('logs/bot.log', encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        # Formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
    
    logger.info(message)
    
    # Also log to main bot's manager if available
    if spam_manager_ref:
        spam_manager_ref.add_log(message)


def load_config():
    """Load configuration from config.json"""
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def get_available_posts():
    """
    Dynamically detect available posts by scanning texts/ directory
    
    Returns:
        list: Sorted list of available post keys
    """
    text_dir = Path("texts/")
    available_keys = []
    
    if text_dir.exists():
        for txt_file in text_dir.glob("*.txt"):
            key = txt_file.stem  # filename without extension
            available_keys.append(key)
    
    return sorted(available_keys)


def load_post(key: str):
    """
    Load post content (text and photo) by key
    
    Args:
        key: Post identifier
    
    Returns:
        tuple: (text, photo_path)
    """
    # Load text
    text_path = Path(f"texts/{key}.txt")
    text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
    
    # Find photo with any supported extension
    photo = None
    for ext in (".jpg", ".jpeg", ".png"):
        photo_path = Path(f"photos/{key}{ext}")
        if photo_path.exists():
            photo = str(photo_path)
            break
    
    return text, photo


async def send_post(client, group, text, photo_path):
    """
    Send a post (text + photo) to a single group/channel
    
    Args:
        client: Telethon client
        group: Chat ID or username
        text: Text content to send
        photo_path: Path to photo file (optional)
    """
    if photo_path:
        # Send photo with caption
        await client.send_file(group, photo_path, caption=text)
    else:
        # Send text only
        await client.send_message(group, text)


async def send_with_retry(client, group, text, photo_path, max_retries=3):
    """
    Send post with retry mechanism for handling errors like FloodWait
    
    Args:
        client: Telethon client
        group: Chat ID or username
        text: Text content to send
        photo_path: Path to photo file (optional)
        max_retries: Number of retry attempts
    
    Returns:
        bool: True if sent successfully, False otherwise
    """
    for attempt in range(max_retries):
        try:
            await send_post(client, group, text, photo_path)
            log_event(f"Successfully sent to {group}")
            return True
        except FloodWaitError as e:
            log_event(f"FloodWait {e.seconds} seconds. Waiting...")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log_event(f"Error sending to {group}: {e}")
            if attempt < max_retries - 1:
                # Wait before retry with increasing delay
                wait_time = 5 * (attempt + 1)
                log_event(f"Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
    
    log_event(f"Failed to send to {group} after {max_retries} attempts")
    return False


async def get_channels_from_file():
    """Read channels from channels.txt file"""
    channels_file = "channels.txt"
    channels = []
    if os.path.exists(channels_file):
        with open(channels_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:  # Skip empty lines
                    # Try to convert to int if it's a numeric ID
                    try:
                        if line.lstrip('-').isdigit():
                            channels.append(int(line))
                        else:
                            channels.append(line)
                    except ValueError:
                        # If conversion fails, add as string
                        channels.append(line)
    return channels


async def main():
    """Main function that runs the bot"""
    # Load configuration
    config = load_config()
    
    # Initialize Telethon client
    client = TelegramClient(
        config['session_file'],
        int(os.getenv('API_ID')),
        os.getenv('API_HASH')
    )
    
    log_event("Starting bot...")
    
    # Start the client and authenticate
    await client.start(phone=os.getenv('PHONE'))
    
    log_event("Client authenticated successfully")
    
    try:
        while True:
            # Get available posts dynamically
            available_posts = get_available_posts()
            if not available_posts:
                log_event("No posts found in texts/ directory. Exiting...")
                break
            
            # Get channels from file instead of config
            channels = await get_channels_from_file()
            if not channels:
                log_event("No channels found in channels.txt. Using config channels.")
                channels = config['groups']
            
            log_event(f"Detected {len(available_posts)} available posts: {available_posts}")
            log_event(f"Loaded {len(channels)} channels from file")
            log_event("Starting new cycle of posts...")
            
            # Loop through each available post
            for key in available_posts:
                text, photo = load_post(key)
                
                log_event(f"Starting distribution of post '{key}'")
                
                # Send to each channel
                for channel in channels:
                    log_event(f"Sending post '{key}' to {channel}...")
                    await send_with_retry(client, channel, text, photo)
                
                log_event(f"Post '{key}' distributed to all channels. Waiting {config['post_interval_seconds']} seconds...")
                
                # Wait before sending the next post
                await asyncio.sleep(config['post_interval_seconds'])
            
            log_event(f"Cycle completed. Waiting {config['cycle_interval_seconds']} seconds before next cycle...")
            
            # Wait before starting the next cycle
            await asyncio.sleep(config['cycle_interval_seconds'])
    
    except KeyboardInterrupt:
        log_event("Received interrupt signal. Stopping bot...")
    finally:
        # Close the client properly
        await client.disconnect()
        log_event("Client disconnected")


if __name__ == "__main__":
    asyncio.run(main())
