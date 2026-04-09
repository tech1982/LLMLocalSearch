"""
Telegram message ingestion via Telethon.
Supports channels, groups, supergroups with forum topics.
"""
import os
import sys
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import (
    Channel, Chat, MessageService,
    MessageReplyHeader
)
from telethon.tl.functions.channels import GetForumTopicsRequest

load_dotenv()

API_ID = int(os.environ.get("TG_API_ID", 0))
API_HASH = os.environ.get("TG_API_HASH", "")
PHONE = os.environ.get("TG_PHONE", "")
CHANNELS = [c.strip() for c in os.environ.get("TG_CHANNELS", "").split(",") if c.strip()]
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES_PER_CHANNEL", 10000))
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SESSION_DIR = os.environ.get("SESSION_DIR", os.path.join(_PROJECT_ROOT, "sessions"))


async def get_forum_topics(client, channel_entity) -> dict:
    """Fetch forum topic names for a supergroup with topics enabled."""
    topics = {}
    try:
        result = await client(GetForumTopicsRequest(
            channel=channel_entity,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q=""
        ))
        for topic in result.topics:
            topics[topic.id] = topic.title
    except Exception as e:
        print(f"  ℹ️ Could not fetch forum topics: {e}")
    return topics


async def ingest_channel(client, channel_name: str) -> tuple[list, list, list]:
    """Pull messages from a Telegram channel/group."""
    texts, metadatas, ids = [], [], []

    try:
        entity = await client.get_entity(channel_name)
    except Exception as e:
        print(f"  ❌ Cannot find '{channel_name}': {e}")
        return texts, metadatas, ids

    channel_title = getattr(entity, "title", channel_name)
    print(f"  📡 Channel: {channel_title} (id: {entity.id})")

    # Check if it's a forum/topics group
    is_forum = getattr(entity, "forum", False)
    topics = {}
    if is_forum:
        topics = await get_forum_topics(client, entity)
        print(f"  📂 Forum with {len(topics)} topics: {list(topics.values())[:5]}...")

    count = 0
    async for message in client.iter_messages(entity, limit=MAX_MESSAGES):
        # Skip service messages (joins, pins, etc.)
        if isinstance(message, MessageService):
            continue
        if not message.text or len(message.text.strip()) < 10:
            continue

        text = message.text.strip()
        msg_date = message.date.strftime("%Y-%m-%d %H:%M") if message.date else ""

        # Determine topic for forum groups
        topic_name = ""
        if is_forum and message.reply_to:
            topic_id = getattr(message.reply_to, "reply_to_top_id", None) or \
                       getattr(message.reply_to, "reply_to_msg_id", None)
            if topic_id and topic_id in topics:
                topic_name = topics[topic_id]

        # Build message URL
        if hasattr(entity, "username") and entity.username:
            url = f"https://t.me/{entity.username}/{message.id}"
        else:
            url = f"https://t.me/c/{entity.id}/{message.id}"

        sender_name = ""
        if message.sender:
            sender = message.sender
            if hasattr(sender, "first_name"):
                sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
            elif hasattr(sender, "title"):
                sender_name = sender.title

        doc_id = f"tg_{entity.id}_{message.id}"

        texts.append(text)
        metadatas.append({
            "source": "telegram",
            "channel": channel_title,
            "topic": topic_name,
            "author": sender_name,
            "date": msg_date,
            "url": url,
            "message_id": str(message.id),
        })
        ids.append(doc_id)

        count += 1
        if count % 1000 == 0:
            print(f"    Collected {count} messages...")

    print(f"  ✅ Collected {count} messages from {channel_title}")
    return texts, metadatas, ids


async def main():
    if not API_ID or not API_HASH:
        print("❌ TG_API_ID and TG_API_HASH must be set in .env")
        sys.exit(1)

    if not CHANNELS:
        print("❌ TG_CHANNELS must be set in .env")
        sys.exit(1)

    os.makedirs(SESSION_DIR, exist_ok=True)
    session_path = os.path.join(SESSION_DIR, "telegram_session")

    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start(phone=PHONE)
    print(f"✅ Connected to Telegram as {(await client.get_me()).first_name}")

    # Import search engine
    sys.path.insert(0, "/app/src")
    from search_engine import add_documents

    for channel in CHANNELS:
        print(f"\n🔄 Processing: {channel}")
        texts, metadatas, ids = await ingest_channel(client, channel)
        if texts:
            added = add_documents(texts, metadatas, ids)
            print(f"  📊 Indexed {added} new messages")

    await client.disconnect()
    print("\n🎉 Telegram ingestion complete!")


if __name__ == "__main__":
    asyncio.run(main())
