"""
Semantic search engine: LanceDB (embedded) + local embeddings + Azure OpenAI RAG.
LanceDB stores vectors on disk via the columnar Lance format.
Local sentence-transformers handles embeddings. Azure OpenAI handles chat.
"""
from __future__ import annotations
import os
import warnings
warnings.filterwarnings("ignore", message=".*urllib3.*OpenSSL.*LibreSSL.*")
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
DEFAULT_RESULTS = int(os.environ.get("DEFAULT_RESULTS", 30))
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
    """Create IVF-PQ vector index and FTS index once the table is large enough."""
    row_count = table.count_rows()
    if row_count < INDEX_THRESHOLD:
        return
    try:
        existing = table.list_indices()
        has_vector = any(i.index_type in ("IvfPq",) for i in existing)
        has_fts = any("FTS" in str(i.index_type) or "fts" in (i.name or "") for i in existing)
    except Exception:
        has_vector = False
        has_fts = False

    if not has_vector:
        print(f"  Creating IVF-PQ index on {row_count:,} vectors...")
        try:
            table.create_index(metric="cosine", vector_column_name="vector")
            print("  Index created.")
        except Exception as e:
            print(f"  ⚠️ Index creation failed (will retry next sync): {e}")

    if not has_fts:
        print(f"  Creating FTS index on {row_count:,} documents...")
        try:
            table.create_fts_index(
                "text", replace=True,
                stem=False, remove_stop_words=False, lower_case=True,
            )
            print("  FTS index created.")
        except Exception as e:
            print(f"  ⚠️ FTS index creation failed: {e}")


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
def _ensure_fts_index(table) -> bool:
    """Create FTS index on-demand if missing. Returns True if FTS is available."""
    try:
        existing = table.list_indices()
        if any("FTS" in str(i.index_type) or "fts" in (i.name or "") for i in existing):
            return True
    except Exception:
        pass
    try:
        table.create_fts_index(
            "text", replace=True,
            stem=False, remove_stop_words=False, lower_case=True,
        )
        return True
    except Exception:
        return False


def rebuild_fts_index() -> None:
    """Rebuild (or create) the FTS index on the full table. Call after ingestion."""
    table = get_table()
    if table.count_rows() == 0:
        return
    print(f"  Rebuilding FTS index on {table.count_rows():,} documents...")
    try:
        table.create_fts_index(
            "text", replace=True,
            stem=False, remove_stop_words=False, lower_case=True,
        )
        print("  FTS index ready.")
    except Exception as e:
        print(f"  ⚠️ FTS index rebuild failed: {e}")


def search(
    query: str,
    n_results: int = DEFAULT_RESULTS,
    source_filter: str | None = None,
    channel_filter: list[str] | None = None,
) -> list[dict]:
    """Hybrid search: vector (semantic) + FTS (keyword), fused with RRF."""
    table = get_table()
    if table.count_rows() == 0:
        return []

    # Expand query: add full forms for abbreviations/slang
    expanded = _expand_query(query)
    search_query = expanded if expanded else query

    # ── Build WHERE clause ──
    where_parts = []
    if source_filter:
        where_parts.append(f"source = '{source_filter.replace(chr(39), chr(39)*2)}'")
    if channel_filter:
        escaped = [c.replace("'", "''") for c in channel_filter]
        in_list = ", ".join(f"'{c}'" for c in escaped)
        where_parts.append(f"channel IN ({in_list})")
    where_clause = " AND ".join(where_parts) if where_parts else None

    # ── Vector search ──
    query_vector = embed_query(search_query)
    fetch_limit = min(n_results * 5, 150)
    vec_builder = table.search(query_vector).limit(fetch_limit)
    if where_clause:
        vec_builder = vec_builder.where(where_clause)
    vec_results = vec_builder.to_list()

    # ── FTS search (keyword) ──
    fts_results = []
    if _ensure_fts_index(table):
        try:
            fts_builder = table.search(query, query_type="fts").limit(fetch_limit)
            if where_clause:
                fts_builder = fts_builder.where(where_clause)
            fts_results = fts_builder.to_list()
        except Exception:
            pass

    # ── Reciprocal Rank Fusion (weighted: FTS gets 1.5× weight) ──
    RRF_K = 60  # standard RRF constant
    VEC_WEIGHT = 1.0
    FTS_WEIGHT = 1.5  # boost keyword matches
    rrf_scores: dict[str, float] = {}
    all_results: dict[str, dict] = {}

    for rank, r in enumerate(vec_results):
        doc_id = r["id"]
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0) + VEC_WEIGHT / (RRF_K + rank + 1)
        all_results[doc_id] = r

    for rank, r in enumerate(fts_results):
        doc_id = r["id"]
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0) + FTS_WEIGHT / (RRF_K + rank + 1)
        if doc_id not in all_results:
            all_results[doc_id] = r

    # ── Recency boost on top of RRF score ──
    now = datetime.now()
    RECENCY_BOOST_MAX = 0.002  # scaled for RRF scores (~0.016 range)
    RECENCY_HALF_LIFE_DAYS = 30

    # Substring match boost: if the raw query appears literally in the text
    query_lower = query.lower()

    output = []
    for doc_id, rrf in rrf_scores.items():
        r = all_results[doc_id]

        recency_boost = 0.0
        date_str = r.get("date", "")
        if date_str:
            try:
                msg_date = datetime.strptime(date_str[:16], "%Y-%m-%d %H:%M")
                days_ago = max(0, (now - msg_date).days)
                recency_boost = RECENCY_BOOST_MAX * (0.5 ** (days_ago / RECENCY_HALF_LIFE_DAYS))
            except (ValueError, TypeError):
                pass

        # Exact substring match bonus (case-insensitive)
        text_lower = r.get("text", "").lower()
        exact_boost = 0.015 if query_lower in text_lower else 0.0

        # Compute display similarity from vector distance if available
        distance = r.get("_distance")
        if distance is not None:
            base_sim = max(0.0, 1.0 - distance)
        else:
            # FTS-only result: use min similarity from vector results as floor
            base_sim = 0.85

        output.append({
            "text": r.get("text", ""),
            "source": r.get("source", ""),
            "channel": r.get("channel", ""),
            "topic": r.get("topic", ""),
            "author": r.get("author", ""),
            "date": r.get("date", ""),
            "url": r.get("url", ""),
            "similarity": round(base_sim + recency_boost * 10, 4),  # display only
            "_rrf": rrf + recency_boost + exact_boost,
        })

    output.sort(key=lambda x: -x["_rrf"])
    # Clean up internal score
    for o in output:
        del o["_rrf"]
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
                    "and informal terms relevant to Ukrainian/Polish/IT context. "
                    "Return ONLY the expanded query — no explanations.\n"
                    "Examples:\n"
                    # Polish bureaucracy / residency
                    "КП -> КП (карта побиту, karta pobytu, посвідка на проживання)\n"
                    "КС -> КС (карта сталего побиту, karta stałego pobytu)\n"
                    "КЧ -> КЧ (карта часового побиту, karta czasowego pobytu)\n"
                    "КП тимчасова -> karta pobytu tymczasowa\n"
                    "блакитна карта -> blue card, EU Blue Card, karta niebieska\n"
                    "блушка -> blue card, EU Blue Card\n"
                    "UKR -> UKR (захист тимчасовий, tymczasowa ochrona, temporary protection)\n"
                    "ТО -> ТО (тимчасова охорона, tymczasowa ochrona)\n"
                    "PESEL -> PESEL (польський ідентифікаційний номер)\n"
                    "NIP -> NIP (польський податковий номер, numer identyfikacji podatkowej)\n"
                    "REGON -> REGON (реєстраційний номер підприємства в Польщі)\n"
                    "ВНЖ -> ВНЖ (вид на жительство, карта побиту)\n"
                    # Polish taxes / finance
                    "ксеф -> KSeF (Krajowy System e-Faktur, електронні фактури)\n"
                    "KSeF -> KSeF (Krajowy System e-Faktur)\n"
                    "ZUS -> ZUS (Zakład Ubezpieczeń Społecznych, соціальне страхування)\n"
                    "PIT -> PIT (podatek dochodowy, податок на доходи фізосіб)\n"
                    "PIT-37 -> PIT-37 (річна декларація з доходів)\n"
                    "CIT -> CIT (podatek dochodowy od osób prawnych, корпоративний податок)\n"
                    "VAT -> VAT (podatek od towarów i usług, ПДВ)\n"
                    "US -> US (urząd skarbowy, податкова)\n"
                    "JP -> JP (jednoosobowa działalność gospodarcza, ФОП)\n"
                    "ФОП -> ФОП (фізична особа-підприємець, działalność gospodarcza)\n"
                    "DG -> DG (działalność gospodarcza, підприємницька діяльність)\n"
                    "B2B -> B2B (контракт між підприємцями, umowa B2B)\n"
                    "UoP -> UoP (umowa o pracę, трудовий договір)\n"
                    "UoZ -> UoZ (umowa zlecenie, договір доручення)\n"
                    "UoD -> UoD (umowa o dzieło, договір підряду)\n"
                    # Polish government / offices
                    "UdSC -> UdSC (Urząd do Spraw Cudzoziemców, відділ у справах іноземців)\n"
                    "МЗС -> МЗС (міністерство закордонних справ)\n"
                    "МОЗ -> МОЗ (міністерство охорони здоров'я)\n"
                    "ZUS -> ZUS (соціальне страхування, Zakład Ubezpieczeń Społecznych)\n"
                    "NFZ -> NFZ (Narodowy Fundusz Zdrowia, медичне страхування)\n"
                    "PFRON -> PFRON (Państwowy Fundusz Rehabilitacji Osób Niepełnosprawnych)\n"
                    # IT / tech
                    "AD -> AD (Active Directory)\n"
                    "GPO -> GPO (Group Policy Object, групова політика)\n"
                    "DC -> DC (Domain Controller, контролер домену)\n"
                    "LDAP -> LDAP (протокол каталогів)\n"
                    "SSO -> SSO (Single Sign-On, єдиний вхід)\n"
                    "MFA -> MFA (Multi-Factor Authentication, двофакторна аутентифікація)\n"
                    "VPN -> VPN (Virtual Private Network)\n"
                    "CI/CD -> CI/CD (Continuous Integration / Continuous Deployment)\n"
                    "k8s -> k8s (Kubernetes)\n"
                    "ТЗ -> ТЗ (технічне завдання, technical specification)\n"
                    # Military / Ukraine
                    "ТЦК -> ТЦК (територіальний центр комплектування, військкомат)\n"
                    "ВЛК -> ВЛК (військово-лікарська комісія)\n"
                    "ТрО -> ТрО (Територіальна оборона)\n"
                    "ЗСУ -> ЗСУ (Збройні Сили України)\n"
                    "ССО -> ССО (Сили Спеціальних Операцій)\n"
                    "СБУ -> СБУ (Служба Безпеки України)\n"
                    "ГУР -> ГУР (Головне управління розвідки)\n"
                    "МО -> МО (Міністерство оборони)\n"
                    "ВСП -> ВСП (военная служба по контракту)\n"
                    "ДМБ -> ДМБ (демобілізація)\n"
                    "If no abbreviations or slang found, return the query unchanged."
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
    for i, r in enumerate(results, 1):
        source_info = f"[{r['source']}] {r['channel']}"
        if r.get("topic"):
            source_info += f" -> {r['topic']}"
        if r.get("author"):
            source_info += f" — {r['author']}"
        if r.get("date"):
            source_info += f" ({r['date']})"
        link = f"\nLink: {r['url']}" if r.get("url") else ""
        context_parts.append(f"--- [{i}] ({source_info}) ---\n{r['text']}{link}")

    context = "\n\n".join(context_parts)
    lang_map = {"uk": "Ukrainian", "ru": "Russian", "pl": "Polish", "en": "English"}
    lang_name = lang_map.get(language, "Ukrainian")

    system_prompt = (
        f"You are a helpful assistant that answers questions based on the provided context "
        f"from Telegram channels. Always answer in {lang_name}. "
        f"Be concrete and informative. Use bullet points or structure when it helps clarity. "
        f"Cite sources using their number in square brackets, e.g. [1], [2, 5]. "
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
