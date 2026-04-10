# Uninstall Guide

## Quick uninstall (everything)

```bash
# 1. Remove Python venv and project data
cd /path/to/LLMLocalSearch
rm -rf .venv data/ sessions/

# 2. Remove .env (contains your API keys)
rm .env
```

---

## Selective uninstall

### Remove only indexed data (re-index from scratch)

```bash
rm -rf data/lance/
# Then re-run: source .venv/bin/activate && python src/ingest_telegram.py
```

### Remove cached embedding model (~1.3 GB)

```bash
rm -rf data/model_cache/
```

Will re-download on next search or indexing run.

---

## What lives where

| Component | Location | Size |
|---|---|---|
| LanceDB index | `data/lance/` | varies |
| Embedding model cache | `data/model_cache/` | ~1.3 GB |
| Telegram sessions | `sessions/` | < 1 MB |
| Python venv | `.venv/` in project | ~2.5 GB |

