# 🔍 Semantic Search — Telegram & Instagram

Local semantic search over Telegram channels/groups and Instagram accounts.
Searches by **meaning and context**, not keywords. Supports 🇺🇦 🇷🇺 🇵🇱 🇬🇧 content natively.

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Telegram    │────▶│  ChromaDB        │◀────│  Streamlit   │
│  (Telethon)  │     │  + embeddings    │     │  Web UI      │
├──────────────┤     │  (multilingual   │     │  :8501       │
│  Instagram   │────▶│   -e5-small)     │     └──────┬───────┘
│ (Instaloader)│     └───────┬──────────┘            │
└──────────────┘             │                       ▼
                      Semantic search         ┌──────────────┐
                      (cosine similarity) ───▶│  Ollama      │
                                              │  (qwen3:8b)  │
                                              │  RAG answer  │
                                              └──────────────┘
```

## Memory footprint on Mac Mini M2 Pro

| State | RAM |
|-------|-----|
| Idle (Streamlit + ChromaDB) | ~200 MB |
| During search (embedding model loads) | ~1.5 GB |
| With Ollama answer generation | +3–4 GB (temporary) |

Ollama can be stopped entirely (`brew services stop ollama`) — search still works, you just see raw results without a synthesized answer.

---

## How indexing works (incremental, not one-time)

The ingestion scripts are **idempotent**: every message/post has a unique ID, and ChromaDB skips duplicates. Re-running the script only adds **new** messages that appeared since the last run.

Three ways to keep the index fresh:

```bash
# Option 1: manual re-run whenever you want
source .venv/bin/activate && python src/ingest_telegram.py

# Option 2: cron job (e.g. every hour)
crontab -e
# add: 0 * * * * cd /path/to/LLMLocalSearch && .venv/bin/python src/ingest_telegram.py

# Option 3: auto-sync script (every 30 min, runs in background)
source .venv/bin/activate
nohup python src/auto_sync.py --interval 30 &
```

---

## Instagram: completely optional

Instagram ingestion is a **separate, manually triggered script**. If you never configure Instagram credentials and never run `ingest_instagram.py`, nothing breaks — the web UI simply searches whatever is in ChromaDB (Telegram only). No errors, no warnings.

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

# 2. Create .env from template
cp .env.example .env

# 3. Edit .env — paste your Telegram API keys
nano .env

# 4. Start Ollama (if not already running)
brew services start ollama

# 5. Run — creates venv, installs deps on first launch, then starts the app
chmod +x run.sh
./run.sh
```

Open **http://localhost:8501** in your browser.

### Index Telegram

```bash
# First run asks for a Telegram verification code
source .venv/bin/activate
python src/ingest_telegram.py
```

---

## 📱 Telegram: what can be indexed

Set channels/groups in `.env` via `TG_CHANNELS` (comma-separated):

```env
# By username (no @)
TG_CHANNELS=ITWarsawCommunity,ukraine_polska,devops_ua

# Or by numeric ID (for private groups you're a member of)
TG_CHANNELS=-1001234567890
```

**Supported:**
- ✅ Public channels
- ✅ Groups / supergroups
- ✅ Forum-style groups with topics (like ITWarsawCommunity) — topic names are preserved
- ✅ Private groups (if you're a member)
- ❌ Bots, DMs, secret chats

---

## 🔧 Useful commands

```bash
# Start the app
./run.sh

# Index Telegram
source .venv/bin/activate
python src/ingest_telegram.py

# Index Instagram (optional)
source .venv/bin/activate
python src/ingest_instagram.py

# Ollama status
brew services info ollama
ollama list

# Stop Ollama
brew services stop ollama

# Force re-index from scratch
rm -rf data/chromadb/
source .venv/bin/activate
python src/ingest_telegram.py

# Switch Ollama model
ollama pull mistral
# then change OLLAMA_MODEL in .env — no restart needed, picked up on next query
```

---

## 💡 Tips

| Operation | Time |
|-----------|------|
| First indexing of 10,000 messages | ~5–10 min |
| Search query | 1–3 sec |
| Ollama answer generation | 5–15 sec |

- The embedding model is cached in `data/model_cache/` — first run downloads ~470 MB
- Ollama models are stored in `~/.ollama/models/` (~5.2 GB for qwen3:8b)
- If RAM is tight, stop Ollama (`brew services stop ollama`) and toggle off answer generation in the sidebar
- Python dependencies live in `.venv/` — `rm -rf .venv` removes them cleanly
- See [UNINSTALL.md](UNINSTALL.md) for full cleanup instructions
