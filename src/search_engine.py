"""
Semantic search engine: embeddings + ChromaDB + Ollama RAG
"""
import os
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
import ollama as ollama_client
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")

_model = None

def get_embedding_model() -> SentenceTransformer:
    global _model
    if _model is None:
        cache_dir = os.path.join(DATA_DIR, "model_cache")
        os.makedirs(cache_dir, exist_ok=True)
        _model = SentenceTransformer(EMBEDDING_MODEL, cache_folder=cache_dir)
    return _model


def get_chroma_client() -> chromadb.ClientAPI:
    db_path = os.path.join(DATA_DIR, "chromadb")
    os.makedirs(db_path, exist_ok=True)
    return chromadb.PersistentClient(path=db_path)


def get_or_create_collection(name: str = "messages"):
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"}
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts with e5 prefix for better retrieval."""
    model = get_embedding_model()
    # multilingual-e5 requires "query: " or "passage: " prefix
    prefixed = [f"passage: {t}" for t in texts]
    embeddings = model.encode(prefixed, show_progress_bar=True, batch_size=64)
    return embeddings.tolist()


def embed_query(query: str) -> list[float]:
    model = get_embedding_model()
    embedding = model.encode(f"query: {query}")
    return embedding.tolist()


def add_documents(
    texts: list[str],
    metadatas: list[dict],
    ids: list[str],
    collection_name: str = "messages",
    batch_size: int = 200
):
    """Add documents to ChromaDB in batches."""
    collection = get_or_create_collection(collection_name)

    # Filter out already existing IDs
    existing = set()
    try:
        for i in range(0, len(ids), 500):
            batch_ids = ids[i:i+500]
            result = collection.get(ids=batch_ids)
            existing.update(result["ids"])
    except Exception:
        pass

    new_texts, new_metas, new_ids = [], [], []
    for t, m, id_ in zip(texts, metadatas, ids):
        if id_ not in existing:
            new_texts.append(t)
            new_metas.append(m)
            new_ids.append(id_)

    if not new_texts:
        print("No new documents to add.")
        return 0

    print(f"Embedding {len(new_texts)} new documents...")
    for i in range(0, len(new_texts), batch_size):
        batch_t = new_texts[i:i+batch_size]
        batch_m = new_metas[i:i+batch_size]
        batch_i = new_ids[i:i+batch_size]
        batch_e = embed_texts(batch_t)

        collection.add(
            documents=batch_t,
            embeddings=batch_e,
            metadatas=batch_m,
            ids=batch_i
        )
        print(f"  Added batch {i//batch_size + 1} ({len(batch_t)} docs)")

    print(f"Total added: {len(new_texts)}")
    return len(new_texts)


def search(
    query: str,
    n_results: int = 10,
    source_filter: str | None = None,
    collection_name: str = "messages"
) -> list[dict]:
    """Semantic search through indexed messages."""
    collection = get_or_create_collection(collection_name)

    if collection.count() == 0:
        return []

    query_embedding = embed_query(query)

    where_filter = None
    if source_filter and source_filter != "all":
        where_filter = {"source": source_filter}

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(n_results, collection.count()),
        where=where_filter,
        include=["documents", "metadatas", "distances"]
    )

    output = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0]
    ):
        output.append({
            "text": doc,
            "source": meta.get("source", "unknown"),
            "channel": meta.get("channel", ""),
            "author": meta.get("author", ""),
            "date": meta.get("date", ""),
            "url": meta.get("url", ""),
            "topic": meta.get("topic", ""),
            "similarity": round(1 - dist, 4)  # cosine distance → similarity
        })
    return output


def generate_answer(query: str, results: list[dict], language: str = "en") -> str:
    """Use Ollama to synthesize an answer from search results (RAG)."""
    if not results:
        return "No matches found for your query."

    context_parts = []
    for i, r in enumerate(results[:7], 1):
        source_info = f"[{r['source']}] {r['channel']}"
        if r.get("author"):
            source_info += f" — {r['author']}"
        if r.get("date"):
            source_info += f" ({r['date']})"
        context_parts.append(f"--- Source {i} ({source_info}) ---\n{r['text']}")

    context = "\n\n".join(context_parts)

    lang_map = {"uk": "Ukrainian", "ru": "Russian", "pl": "Polish", "en": "English"}
    lang_instruction = lang_map.get(language, "English")

    prompt = f"""You are a helpful assistant that answers questions using only the provided context from Telegram channels and Instagram.
Respond in {lang_instruction}. Be specific and cite sources when possible.
If the context does not contain the answer, say so honestly.

CONTEXT:
{context}

QUESTION: {query}

ANSWER:"""

    try:
        client = ollama_client.Client(host=OLLAMA_HOST)
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.3, "num_ctx": 4096}
        )
        return response["message"]["content"]
    except Exception as e:
        return f"⚠️ Ollama is unavailable ({e}). The relevant search results are shown above."


def get_stats() -> dict:
    """Get indexing statistics."""
    collection = get_or_create_collection()
    total = collection.count()

    stats = {"total": total, "telegram": 0, "instagram": 0}
    if total > 0:
        try:
            # Sample to estimate source distribution
            sample = collection.get(limit=min(total, 1000), include=["metadatas"])
            for m in sample["metadatas"]:
                src = m.get("source", "")
                if src == "telegram":
                    stats["telegram"] += 1
                elif src == "instagram":
                    stats["instagram"] += 1
            # Extrapolate
            sample_size = len(sample["metadatas"])
            if sample_size < total:
                ratio = total / sample_size
                stats["telegram"] = int(stats["telegram"] * ratio)
                stats["instagram"] = int(stats["instagram"] * ratio)
        except Exception:
            pass
    return stats
