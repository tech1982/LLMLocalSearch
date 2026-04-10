# 🔍 Semantic Search — Telegram & Instagram

Local semantic search over Telegram channels/groups and Instagram accounts.
Searches by **meaning and context**, not keywords. Supports 🇺🇦 🇷🇺 🇵🇱 🇬🇧 content natively.

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Telegram    │────▶│  LanceDB         │◀────│  Streamlit   │
│  (Telethon)  │     │  + embeddings    │     │  Web UI      │
├──────────────┤     │  (multilingual   │     │  :8501       │
│  Instagram   │────▶│  -e5-large-      │     └──────┬───────┘
│ (Instaloader)│     │   instruct)      │            │
└──────────────┘     └───────┬──────────┘            ▼
                      Semantic search         ┌──────────────┐
                      (cosine similarity) ───▶│  Azure OpenAI │
                                              │  (gpt-4.1)   │
                                              │  RAG answer  │
                                              └──────────────┘
```

## Memory footprint on Mac Mini M2 Pro

| State | RAM |
|-------|-----|
| Idle (Streamlit + LanceDB) | ~200 MB |
| During search (embedding model loads) | ~2.5 GB |
| With Azure OpenAI answer generation | negligible (cloud API call) |

---

## How indexing works

The ingestion scripts are **idempotent**: every message has a unique ID, and LanceDB skips duplicates.

```bash
# New messages only (incremental — run regularly)
source .venv/bin/activate
python src/ingest_telegram.py

# Older messages that were missed due to limits (backfill)
source .venv/bin/activate
python src/ingest_telegram.py --backfill

# Cron job — e.g. every hour
crontab -e
# add: 0 * * * * cd /path/to/LLMLocalSearch && .venv/bin/python src/ingest_telegram.py
```

**How it works under the hood:**
- Normal sync: queries the highest stored `message_id` per channel → fetches only newer messages
- Backfill: queries the lowest stored `message_id` per channel → fetches older messages not yet in the index
- No state file needed — LanceDB is the source of truth
- `MAX_DAYS_BACK` (default 5 years) caps how far back to go on first sync or backfill
- Progress is printed every 5,000 messages with `[channel N/total]`, scanned count, and `%` of channel total
- Large ingestions flush to DB every 50,000 messages — safe to Ctrl+C without losing all progress

---

## Instagram: completely optional

Instagram ingestion is a **separate, manually triggered script**. If you never configure Instagram credentials and never run `ingest_instagram.py`, nothing breaks — the web UI simply searches whatever is in LanceDB (Telegram only). No errors, no warnings.

> ⚠️ Instagram aggressively rate-limits scrapers. If you do use it: index 10–15 accounts per session, wait hours between runs, and use an account that is 1+ year old.

---

## 🔑 Where to get API tokens

### Telegram API (required)

1. Open **https://my.telegram.org/apps** in a browser
2. Log in with your phone number (the one linked to your Telegram account)
3. Click **"API development tools"**
4. Fill in the form:
   - **App title**: anything (e.g. "My Search")
   - **Short name**: anything (e.g. "mysearch")
   - **Platform**: Desktop
5. You'll receive **`api_id`** (integer) and **`api_hash`** (string) — put them in `.env`

> These keys are tied to your account. Do not share publicly.

### Instagram (optional)

Uses **Instaloader** which logs in with a real Instagram account and pulls post captions from followed accounts or a specified list.

1. Set in `.env`:
   - `INSTA_USERNAME` — Instagram login
   - `INSTA_PASSWORD` — password
2. First run may request a **2FA code** interactively
3. After successful login, the session is saved to `sessions/` and reused

---

## 🚀 Quick start

```bash
# 1. Enter the project
cd LLMLocalSearch

# 2. Create config files from templates
cp .env.example .env
cp channels.example.txt channels.txt

# 3. Edit .env — paste your Telegram API keys and Azure OpenAI key
nano .env

# 4. Edit channels.txt — add your Telegram channels
nano channels.txt

# 5. Run — creates venv, installs deps on first launch, then starts the app
chmod +x run.sh
./run.sh
```

Open **http://localhost:8501** in your browser (or `http://<your-ip>:8501` on LAN).

---

## 📱 Telegram: what can be indexed

Edit **`channels.txt`** in the project root (copy from `channels.example.txt`):

```
# One channel per line. Comments and empty lines are ignored.

# --- IT / Tech ---
ITWarsawCommunity       # Simple — index everything

# Exclude noisy topics from a forum:
ITWarsawCommunity | -Мемаси | -Барахолка

# Limit specific topics to N days (e.g. job listings go stale):
ITWarsawCommunity | -Мемаси | 180:Вакансії | 180:Пошук роботи

# Limit the whole channel to N days:
djinni_official | 180:*
doucommunity | 180:*

# --- Private groups (numeric ID) ---
-1001234567890          # My private group
```

Fallback: if `channels.txt` doesn't exist, `TG_CHANNELS` in `.env` is used.

**`channels.txt` rules:**
| Prefix | Meaning |
|---|---|
| *(none)* | Index channel with default settings |
| `\| -Topic` | Exclude this forum topic entirely |
| `\| 180:Topic` | Only index messages from this topic newer than 180 days |
| `\| 180:*` | Limit the entire channel to 180 days |

**Supported:**
- ✅ Public channels
- ✅ Groups / supergroups
- ✅ Forum-style groups with topics — topic names are preserved
- ✅ Private groups (if you're a member) — use numeric ID
- ❌ Bots, DMs, secret chats

> **Numeric ID format**: use the Bot API format `-100XXXXXXXXXX` or the raw channel ID without prefix — both work.

---

## 🔧 Useful commands

```bash
# Start the app
./run.sh

# Index new Telegram messages (incremental)
source .venv/bin/activate
python src/ingest_telegram.py

# Backfill older messages that were missed
source .venv/bin/activate
python src/ingest_telegram.py --backfill

# Index Instagram (optional)
source .venv/bin/activate
python src/ingest_instagram.py

# Force re-index from scratch
rm -rf data/lance/
source .venv/bin/activate
python src/ingest_telegram.py
```

---

## 💡 Tips

| Operation | Time |
|-----------|------|
| First indexing (1 year of history, ~10K msgs) | ~5–10 min |
| Backfill (older messages) | depends on channel size |
| Search query | 1–3 sec |
| Azure OpenAI answer generation | 2–5 sec |

- The embedding model is cached in `data/model_cache/` — first run downloads ~1.1 GB
- Python dependencies live in `.venv/` — `rm -rf .venv` removes them cleanly
- See [UNINSTALL.md](UNINSTALL.md) for full cleanup instructions
