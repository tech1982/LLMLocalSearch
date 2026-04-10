"""
Telegram ingestion with incremental sync via LanceDB.
First run: pulls full history (or up to MAX_MESSAGES_PER_CHANNEL).
Subsequent runs: only fetches messages with id > max(stored id) per channel.
No state file needed — LanceDB tracks the highest message_id per channel.
"""
import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageService, PeerChannel
from telethon.tl.functions.channels import GetForumTopicsRequest

load_dotenv()

API_ID = int(os.environ.get("TG_API_ID", 0))
API_HASH = os.environ.get("TG_API_HASH", "")
PHONE = os.environ.get("TG_PHONE", "")
def _load_channels() -> list[str]:
    """Load channels from channels.txt (preferred) or TG_CHANNELS env var (fallback).

    channels.txt format: one entry per line, # comments allowed, inline # comments stripped.
    """
    channels_file = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "channels.txt")
    if os.path.exists(channels_file):
        channels = []
        with open(channels_file, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()  # strip inline comments
                if line:
                    channels.append(line)
        return channels
    # Fallback: comma-separated env var
    return [c.strip() for c in os.environ.get("TG_CHANNELS", "").split(",") if c.strip()]

CHANNELS = _load_channels()
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES_PER_CHANNEL", 0)) or None  # 0 = no limit
# How many days back to fetch on the first run (0 = unlimited)
_DAYS_BACK = int(os.environ.get("MAX_DAYS_BACK", 365))

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SESSION_DIR = os.environ.get("SESSION_DIR", os.path.join(_PROJECT_ROOT, "sessions"))


async def get_forum_topics(client, channel_entity) -> dict:
    topics = {}
    try:
        result = await client(GetForumTopicsRequest(
            channel=channel_entity,
            offset_date=0, offset_id=0, offset_topic=0,
            limit=100, q="",
        ))
        for topic in result.topics:
            topics[topic.id] = topic.title
    except Exception as e:
        print(f"  ℹ️ Could not fetch forum topics: {e}")
    return topics


async def ingest_channel(client, channel_name: str, min_id: int = 0):
    texts, metadatas, ids = [], [], []

    try:
        # Numeric IDs: convert to PeerChannel (strip -100 prefix used by Bot API)
        if isinstance(channel_name, str) and channel_name.lstrip("-").isdigit():
            raw_id = int(channel_name)
            # Bot API format: -100XXXXXXXXXX → strip the -100 prefix
            if raw_id < 0:
                raw_id = int(str(raw_id).replace("-100", "", 1)) if str(raw_id).startswith("-100") else abs(raw_id)
            entity = await client.get_entity(PeerChannel(raw_id))
        else:
            entity = await client.get_entity(channel_name)
    except Exception as e:
        print(f"  ❌ Cannot find '{channel_name}': {e}")
        return texts, metadatas, ids

    channel_title = getattr(entity, "title", channel_name)
    channel_username = getattr(entity, "username", None) or channel_name
    print(f"  📡 {channel_title}  (incremental from id > {min_id})")

    is_forum = getattr(entity, "forum", False)
    topics = {}
    if is_forum:
        topics = await get_forum_topics(client, entity)
        print(f"  📂 Forum with {len(topics)} topics")

    count = 0
    iter_kwargs = {"limit": MAX_MESSAGES}
    if min_id > 0:
        iter_kwargs["min_id"] = min_id
    elif _DAYS_BACK > 0:
        # First run: cap by date, not message count — always gets the newest N days
        iter_kwargs["offset_date"] = datetime.now(timezone.utc) - timedelta(days=_DAYS_BACK)

    async for message in client.iter_messages(entity, **iter_kwargs):
        if isinstance(message, MessageService):
            continue
        if not message.text or len(message.text.strip()) < 10:
            continue

        text = message.text.strip()
        msg_date = message.date.strftime("%Y-%m-%d %H:%M") if message.date else ""

        topic_name = ""
        topic_id = None
        if is_forum and message.reply_to:
            topic_id = (
                getattr(message.reply_to, "reply_to_top_id", None)
                or getattr(message.reply_to, "reply_to_msg_id", None)
            )
            if topic_id and topic_id in topics:
                topic_name = topics[topic_id]

        # Build full URL — for forum topics, deep-link to the topic thread
        if hasattr(entity, "username") and entity.username:
            base = f"https://t.me/{entity.username}"
            if is_forum and topic_id:
                url = f"{base}/{topic_id}/{message.id}"
            else:
                url = f"{base}/{message.id}"
        else:
            url = f"https://t.me/c/{entity.id}/{message.id}"

        sender_name = ""
        if message.sender:
            s = message.sender
            if hasattr(s, "first_name"):
                sender_name = f"{s.first_name or ''} {s.last_name or ''}".strip()
            elif hasattr(s, "title"):
                sender_name = s.title

        doc_id = f"tg_{entity.id}_{message.id}"

        texts.append(text)
        metadatas.append({
            "source": "telegram",
            "channel": channel_title,
            "channel_username": channel_username,
            "topic": topic_name,
            "author": sender_name,
            "date": msg_date,
            "url": url,
            "message_id": message.id,
        })
        ids.append(doc_id)

        count += 1
        if count % 500 == 0:
            print(f"    Collected {count} new messages...")

    hit_limit = MAX_MESSAGES and count >= MAX_MESSAGES
    if hit_limit and metadatas:
        oldest_date = metadatas[-1].get("date", "unknown")
        print(f"  ⚠️  {count} messages collected but hit MAX_MESSAGES_PER_CHANNEL={MAX_MESSAGES} limit!")
        print(f"     Oldest message fetched: {oldest_date} — earlier messages were NOT indexed.")
        print(f"     Set MAX_MESSAGES_PER_CHANNEL=0 in .env to remove the limit.")
    else:
        print(f"  ✅ {count} new messages from {channel_title}")
    return texts, metadatas, ids


async def main():
    if not API_ID or not API_HASH:
        print("❌ TG_API_ID and TG_API_HASH must be set")
        sys.exit(1)
    if not CHANNELS:
        print("❌ TG_CHANNELS must be set")
        sys.exit(1)

    os.makedirs(SESSION_DIR, exist_ok=True)
    session_path = os.path.join(SESSION_DIR, "telegram_session")

    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start(phone=PHONE)
    me = await client.get_me()
    print(f"✅ Connected to Telegram as {me.first_name}")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from search_engine import add_documents, get_max_message_id_per_channel

    print("\n📊 Checking last sync state from LanceDB...")
    max_ids = get_max_message_id_per_channel()
    if max_ids:
        for ch, mid in max_ids.items():
            print(f"  • {ch}: last id = {mid}")
    else:
        print("  (empty index — full sync)")

    total_new = 0
    for channel in CHANNELS:
        print(f"\n🔄 Processing: {channel}")
        min_id = max_ids.get(channel, 0)
        texts, metadatas, ids = await ingest_channel(client, channel, min_id=min_id)
        if texts:
            added = add_documents(texts, metadatas, ids)
            total_new += added
            print(f"  📊 Indexed {added} new messages")

    await client.disconnect()
    print(f"\n🎉 Sync complete. Total new messages: {total_new}")


if __name__ == "__main__":
    asyncio.run(main())
