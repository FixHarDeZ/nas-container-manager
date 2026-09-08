# secretary

![Kim Secretary Telegram Bot](../screenshots/secretary.png)

Personal knowledge base stack. Ingests Notion pages into Qdrant and serves RAG queries via FastAPI, orchestrated with n8n Telegram bot workflows (n8n is its own stack — see [`../n8n/README.md`](../n8n/README.md)).

## Architecture

```
Notion API
    ↓ (secretary-ingest, one-shot)
Qdrant (secretary_notes collection, hybrid BGE-M3)
    ↑
secretary-query (FastAPI :5065)
    ↑
n8n (../n8n stack, :5678) → Telegram bot
```

## Services

| Service | Container | Port (host→container) | RAM limit | OMP threads | Notes |
|---|---|---|---|---|---|
| qdrant | secretary-qdrant | 6333→6333 | 1.5G | — | Collection: `secretary_notes`, named vectors `dense`+`sparse` |
| secretary-query | secretary-query | 5065→5065 | 4G | 2 | FastAPI RAG. LLM provider switchable via `LLM_PROVIDER` env. External via Synology RP :15065 |
| secretary-ingest | secretary-ingest | — | 4G | 3 | `restart: "no"`. Run once manually (see below) |

> **Resource limits:** Synology DSM's kernel ships **without the CFS scheduler**, so docker `cpus:` limits return `NanoCPUs can not be set`. CPU is therefore capped *inside* the BGE-M3 containers via `OMP_NUM_THREADS` / `MKL_NUM_THREADS` (PyTorch + FlagEmbedding obey these). Memory `limits:` work normally and are enforced. Logs are capped to 10 MB × 3 files per service.

## Quickstart

```bash
# 1. Copy env templates
cp secretary/ingest/.env.example secretary/ingest/.env
cp secretary/query/.env.example secretary/query/.env
# Fill in real values in each .env

# 2. Start persistent services
docker compose up -d qdrant secretary-query

# 3. First ingest (downloads BGE-M3 ~2GB on first run)
docker compose run --rm secretary-ingest

# 4. Test query
curl -X POST http://localhost:5065/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "what is X?"}'
```

## Volumes (NAS paths)

| Volume | NAS path |
|---|---|
| qdrant_storage | `/volume2/docker/secretary/qdrant_storage` |
| ingest_state | `/volume2/docker/secretary/ingest_state` |
| hf_cache | `/volume2/docker/secretary/hf_cache` |
| query-data | `/volume2/docker/secretary/query-data` (bind, not named volume) |

## Env Files

This stack has no root-level `.env` or `secrets.manifest.yaml` — the only one
was n8n's, and it left with the n8n stack. The two sub-services carry their own.

| File | Used by | Key variables |
|---|---|---|
| `secretary/ingest/.env` | secretary-ingest, /ingest-trigger | `NOTION_TOKEN`, `QDRANT_URL`, `NOTION_SOURCE_TYPE` |
| `secretary/query/.env` | secretary-query | `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `COHERE_API_KEY` |

## LLM Providers

Set `LLM_PROVIDER` in `query/.env`:

| Value | Auth | Notes |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | Default. Model: `claude-sonnet-4-20250514` |
| `openrouter` | `OPENROUTER_API_KEY` + `OPENROUTER_MODEL` | OpenAI-compat API |
| `nous` | OAuth 2.0 Device Code | Run `GET /nous/auth` once after deploy to authenticate |

## Reranking

Set `COHERE_API_KEY` in `query/.env` to enable Cohere reranking (`rerank-multilingual-v3.0`). Strongly recommended for Thai queries against English content — improves cross-lingual retrieval accuracy.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/query` | RAG query. Body: `{"question": str, "top_k_retrieve": int=20, "top_k_final": int=6}` |
| `GET` | `/health` | Liveness + Qdrant collection stats |
| `POST` | `/ingest-trigger` | Run incremental ingest inside the query process, reusing its resident BGE-M3. Query params: `full=true`, `page_id=<id>` |
| `GET` | `/nous/auth` | Start Nous OAuth Device Code flow |
| `GET` | `/nous/auth/status` | Check Nous auth status |

## Ingest

```bash
docker compose run --rm secretary-ingest python ingest.py          # incremental
docker compose run --rm secretary-ingest python ingest.py --full   # re-ingest all
docker compose run --rm secretary-ingest python ingest.py --page <NOTION_PAGE_ID>
docker compose run --rm secretary-ingest python ingest.py --dry-run
```

State DB for standalone ingest: `ingest_state` volume (`/volume2/docker/secretary/ingest_state`).  
State DB for `/ingest-trigger`: `query-data` volume (`/volume2/docker/secretary/query-data`).

`/ingest-trigger` imports `ingest.py` into the query process and injects the encoder that is
already resident there, so a sync run holds one BGE-M3 in RAM instead of two. It used to spawn
`ingest.py` as a subprocess, which loaded its own copy on top and exhausted the 12 GB NAS with a
host-level OOM on 2026-08-19. Every encode path (`/query`, keep-warm, ingest) shares one lock so
only one batch of activations is resident at a time; the container is capped at 4 GB.

## Embedding Model

**BGE-M3** (`BAAI/bge-m3`) via FlagEmbedding — CPU-only torch (~200MB, not CUDA ~2.5GB). Hybrid search: 1024d dense (Cosine) + sparse lexical weights, fused with RRF.

First run downloads ~2GB to `hf_cache` volume shared between ingest and query containers.

## n8n

n8n runs as its own stack — see [`../n8n/README.md`](../n8n/README.md). Its
workflows call this stack over the host's published port
(`http://host.docker.internal:5065/query`), not over a shared docker network: a
docker DNS name like `secretary-query` only resolves inside this stack's own
compose network.

Its data volume still lived under `/volume2/docker/secretary/` until the
2026-09-08 split and now sits at `/volume2/docker/n8n/n8n_data`, so deleting and
recreating this stack's DSM project no longer puts n8n's credentials at risk.
