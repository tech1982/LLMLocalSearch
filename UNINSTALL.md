# Uninstall Guide

## Quick uninstall (everything)

```bash
# 1. Remove Python venv and project data
cd /path/to/LLMLocalSearch
rm -rf .venv data/ sessions/

# 2. Remove .env (contains your API keys)
rm .env

# 3. Stop and uninstall native Ollama + its dependencies
brew services stop ollama
brew uninstall ollama
brew autoremove        # removes mlx, mlx-c, python@3.14 (~280 MB, ollama-only deps)

# 4. Remove Ollama models (~5.2 GB for qwen3:8b)
rm -rf ~/.ollama
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
# Then re-run: source .venv/bin/activate && python src/ingest_telegram.py
```

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
| Ollama models | `~/.ollama/models/` | ~5.2 GB (qwen3:8b) |
| ChromaDB index | `data/chromadb/` | varies |
| Embedding model cache | `data/model_cache/` | ~470 MB |
| Telegram sessions | `sessions/` | < 1 MB |
| Python venv | `.venv/` in project | ~2.5 GB |

## Verify clean removal

```bash
# Check Ollama is gone
which ollama        # should return nothing
brew list ollama    # should say "not installed"

# Check no background services
brew services list | grep ollama
```
