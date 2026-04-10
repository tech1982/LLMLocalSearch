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
from telethon.tl.functions.messages import GetHistoryRequest

load_dotenv()

API_ID = int(os.environ.get("TG_API_ID", 0))
API_HASH = os.environ.get("TG_API_HASH", "")
PHONE = os.environ.get("TG_PHONE", "")
def _load_channels() -> list[dict]:
    """Load channels from channels.txt (preferred) or TG_CHANNELS env var (fallback).

    channels.txt format:
      channel_name                           # simple
      channel_name | -Topic1 | -Topic2       # exclude topics
      channel_name | 180:Topic1 | 180:Topic2  # limit specific topics to N days
      channel_name | 180:*                    # limit entire channel to N days
    """
    channels_file = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "channels.txt")
    if os.path.exists(channels_file):
        channels = []
        with open(channels_file, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()  # strip inline comments
                if not line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                name = parts[0]
                excluded = [p[1:].strip() for p in parts[1:] if p.startswith("-")]
                # Parse day limits: "180:TopicName" or "180:*"
                topic_days = {}  # topic_name -> max_days ("*" for whole channel)
                for p in parts[1:]:
                    if ":" in p and not p.startswith("-"):
                        days_str, topic = p.split(":", 1)
                        days_str = days_str.strip()
                        topic = topic.strip()
                        if days_str.isdigit() and topic:
                            topic_days[topic] = int(days_str)
                channels.append({"name": name, "exclude_topics": excluded, "topic_days": topic_days})
        return channels
    # Fallback: comma-separated env var
    return [{"name": c.strip(), "exclude_topics": [], "topic_days": {}}
            for c in os.environ.get("TG_CHANNELS", "").split(",") if c.strip()]

CHANNELS = _load_channels()
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES_PER_CHANNEL", 0)) or None  # 0 = no limit
# How many days back to fetch on the first run (0 = unlimited)
_DAYS_BACK = int(os.environ.get("MAX_DAYS_BACK", 365))

FLUSH_EVERY = int(os.environ.get("FLUSH_EVERY", 10_000))  # Save to DB every N messages to avoid losing progress on Ctrl+C

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


async def ingest_channel(client, channel_name: str, min_id: int = 0, max_id: int = 0,
                         save_fn=None, exclude_topics=None, topic_days=None):
    """Fetch messages from a channel.
    - min_id > 0: forward incremental (messages newer than min_id)
    - max_id > 0: backfill (messages older than max_id)
    - both 0: first sync (respects MAX_MESSAGES / MAX_DAYS_BACK)
    - save_fn: if provided, flush to DB every FLUSH_EVERY messages
    - exclude_topics: list of topic names to skip
    - topic_days: dict {topic_name: max_days} — per-topic date limits ("*" = whole channel)
    """
    texts, metadatas, ids = [], [], []
    total_saved = 0
    excluded_count = 0

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
        return 0 if save_fn else (texts, metadatas, ids)

    channel_title = getattr(entity, "title", channel_name)
    channel_username = getattr(entity, "username", None) or channel_name

    # Get total message count for progress tracking
    total_in_channel = 0
    try:
        hist = await client(GetHistoryRequest(
            peer=entity, offset_id=0, offset_date=None,
            add_offset=0, limit=1, max_id=0, min_id=0, hash=0
        ))
        total_in_channel = hist.count or 0
    except Exception:
        pass

    if max_id > 0:
        print(f"  📡 {channel_title}  (backfill: messages older than id {max_id}) — {total_in_channel:,} total in TG")
    else:
        print(f"  📡 {channel_title}  (incremental from id > {min_id}) — {total_in_channel:,} total in TG")

    is_forum = getattr(entity, "forum", False)
    topics = {}
    if is_forum:
        topics = await get_forum_topics(client, entity)
        print(f"  📂 Forum with {len(topics)} topics")
        if exclude_topics:
            print(f"  🚫 Excluding topics: {', '.join(exclude_topics)}")

    # Build per-topic cutoff dates from topic_days config
    topic_days = topic_days or {}
    topic_cutoffs = {}  # topic_name -> cutoff datetime
    channel_cutoff_from_config = None
    now_utc = datetime.now(timezone.utc)
    if topic_days:
        for tname, days in topic_days.items():
            td_cutoff = now_utc - timedelta(days=days)
            if tname == "*":
                channel_cutoff_from_config = td_cutoff
                print(f"  📅 Channel limit: {days} days (since {td_cutoff.strftime('%Y-%m-%d')})")
            else:
                topic_cutoffs[tname] = td_cutoff
                print(f"  📅 Topic limit: {tname!r} → {days} days (since {td_cutoff.strftime('%Y-%m-%d')})")

    count = 0
    iter_kwargs = {"limit": MAX_MESSAGES}
    # Hard date cutoff — break the loop if message is older than this
    cutoff_date = None
    if channel_cutoff_from_config:
        # Per-channel config overrides global MAX_DAYS_BACK (use the more recent one)
        global_cutoff = now_utc - timedelta(days=_DAYS_BACK) if _DAYS_BACK > 0 else None
        if global_cutoff and global_cutoff > channel_cutoff_from_config:
            cutoff_date = global_cutoff
        else:
            cutoff_date = channel_cutoff_from_config
    elif _DAYS_BACK > 0:
        cutoff_date = now_utc - timedelta(days=_DAYS_BACK)
    if cutoff_date:
        print(f"  📅 Date cutoff: {cutoff_date.strftime('%Y-%m-%d')}")

    if max_id > 0:
        # Backfill: fetch messages with id < max_id (going backwards from that point)
        iter_kwargs["offset_id"] = max_id
    elif min_id > 0:
        iter_kwargs["min_id"] = min_id

    latest_date = None
    oldest_date_seen = None
    scanned = 0  # all messages seen from API (including skipped/excluded)

    async for message in client.iter_messages(entity, **iter_kwargs):
        scanned += 1
        # Hard date cutoff — stop fetching if message is too old
        if cutoff_date and message.date and message.date.replace(tzinfo=timezone.utc) < cutoff_date:
            print(f"  📅 Reached date cutoff ({cutoff_date.strftime('%Y-%m-%d')}), stopping.")
            break

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

        # Skip excluded topics
        if exclude_topics and topic_name and any(t in topic_name for t in exclude_topics):
            excluded_count += 1
            continue

        # Per-topic date limit: skip messages older than the topic's cutoff
        if topic_cutoffs and topic_name and message.date:
            msg_dt = message.date.replace(tzinfo=timezone.utc)
            skip = False
            for tname, tc in topic_cutoffs.items():
                if tname in topic_name and msg_dt < tc:
                    skip = True
                    break
            if skip:
                excluded_count += 1
                continue

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

        # Track date range
        if latest_date is None:
            latest_date = msg_date
        oldest_date_seen = msg_date

        count += 1
        if count % 5000 == 0:
            pct = f" ({scanned * 100 // total_in_channel}%)" if total_in_channel else ""
            print(f"    Collected {count:,} / scanned {scanned:,}{pct}... (at {oldest_date_seen})")

        # Flush to DB periodically to avoid losing progress
        if save_fn and len(texts) >= FLUSH_EVERY:
            print(f"    💾 Saving {len(texts):,} messages to DB...")
            added = save_fn(texts, metadatas, ids)
            total_saved += added
            print(f"    💾 Saved ({total_saved:,} total for this channel so far)")
            texts, metadatas, ids = [], [], []

    # Flush remaining
    if save_fn and texts:
        print(f"    💾 Saving final {len(texts):,} messages to DB...")
        added = save_fn(texts, metadatas, ids)
        total_saved += added
        texts, metadatas, ids = [], [], []

    hit_limit = MAX_MESSAGES and count >= MAX_MESSAGES
    date_range = f" ({oldest_date_seen} → {latest_date})" if oldest_date_seen else ""
    excluded_info = f", 🚫 {excluded_count:,} excluded" if excluded_count else ""
    if hit_limit:
        print(f"  ⚠️  {count:,} messages collected but hit MAX_MESSAGES_PER_CHANNEL={MAX_MESSAGES} limit!")
        print(f"     Date range: {date_range} — earlier messages were NOT indexed.")
        print(f"     Set MAX_MESSAGES_PER_CHANNEL=0 in .env to remove the limit.")
    else:
        print(f"  ✅ {count:,} messages from {channel_title}{date_range}{excluded_info}")

    if save_fn:
        return total_saved
    return texts, metadatas, ids


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch older messages that were missed due to limits")
    args = parser.parse_args()

    if not API_ID or not API_HASH:
        print("❌ TG_API_ID and TG_API_HASH must be set")
        sys.exit(1)
    if not CHANNELS:
        print("❌ No channels configured (channels.txt or TG_CHANNELS)")
        sys.exit(1)

    os.makedirs(SESSION_DIR, exist_ok=True)
    session_path = os.path.join(SESSION_DIR, "telegram_session")

    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start(phone=PHONE)
    me = await client.get_me()
    print(f"✅ Connected to Telegram as {me.first_name}")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from search_engine import add_documents, get_max_message_id_per_channel, get_min_message_id_per_channel, rebuild_fts_index

    if args.backfill:
        print("\n📊 Backfill mode — fetching older messages...")
        min_ids = get_min_message_id_per_channel()
        if not min_ids:
            print("  (empty index — run normal sync first)")
            await client.disconnect()
            return
        for ch, mid in min_ids.items():
            print(f"  • {ch}: oldest id = {mid}")

        total_new = 0
        ch_total = len(CHANNELS)
        for ch_idx, ch_conf in enumerate(CHANNELS, 1):
            channel = ch_conf['name']
            oldest_id = min_ids.get(channel, 0)
            if oldest_id == 0:
                print(f"\n⏭️  [{ch_idx}/{ch_total}] Skipping {channel} — not in index yet, run normal sync first")
                continue
            print(f"\n🔄 [{ch_idx}/{ch_total}] Backfill: {channel}")
            added = await ingest_channel(client, channel, max_id=oldest_id, save_fn=add_documents,
                                         exclude_topics=ch_conf['exclude_topics'],
                                         topic_days=ch_conf.get('topic_days', {}))
            total_new += added
            if added:
                print(f"  📊 Indexed {added:,} older messages")
            else:
                print(f"  ✅ No older messages to fetch")

        await client.disconnect()
        print(f"\n🎉 Backfill complete. Total older messages added: {total_new}")
        if total_new:
            rebuild_fts_index()
        return

    print("\n📊 Checking last sync state from LanceDB...")
    max_ids = get_max_message_id_per_channel()
    if max_ids:
        for ch, mid in max_ids.items():
            print(f"  • {ch}: last id = {mid}")
    else:
        print("  (empty index — full sync)")

    total_new = 0
    ch_total = len(CHANNELS)
    for ch_idx, ch_conf in enumerate(CHANNELS, 1):
        channel = ch_conf['name']
        print(f"\n🔄 [{ch_idx}/{ch_total}] Processing: {channel}")
        min_id = max_ids.get(channel, 0)
        added = await ingest_channel(client, channel, min_id=min_id, save_fn=add_documents,
                                     exclude_topics=ch_conf['exclude_topics'],
                                     topic_days=ch_conf.get('topic_days', {}))
        total_new += added
        if added:
            print(f"  📊 Indexed {added:,} new messages")

    await client.disconnect()
    print(f"\n🎉 Sync complete. Total new messages: {total_new}")
    if total_new:
        rebuild_fts_index()


if __name__ == "__main__":
    asyncio.run(main())
