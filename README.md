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
                                              │  (gemma3:4b) │
                                              │  RAG answer  │
                                              └──────────────┘
```

## Memory footprint on Mac Mini M2 Pro

| State | RAM |
|-------|-----|
| Idle (Streamlit + ChromaDB) | ~200 MB |
| During search (embedding model loads) | ~1.5 GB |
| With Ollama answer generation | +3–4 GB (temporary) |

Ollama can be stopped entirely (`docker compose stop ollama`) — search still works, you just see raw results without a synthesized answer.

---

## How indexing works (incremental, not one-time)

The ingestion scripts are **idempotent**: every message/post has a unique ID, and ChromaDB skips duplicates. Re-running the script only adds **new** messages that appeared since the last run.

Three ways to keep the index fresh:

```bash
# Option 1: manual re-run whenever you want
docker exec -it semantic-search python src/ingest_telegram.py

# Option 2: auto-sync container (every 30 min, runs in background)
docker compose --profile sync up -d

# Option 3: host-level cron (e.g. every hour)
crontab -e
# add: 0 * * * * docker exec semantic-search python src/ingest_telegram.py
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

# 4. Install and start Ollama natively (uses Apple Metal GPU)
brew install ollama
brew services start ollama
ollama pull gemma3:4b

# 5. Create data directories and build containers
mkdir -p data sessions ollama_models
docker compose build
docker compose up -d app

# 6. Index Telegram (first run will ask for a verification code via Telegram)
docker exec -it semantic-search python src/ingest_telegram.py

# 7. Open http://localhost:8501 and search!
```

> **Note:** Ollama runs natively on macOS (not in Docker) for full Apple Silicon GPU acceleration.
> The app container connects to it via `host.docker.internal:11434`.

### (Optional) Enable auto-sync

```bash
# Starts a background container that re-indexes Telegram every 30 minutes
docker compose --profile sync up -d
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
# Container status
docker compose ps

# Logs
docker compose logs -f app

# Ollama status (runs natively, not in Docker)
brew services info ollama
ollama list

# Restart app container
docker compose restart app

# Stop (preserves data)
docker compose down

# Full wipe (deletes all indexed data)
docker compose down -v
rm -rf data/ sessions/ ollama_models/

# Force re-index from scratch
rm -rf data/chromadb/
docker exec -it semantic-search python src/ingest_telegram.py

# Switch Ollama model
ollama pull mistral
# then change OLLAMA_MODEL in .env and restart: docker compose restart app
```

---

## 💡 Tips

| Operation | Time |
|-----------|------|
| First indexing of 10,000 messages | ~5–10 min |
| Search query | 1–3 sec |
| Ollama answer generation | 5–15 sec |

- The embedding model is cached in `data/model_cache/` — first run downloads ~470 MB
- Ollama models are stored in `~/.ollama/models/` (~3.3 GB for gemma3:4b)
- If RAM is tight, stop Ollama (`brew services stop ollama`) and search without answer synthesis
- The `src/` directory is mounted as a volume — you can edit scripts without rebuilding
- See [UNINSTALL.md](UNINSTALL.md) for full cleanup instructions
