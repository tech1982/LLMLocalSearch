"""
Semantic search engine: LanceDB (embedded) + local embeddings + Azure OpenAI RAG.
LanceDB stores vectors on disk via the columnar Lance format.
Local sentence-transformers handles embeddings. Azure OpenAI handles chat.
"""
from __future__ import annotations
import os
from datetime import datetime
import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer
from openai import AzureOpenAI
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# Suppress noisy but harmless warnings
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# ─── Config ──────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_PROJECT_ROOT, "data"))
LANCE_DB_PATH = os.path.join(DATA_DIR, "lance")
TABLE_NAME = "messages"
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large-instruct")
VECTOR_DIM = 1024  # multilingual-e5-large-instruct

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4-1")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

# Index is auto-created after table grows past this many rows
INDEX_THRESHOLD = 50_000

_model = None
_openai_client = None
_db = None


# ─── Clients ─────────────────────────────────────────────────────
def _get_openai_client() -> AzureOpenAI:
    global _openai_client
    if _openai_client is None:
        if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_KEY:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY must be set in .env")
        _openai_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_KEY,
            api_version=AZURE_OPENAI_API_VERSION,
        )
    return _openai_client


def get_embedding_model() -> SentenceTransformer:
    global _model
    if _model is None:
        cache_dir = os.path.join(DATA_DIR, "model_cache")
        os.makedirs(cache_dir, exist_ok=True)
        _model = SentenceTransformer(EMBEDDING_MODEL, cache_folder=cache_dir)
    return _model


def get_db() -> lancedb.DBConnection:
    global _db
    if _db is None:
        os.makedirs(LANCE_DB_PATH, exist_ok=True)
        _db = lancedb.connect(LANCE_DB_PATH)
    return _db


def _schema() -> pa.Schema:
    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("channel", pa.string()),
        pa.field("channel_username", pa.string()),
        pa.field("topic", pa.string()),
        pa.field("author", pa.string()),
        pa.field("date", pa.string()),
        pa.field("url", pa.string()),
        pa.field("message_id", pa.int64()),
    ])


def get_table():
    db = get_db()
    if TABLE_NAME not in db.table_names():
        return db.create_table(TABLE_NAME, schema=_schema())
    return db.open_table(TABLE_NAME)


# ─── Embeddings ──────────────────────────────────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed document texts with e5-instruct 'passage:' prefix."""
    model = get_embedding_model()
    cleaned = [t[:8000] if len(t) > 8000 else t for t in texts]
    prefixed = [f"passage: {t}" for t in cleaned]
    embeddings = model.encode(prefixed, show_progress_bar=True, batch_size=32)
    return embeddings.tolist()


def embed_query(query: str) -> list[float]:
    model = get_embedding_model()
    task = "Given a search query, retrieve relevant Telegram messages"
    embedding = model.encode(f"Instruct: {task}\nquery: {query}")
    return embedding.tolist()


# ─── Indexing maintenance ────────────────────────────────────────
def _ensure_index(table) -> None:
    """Create an IVF-PQ index once the table is large enough."""
    row_count = table.count_rows()
    if row_count < INDEX_THRESHOLD:
        return
    try:
        existing = table.list_indices()
        if existing:
            return
    except Exception:
        pass

    print(f"  Creating IVF-PQ index on {row_count:,} vectors...")
    try:
        table.create_index(metric="cosine", vector_column_name="vector")
        print("  Index created.")
    except Exception as e:
        print(f"  ⚠️ Index creation failed (will retry next sync): {e}")


# ─── Document operations ────────────────────────────────────────
def _existing_ids(table, ids: list[str]) -> set[str]:
    """Check which IDs already exist in the table."""
    if table.count_rows() == 0 or not ids:
        return set()
    existing = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        escaped = ", ".join("'" + x.replace("'", "''") + "'" for x in chunk)
        try:
            result = table.to_lance().to_table(
                columns=["id"],
                filter=f"id IN ({escaped})",
            )
            existing.update(result.column("id").to_pylist())
        except Exception:
            pass
    return existing


def add_documents(
    texts: list[str],
    metadatas: list[dict],
    ids: list[str],
    batch_size: int = 100,
) -> int:
    """Add documents, skipping existing IDs. Returns count added."""
    table = get_table()
    existing = _existing_ids(table, ids)

    new_records: list[tuple[str, str, dict]] = []
    for t, m, id_ in zip(texts, metadatas, ids):
        if id_ not in existing:
            new_records.append((id_, t, m))

    if not new_records:
        print("  No new documents to add.")
        return 0

    print(f"  Embedding {len(new_records)} new documents...")
    total_added = 0

    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        batch_texts = [r[1] for r in batch]
        embeddings = embed_texts(batch_texts)

        rows = []
        for (id_, text, meta), vec in zip(batch, embeddings):
            rows.append({
                "id": id_,
                "vector": vec,
                "text": text,
                "source": meta.get("source", ""),
                "channel": meta.get("channel", ""),
                "channel_username": meta.get("channel_username", ""),
                "topic": meta.get("topic", ""),
                "author": meta.get("author", ""),
                "date": meta.get("date", ""),
                "url": meta.get("url", ""),
                "message_id": int(meta.get("message_id", 0)),
            })

        table.add(rows)
        total_added += len(rows)
        print(f"    Added batch {i // batch_size + 1} ({total_added}/{len(new_records)})")

    _ensure_index(table)
    return total_added


# ─── State queries (for incremental sync and UI) ────────────────
def get_max_message_id_per_channel() -> dict[str, int]:
    """Return the highest stored message_id per Telegram channel.
    Used for incremental sync — no state file needed."""
    table = get_table()
    if table.count_rows() == 0:
        return {}

    arrow = table.to_lance().to_table(
        columns=["channel_username", "message_id"],
        filter="source = 'telegram'",
    )
    if arrow.num_rows == 0:
        return {}

    df = arrow.to_pandas()
    grouped = df.groupby("channel_username")["message_id"].max()
    return {str(k): int(v) for k, v in grouped.items() if k}


def get_min_message_id_per_channel() -> dict[str, int]:
    """Return the lowest stored message_id per Telegram channel.
    Used for backfill — fetching older messages that were missed."""
    table = get_table()
    if table.count_rows() == 0:
        return {}

    arrow = table.to_lance().to_table(
        columns=["channel_username", "message_id"],
        filter="source = 'telegram'",
    )
    if arrow.num_rows == 0:
        return {}

    df = arrow.to_pandas()
    grouped = df.groupby("channel_username")["message_id"].min()
    return {str(k): int(v) for k, v in grouped.items() if k}


def list_channels() -> list[dict]:
    """All unique channels with message counts (for UI multiselect)."""
    table = get_table()
    if table.count_rows() == 0:
        return []

    arrow = table.to_lance().to_table(columns=["channel", "source"])
    df = arrow.to_pandas()
    counts = df.groupby(["source", "channel"]).size().reset_index(name="count")
    return sorted(
        [
            {
                "name": row["channel"],
                "source": row["source"],
                "count": int(row["count"]),
            }
            for _, row in counts.iterrows()
        ],
        key=lambda c: -c["count"],
    )


def get_stats() -> dict:
    table = get_table()
    total = table.count_rows()
    if total == 0:
        return {"total": 0, "telegram": 0, "instagram": 0, "channels": 0}

    channels = list_channels()
    tg = sum(c["count"] for c in channels if c["source"] == "telegram")
    ig = sum(c["count"] for c in channels if c["source"] == "instagram")
    return {"total": total, "telegram": tg, "instagram": ig, "channels": len(channels)}


# ─── Search ──────────────────────────────────────────────────────
def search(
    query: str,
    n_results: int = 10,
    source_filter: str | None = None,
    channel_filter: list[str] | None = None,
) -> list[dict]:
    """Semantic search with optional channel filter and query expansion."""
    table = get_table()
    if table.count_rows() == 0:
        return []

    # Expand query: add full forms for abbreviations/slang
    expanded = _expand_query(query)
    search_query = expanded if expanded else query

    query_vector = embed_query(search_query)
    # Fetch extra results to allow re-ranking by recency
    fetch_limit = min(n_results * 3, 100)
    builder = table.search(query_vector).limit(fetch_limit)

    where_parts = []
    if source_filter:
        where_parts.append(f"source = '{source_filter.replace(chr(39), chr(39)*2)}'")
    if channel_filter:
        escaped = [c.replace("'", "''") for c in channel_filter]
        in_list = ", ".join(f"'{c}'" for c in escaped)
        where_parts.append(f"channel IN ({in_list})")

    if where_parts:
        builder = builder.where(" AND ".join(where_parts))

    raw_results = builder.to_list()

    # Recency boost: newer messages get up to +0.05 similarity bonus
    # This helps surface recent discussions without killing semantic relevance
    now = datetime.now()
    RECENCY_BOOST_MAX = 0.05  # max bonus for today's messages
    RECENCY_HALF_LIFE_DAYS = 30  # bonus halves every 30 days

    output = []
    for r in raw_results:
        distance = r.get("_distance", 1.0)
        base_sim = max(0.0, 1.0 - distance)

        # Calculate recency boost
        recency_boost = 0.0
        date_str = r.get("date", "")
        if date_str:
            try:
                msg_date = datetime.strptime(date_str[:16], "%Y-%m-%d %H:%M")
                days_ago = max(0, (now - msg_date).days)
                recency_boost = RECENCY_BOOST_MAX * (0.5 ** (days_ago / RECENCY_HALF_LIFE_DAYS))
            except (ValueError, TypeError):
                pass

        output.append({
            "text": r.get("text", ""),
            "source": r.get("source", ""),
            "channel": r.get("channel", ""),
            "topic": r.get("topic", ""),
            "author": r.get("author", ""),
            "date": r.get("date", ""),
            "url": r.get("url", ""),
            "similarity": round(min(1.0, base_sim + recency_boost), 4),
        })

    # Re-rank by boosted similarity and return top N
    output.sort(key=lambda x: -x["similarity"])
    return output[:n_results]


# ─── RAG answer generation ──────────────────────────────────────

def _expand_query(query: str) -> str:
    """Use LLM to expand abbreviations and slang in the query for better search."""
    try:
        client = _get_openai_client()
    except RuntimeError:
        return query
    try:
        response = client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": (
                    "You expand search queries by adding full forms of abbreviations, slang, "
                    "and informal terms. Return ONLY the expanded query — no explanations.\n"
                    "Examples:\n"
                    "КП → КП (карта побиту, karta pobytu)\n"
                    "ксеф → ксеф (KSeF, Krajowy System e-Faktur)\n"
                    "ZUS → ZUS (Zakład Ubezpieczeń Społecznych, соціальне страхування)\n"
                    "PIT → PIT (podatek dochodowy, податок на доходи)\n"
                    "If no abbreviations found, return the query unchanged."
                )},
                {"role": "user", "content": query},
            ],
            temperature=0,
            max_completion_tokens=200,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return query


def generate_answer(query: str, results: list[dict], language: str = "uk") -> str:
    """Synthesize an answer from search results using Azure OpenAI."""
    if not results:
        return "No matches found for your query."

    context_parts = []
    for i, r in enumerate(results[:12], 1):
        source_info = f"[{r['source']}] {r['channel']}"
        if r.get("topic"):
            source_info += f" → {r['topic']}"
        if r.get("author"):
            source_info += f" — {r['author']}"
        if r.get("date"):
            source_info += f" ({r['date']})"
        link = f"\nLink: {r['url']}" if r.get("url") else ""
        context_parts.append(f"--- Source {i} ({source_info}) ---\n{r['text']}{link}")

    context = "\n\n".join(context_parts)
    lang_map = {"uk": "Ukrainian", "ru": "Russian", "pl": "Polish", "en": "English"}
    lang_name = lang_map.get(language, "Ukrainian")

    system_prompt = (
        f"You are a helpful assistant that answers questions based on the provided context "
        f"from Telegram channels. Always answer in {lang_name}. "
        f"Be concrete and informative. Use bullet points or structure when it helps clarity. "
        f"Cite sources by referring to '[Source N]'. "
        f"If the context does not contain an answer, say so honestly."
    )
    user_prompt = f"CONTEXT:\n{context}\n\nQUESTION: {query}\n\nANSWER:"

    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_completion_tokens=3072,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"⚠️ Azure OpenAI error: {e}"
