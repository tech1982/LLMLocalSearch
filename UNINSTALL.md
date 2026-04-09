# Uninstall Guide

## Quick uninstall (everything)

```bash
# 1. Stop and remove Docker containers + network
cd /path/to/LLMLocalSearch
docker compose down

# 2. Remove Docker image
docker rmi llmlocalsearch-app

# 3. Stop and uninstall native Ollama + its dependencies
brew services stop ollama
brew uninstall ollama
brew autoremove        # removes mlx, mlx-c, python@3.14 (~280 MB, ollama-only deps)

# 4. Remove Ollama models (~3.3 GB for gemma3:4b)
rm -rf ~/.ollama

# 5. Remove project data (ChromaDB index, sessions, cached embeddings)
rm -rf data/ sessions/ ollama_models/

# 6. Remove .env (contains your API keys)
rm .env
```

---

## Selective uninstall

### Remove only Ollama (keep search working without AI answers)

```bash
brew services stop ollama
brew uninstall ollama
brew autoremove        # removes mlx, mlx-c, python@3.14 (~280 MB, ollama-only deps)
rm -rf ~/.ollama
```

The search UI will still work at http://localhost:8501 — toggle off "Generate answer (Ollama)" in the sidebar.

### Remove only indexed data (re-index from scratch)

```bash
rm -rf data/chromadb/
# Then re-run: docker exec -it semantic-search python src/ingest_telegram.py
```

### Remove only Docker containers (keep data)

```bash
docker compose down
```

Data in `data/`, `sessions/` persists on disk. Run `docker compose up -d app` to restart.

### Remove cached embedding model (~470 MB)

```bash
rm -rf data/model_cache/
```

Will re-download on next search or indexing run.

---

## What lives where

| Component | Location | Size |
|---|---|---|
| Ollama binary | `$(brew --prefix)/Cellar/ollama/` | ~50 MB |
| Ollama-only deps | mlx (~130 MB), mlx-c (~1 MB), python@3.14 (~75 MB) | ~206 MB (removed by autoremove) |
| Ollama models | `~/.ollama/models/` | ~3.3 GB per model |
| ChromaDB index | `data/chromadb/` | varies |
| Embedding model cache | `data/model_cache/` | ~470 MB |
| Telegram sessions | `sessions/` | < 1 MB |
| Docker image | `llmlocalsearch-app` | ~2 GB |
| Old container Ollama data | `ollama_models/` | safe to delete |

## Verify clean removal

```bash
# Check no containers remain
docker ps -a | grep -E "semantic-search|ollama"

# Check Ollama is gone
which ollama        # should return nothing
brew list ollama    # should say "not installed"

# Check no background services
brew services list | grep ollama
```
