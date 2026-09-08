# Secretary Stack — Index

## Stack Overview
Personal knowledge base stack: ingests Notion pages into Qdrant, serves RAG queries via FastAPI, orchestrated with n8n Telegram bot workflows (n8n แยกเป็น stack ของตัวเองแล้วตั้งแต่ 2026-09-08 — ดู `../../n8n/.notes/00_INDEX.md`).

## Architecture
```
Notion API
    ↓ (secretary-ingest, one-shot)
Qdrant (secretary_notes collection)
    ↑ (hybrid search: BGE-M3 dense 1024d + sparse)
secretary-query (FastAPI :5065)
    ↑ (POST /query, POST /ingest-trigger)
n8n (stack แยก, :15678) → Telegram bot
    ยิงผ่าน http://host.docker.internal:5065 ไม่ใช่ชื่อ DNS ใน compose network
```

## Services
| Service | Container | Port | RAM / OMP | Notes |
|---|---|---|---|---|
| qdrant | secretary-qdrant | 6333 (internal) | 1.5G | Collection: `secretary_notes`, named vectors `dense`+`sparse` |
| secretary-query | secretary-query | 15065→5065 | 6G / OMP=2 | FastAPI RAG. LLM provider switchable via `LLM_PROVIDER` env. **`/ingest-trigger` spawns ingest.py subprocess inside this container** — needed 6G headroom because subprocess loads its own BGE-M3 on top of parent's resident BGE-M3 (see below) |
| secretary-ingest | secretary-ingest | — | 6G / OMP=3 | `restart: "no"`. Run: `docker compose run --rm secretary-ingest`. **Use this path for table-heavy re-ingests** |

## Resource Limits Rationale
- NAS DS925+ has 8 CPU threads / 12 GB RAM. Swap thrashes when memory pressure goes past ~9 GB.
- **Synology DSM kernel lacks CFS scheduler** — `docker run --cpus=N` returns `NanoCPUs can not be set, as your kernel does not support CPU CFS scheduler`. So docker-level CPU limits are unavailable. We cap CPU at the **application layer** via `OMP_NUM_THREADS` / `MKL_NUM_THREADS` env vars (BGE-M3 + PyTorch + FlagEmbedding obey these). Without it PyTorch reads `/proc/cpuinfo`=8 and spawns 8 OpenMP workers, saturating the NAS.
- Memory `limits` work normally on Synology and are enforced via cgroup.
- secretary-ingest gets 6 GB (not 4 GB) because FlagEmbedding's batched encode of 20–50 chunks spikes RAM transiently past the resident ~2 GB model footprint — 4 GB was confirmed too tight by OOM kill on the User-Password page (29 chunks).
- secretary-query bumped to 6 GB on 2026-06-02 (evening) after every-hour OOM kills of the `/ingest-trigger` subprocess (dmesg: anon-rss ~3.1 GB on the subprocess alone, plus ~1-2 GB parent footprint, exceeded the 4 GB ceiling). An earlier "fix" commit on the same day failed silently because of a YAML duplicate-`deploy:` key bug — see daily_log 2026-06-02. **Heads up:** this still relies on headroom, not architectural isolation — see `/ingest-trigger` row below.

## `/ingest-trigger` vs standalone ingest — Known Limitation
| | Memory budget | Notes |
|---|---|---|
| `POST /ingest-trigger?page_id=…&full=true` (subprocess inside query) | 6 GB shared with the running BGE-M3 in the parent | n8n auto-sync workflow uses this path. As of 2026-06-02 evening fix, incremental sync (~155 pages, ~2 updated) completes in ~115 s without OOM. Single very-large pages may still pressure the ceiling — `secretary-ingest` is the safer path for those. |
| `docker compose run --rm secretary-ingest python ingest.py --page <ID>` | 6 GB dedicated | Slower per-page (BGE-M3 cold load each run, ~30 s), but reliable. **Use this for User-Password and any page with multi-row tables.** |

## Volumes (NAS paths)
| Volume | Path |
|---|---|
| qdrant_storage | `/volume2/docker/secretary/qdrant_storage` |
| ingest_state | `/volume2/docker/secretary/ingest_state` |
| hf_cache | `/volume2/docker/secretary/hf_cache` |

## Env Files
- `secretary/.env` — **ไม่มีแล้ว** ย้ายไป `n8n/.env` พร้อม stack n8n (stack root ไม่มี manifest แล้ว, ingest/query มีของตัวเอง)
- `secretary/ingest/.env` — Notion token, Qdrant URL, source config
- `secretary/query/.env` — Qdrant URL, LLM provider keys, Cohere key

## Embedding Model
- **BGE-M3** (`BAAI/bge-m3`) via FlagEmbedding
- CPU-only torch (~200MB, not CUDA ~2.5GB)
- First run downloads ~2GB to `hf_cache` volume (shared between ingest + query)

## LLM Providers (query service)
`LLM_PROVIDER` env: `anthropic` (default) | `openrouter` | `nous`

| Provider | Auth | Notes |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | Default |
| `openrouter` | `OPENROUTER_API_KEY` | Via OpenAI-compat API |
| `nous` | OAuth 2.0 Device Code | Token persisted to `/data/nous_token.json`. Setup: `GET /nous/auth` → browser → approve |

### Nous OAuth Endpoints
- `GET /nous/auth` — starts device flow; returns `{verification_uri, user_code, expires_in, message}`; 503 if Nous Portal unreachable
- `GET /nous/auth/status` — returns `{authenticated: bool, expires_at: int|null}`

### Nous Auth Implementation
- File: `secretary/query/nous_auth.py` (`NousTokenManager`)
- OAuth endpoints: `portal.nousresearch.com/api/oauth/device/code` + `/token`, `client_id=hermes-cli`
- Inference URL: `https://inference-api.nousresearch.com/v1`
- Token auto-refresh with 60s buffer; `_poll_for_token` retries on network errors
- Override token path: `NOUS_TOKEN_FILE` env var

## Reranking
Optional Cohere reranking: set `COHERE_API_KEY` in `query/.env`. Model: `rerank-multilingual-v3.0`.

## Qdrant Collection Schema
- Dense vector: 1024d, Cosine distance
- Sparse vector: named `sparse`
- Payload fields: `source`, `page_id`, `page_url`, `page_title`, `breadcrumb`, `text`, `chunk_index`, `last_edited_time`, `tags`

## Tests
Run from `secretary/query/`:
```bash
pip install -r requirements-dev.txt
pip install -r requirements.txt
pytest -v   # 22 tests
```
Stubs: `FlagEmbedding` + `torch` + `cohere` mocked at `sys.modules` level (no model download needed).
Note: `ASGITransport` does not fire ASGI lifespan — conftest directly assigns `main.qdrant` and `main.app.state.model`.

## /ingest-trigger Behavior
The `POST /ingest-trigger` endpoint in `secretary-query` runs `ingest.py` as a subprocess (at `/ingest/ingest.py` inside the container, bind-mounted from `./ingest/ingest.py`). It inherits env vars from both `query/.env` and `ingest/.env`. State DB writes to `query-data` volume (`/data/ingest_state.db`), separate from the `ingest_state` volume used by `docker compose run --rm secretary-ingest`.

## /query Response — Sources Filtering (2026-05-28)
`sources` in the response now contains only chunks actually cited by the LLM (`[1]`, `[2]`, etc.), not all top_k_final hits. Regex parse in `main.py:147`. This fixes n8n showing unrelated reference links alongside the answer.

## n8n Workflow Backup
Manual scripts to export/import n8n workflows via REST API (SSH tunnel to NAS localhost:5678).

**Export:** `./scripts/n8n_export.sh` → saves workflow JSONs to `n8n/workflows/`
**Import:** `./scripts/n8n_import.sh [file.json]` → updates existing workflows by name (upsert)

Requires `N8N_API_KEY` in `secretary/.env` (generated from vault via `make secrets`).
Workflow files are git-tracked for version control.

## Notion Table Ingest — Gotcha (fixed 2026-06-12)
Simple `table` block cells can hold **soft line-breaks** (shift+enter) → literal `\n` in
`rich_text.plain_text`. `_table_to_md` now escapes those to `<br>` (keeps each row on one physical
line); `_split_table_to_rows` restores `<br>`→`\n` on parse. Without this, multi-line cell content
was silently dropped and only the first cell of the row survived as a chunk — symptom was "bot
returns the page link but says ไม่พบข้อมูล / no detail". After editing table parsing, force a
targeted re-ingest (`--page <id>`); incremental sync skips unchanged pages.

## Gaps / TODOs
- **Architectural improvement (optional):** `/ingest-trigger` currently loads BGE-M3 in a subprocess inside `secretary-query` (which already has BGE-M3 resident). This works at 6 GB but is structurally OOM-prone. Proper fix: refactor `/ingest-trigger` to launch a one-shot `secretary-ingest` container via Docker socket so the ingest workload runs in its own 6 GB cgroup. Requires mounting docker.sock into secretary-query — security trade-off worth weighing.
- **Schedule frequency:** n8n "Secretary Auto Sync" runs hourly. Most ticks update 0-2 pages. Consider dropping to every 4-6 h to amortise the BGE-M3 cold-load + Notion-poll cost. One-line workflow JSON change.

## Ingest Path — in-process ตั้งแต่ 2026-08-19
`POST /ingest-trigger` **ไม่ spawn subprocess แล้ว** — import `ingest` เข้ามาในโปรเซสของ query แล้วยัด
`SharedEncoder(app.state.model)` ใส่ `ingest.embed_model` ทำให้ BGE-M3 มีชุดเดียวใน RAM.
`ingest.main(argv)` รับ argv เป็น list เสมอเมื่อเรียกจากเซิร์ฟเวอร์ (ห้ามเรียก `main()` เปล่า —
argparse จะไปอ่าน argv ของ uvicorn). `full=true` → `argv=["--full"]` ห้ามใช้ env `FULL_INGEST`
เพราะ mutate env ในโปรเซสยาว = ทุกรอบถัดไปกลายเป็น full ingest.
ทุก encode ผ่าน `encode_lock` (threading.Lock) ทั้ง `/query`, keep-warm และ ingest — คุม memory spike.

## Memory Ceiling — secretary-query (updated 2026-08-19)
`limits.memory: 6G` บนเครื่องที่มี RAM 12 GB = ครึ่งเครื่อง. 2026-08-19 container วิ่งชนเพดานจริง
(python ingest subprocess 3.49 GB + uvicorn parent 2.51 GB) แล้วทำให้ **host** OOM ก่อนที่ cgroup limit
จะทำงาน (`constraint=CONSTRAINT_NONE ... global_oom`) — dockerd ตายตาม container ทั้ง NAS ดับ.
ปัจจุบันตั้งเป็น **4G ใน compose** (ถาวร) หลังแก้ ingest ให้ใช้โมเดลร่วม — วัดจริง steady ~0.95 GB,
peak ระหว่าง ingest 3.70 GB. ถ้าจะกดต่ำกว่านี้ต้องลด batch size ของ `upsert_chunks` ก่อน.

## ONNX Backend ไม่ได้ทำงานจริง (พบ 2026-08-19)
ตั้ง `EMBEDDING_BACKEND=onnx` ไว้ แต่ตอน start log ขึ้น
`ONNX encoder unavailable (NO_SUCHFILE: /models/bge-m3-int8.onnx); falling back to torch`
เพราะ `RUN python export_onnx.py || echo "WARN..."` ใน `query/Dockerfile` fail แบบเงียบตอน build.
ผลคือ runtime ใช้ torch/FlagEmbedding ซึ่งกิน RAM มากกว่า ONNX int8 — เป็นตัวเร่งของ OOM ข้างบน.

## Memory Ceiling — secretary-ingest 6G → 4G (2026-09-08)
`secretary-ingest` **มี `limits.memory` อยู่แล้ว = 6G** (ไม่ใช่ไม่มี limit) แต่เพดานสูงเกินไปบนเครื่อง
12 GB — container โตได้ถึง 4.08 GB `anon-rss` (total-vm 7.1 GB) โดย cgroup limit ยังไม่ทำงาน
2026-09-08 14:15:13 มันชนกับ DSM `synofoto-face-extraction` ที่กำลัง index ใบหน้าอยู่พอดี →
**global OOM ของ host** (`constraint=CONSTRAINT_NONE ... global_oom,
task_memcg=/docker/ea61abdd49af...`) container โดน kill (Exited 137)
แพตเทิร์นเดียวกับ `secretary-query` ตอน 19/08 เป๊ะ: เพดาน = ครึ่งเครื่อง แปลว่า host ตายก่อน cgroup

ผลข้างเคียงที่มองไม่ออกจากฝั่ง stack: OOM ระดับ host ลาก `synorelayd` ตายไปด้วย →
**QuickConnect ใช้ไม่ได้** (`/portal/error.html?error=6`) จน DSM restart daemon เองตอน 14:59
ทั้ง `docker ps` และ dashboard ยังเขียวหมด ไม่มีอะไรชี้ว่าเป็นเรื่อง memory

แก้แล้ว 2026-09-08: ลดเป็น **4G** เท่ากับ query. **Trade-off ที่รับไว้เอง** — คอมเมนต์เดิมในไฟล์บันทึกว่า
2026-06-01 เพดาน 4 GB เคย OOM-kill container นี้ตอน ingest หน้า User-Password มาแล้ว (batch 20–50 chunk
ที่คิด dense + sparse + colbert พร้อมกัน spike เกิน 4 GB) ถ้าโดน kill ซ้ำ **ห้ามดันกลับเป็น 6G**
ให้ลด batch size ของ `upsert_chunks` แทน — ingest ตายรันใหม่ได้ (`restart: "no"`) host ตายไม่ได้
