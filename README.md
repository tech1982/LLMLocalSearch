# 🔍 Semantic Search — Telegram & Instagram

Hybrid semantic search over Telegram channels/groups and Instagram accounts.
Core search runs **locally** (embeddings + vector/FTS index). AI answer generation and query expansion use **Azure OpenAI** (cloud). Supports 🇺🇦 🇷🇺 🇵🇱 🇬🇧 content natively.

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Telegram    │────▶│  LanceDB         │◀────│  Streamlit   │
│  (Telethon)  │     │  vector + FTS    │     │  Web UI      │
├──────────────┤     │  (multilingual   │     │  :8501       │
│  Instagram   │────▶│  -e5-large-      │     └──────┬───────┘
│ (Instaloader)│     │   instruct)      │            │
└──────────────┘     └───────┬──────────┘            ▼
                      Hybrid search          ┌──────────────┐
                      (vector + FTS/RRF) ───▶│  Azure OpenAI │
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
- `MAX_DAYS_BACK` (default 3 years) caps how far back to go on first sync or backfill
- Progress is printed every 5,000 messages with `[channel N/total]`, scanned count, and `%` of channel total
- Flushes to DB every `FLUSH_EVERY` messages (default 10,000) — safe to Ctrl+C without losing progress
- After each sync, FTS index is rebuilt and Lance fragments are compacted automatically

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

## 🧠 Search quality

### Hybrid search (vector + full-text)
Every query runs two searches in parallel and merges results with **Reciprocal Rank Fusion (RRF)**:
- **Vector search** — semantic similarity via `multilingual-e5-large-instruct` embeddings
- **FTS search** — exact keyword matching via a LanceDB full-text index
- FTS results get 1.5× weight in RRF (keyword matches are usually more precise)
- An exact substring bonus (+0.015) further promotes messages that literally contain the query

### Multilingual morphology
The embedding model (`multilingual-e5-large-instruct`) natively handles Ukrainian, Russian, Polish, and English morphology. Searching for "повірка лічильника" will surface messages containing "повірці лічильника", "повірку лічильника", "перевірка лічильника" — all with >0.95 cosine similarity. No stemming config needed.

### Recency boost
Recent messages are promoted in results even when an older message is slightly more semantically similar. Boost decays exponentially with a 30-day half-life:

| Message age | Boost added |
|---|---|
| Today | +0.050 |
| 1 week | +0.044 |
| 1 month | +0.025 |
| 3 months | +0.006 |
| 6+ months | ~0 |

The search fetches 5× more candidates from the vector/FTS indexes, applies RRF + recency boost, then re-ranks and returns top-N (default: **30**).

### Query expansion (abbreviation/slang handling)
Before embedding, the query is sent to Azure OpenAI to expand abbreviations and informal terms. Examples:
- `КП` → `КП (карта побиту, karta pobytu)`
- `ксеф` → `ксеф (KSeF, Krajowy System e-Faktur)`
- `ZUS` → `ZUS (Zakład Ubezpieczeń Społecznych, соціальне страхування)`

This makes the expanded query match documents that spell things out in full. Falls back to the original query silently if Azure OpenAI is unavailable.

---

## 🖥️ Web UI

**Left sidebar** — channel filters:
- Channels grouped by category (from `channels.txt` section headers like `# --- Poland / Warsaw ---`)
- Category order preserved from the file; reorderable via the right panel
- Per-category **+/−** toggle for quick select/deselect
- Search only triggers on **Enter** or the Search button — not on channel checkbox changes

**Right panel** — admin tools (collapsed expanders):
- **⚙️ Налаштування** — result count slider (3–50, default 30), LLM toggle, language selector
- **📊 Статистика** — Telegram/Instagram/total document counts
- **➕ Додати канал** — add a new channel by username; title is auto-resolved from Telegram; optional retention limit (days)
- **🔄 Індексація** — run Telegram delta sync, backfill, or Instagram ingestion with live log output
- **📋 Порядок категорій** — reorder sidebar category groups with ▲/▼ buttons

**AI answer:**
- Source citations rendered as clickable date links: `[[2025-04-09]](url)` opening the message in Telegram web preview
- Language selector: 🇺🇦 🇬🇧 🇷🇺 🇵🇱

---

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
| Search query (with recency boost) | 1–3 sec |
| Azure OpenAI query expansion | +0.5 sec |
| Azure OpenAI answer generation | 2–5 sec |

- The embedding model is cached in `data/model_cache/` — first run downloads ~1.1 GB
- Python dependencies live in `.venv/` — `rm -rf .venv` removes them cleanly
- See [UNINSTALL.md](UNINSTALL.md) for full cleanup instructions
