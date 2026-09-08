# Secretary Stack — Daily Log

## 2026-09-08 — แยก n8n ออกไปเป็น stack ของตัวเอง

n8n ย้ายไป `n8n/` (ดู `../../n8n/.notes/daily_log.md` สำหรับรายละเอียดฝั่งนั้น)

ผลกับ stack นี้:
- `secretary/docker-compose.yml` ตัด service `n8n` + volume `n8n_data` ออก
- ลบ `secretary/secrets.manifest.yaml` และ `secretary/.env` — หลังตัด n8n ออก
  compose ไม่มี `${...}` เหลือแล้ว (ingest/query มี manifest ของตัวเองอยู่)
- ย้าย `secretary/n8n-workflows/` → `n8n/workflows/`
- `/volume2/docker/secretary/n8n_data` ย้ายออกไปที่ `/volume2/docker/n8n/n8n_data`
  (rename ภายใน volume2 = atomic) — project ของ n8n จึงไม่ขึ้นกับโฟลเดอร์นี้แล้ว
  ตอน cutover ต้อง `down` secretary ก่อนย้าย (**`down` เฉยๆ ห้ามใส่ `-v`**)

**แก้บันทึกเก่า 2026-06-23 ที่ผิด:** entry นั้นสรุปว่า `device:` ใน compose
"เป็น path ที่ไม่ได้ map จริง" — ไม่จริง. วัดบน NAS วันนี้ (`stat` ผ่าน
throwaway container mount ทั้งสอง path) ได้ device+inode ตรงกันเป๊ะ
(`43 35734`) = `/volume2/@docker/volumes/secretary_n8n_data/_data` เป็นแค่จุดที่
docker เอา bind ไปแปะ ไฟล์ชุดเดียวกับ `/volume2/docker/secretary/n8n_data`
(นั่นคือเหตุผลที่ `chown` ที่ `_data` ตอนนั้นแก้ปัญหาได้ — มันคือไฟล์เดียวกัน)
แผน `mv` จึงย้าย data จริง
- workflow เรียก stack นี้ผ่าน `http://host.docker.internal:5065` แทน
  `http://secretary-query:5065` แล้ว (คนละ compose network กันแล้ว)
- ยังไม่ deploy

## 2026-08-19 (รอบสอง) — แก้ถาวร: /ingest-trigger ใช้ encoder ตัวเดียวกับ query + เพดาน 4 GB

1. **`ingest/ingest.py`:** ย้าย `BGEM3FlagModel("BAAI/bge-m3")` จาก module level ไปเป็น lazy
   `get_embed_model()` และเปลี่ยน `embed_model` เป็น `None` เพื่อให้ inject จากภายนอกได้.
   `main()` รับ `argv` แล้ว (`main(argv=None)` + `parser.parse_args(argv)`) และ **return stats**.
   CLI (`docker compose run --rm secretary-ingest`) ยังโหลดโมเดลเองเหมือนเดิม.
2. **`query/main.py`:** `/ingest-trigger` เลิก `create_subprocess_exec` แล้ว import `ingest` ในโปรเซสเดียวกัน
   (`sys.path` + `INGEST_PATH`, default `/ingest`) แล้ว inject `SharedEncoder(app.state.model)` เข้าไป →
   BGE-M3 มีชุดเดียวใน RAM ตลอด. `full=true` ส่งเป็น `argv=["--full"]` ไม่ใช่ `os.environ["FULL_INGEST"]`
   (ถ้า mutate env ในโปรเซสยาว ๆ ทุก tick ถัดไปจะกลายเป็น full ingest ถาวร).
   `summary` ยังเป็น string รูปเดิม (`pages: N | updated: N | ...`) เพื่อไม่ให้ n8n node พัง.
3. **Lock:** `encode_lock` (`threading.Lock`) ครอบทุกทางที่เรียก encode — `/query`, keep-warm, และ ingest —
   เหตุผลคือ **memory ไม่ใช่ thread-safety**: encode พร้อมกันสองที่ = activations สองชุด ซึ่งเป็น spike
   ที่เพดาน cgroup ต้องรับ. ล็อกต่อ batch ไม่ใช่ต่อ run (ไม่งั้น query จะรอทั้งรอบ sync เป็นนาที).
   `ingest_lock` (asyncio) กัน ingest ซ้อนกันเอง.
4. **`docker-compose.yml`:** `secretary-query` limit 6G → **4G** (เครื่องมี 12 GB — 6G คือครึ่งเครื่อง
   จึงทำให้ host OOM ก่อน cgroup limit ทำงาน). ไม่ได้ลดเป็น 2G เพราะวัดจริงแล้ว peak ระหว่าง ingest
   ~3.7 GB.
5. **วัดผลจริงหลัง deploy:** `grep -c "Loading BGE-M3"` = **1** ตลอด ingest run (เดิมจะเป็น 2),
   `docker top` เห็นแค่ uvicorn ตัวเดียวไม่มี subprocess, steady state ~0.95 GB, peak ระหว่าง ingest
   3.70 GB / 4 GB, host ไม่ OOM, `RestartCount` ไม่ขยับ.
6. **ยังไม่ได้ทำ:** ลดความถี่ n8n auto sync (ไม่จำเป็นแล้วหลังแก้ข้อ 1 และต้องแก้ผ่าน n8n API/UI),
   และซ่อม ONNX export (ยัง fallback torch อยู่ — ดู 00_INDEX.md).

## 2026-08-19 — Global (host) OOM ล้ม dockerd ทั้งเครื่อง — ต้นเหตุ secretary-query ~6 GB

1. **อาการ:** 13:11:38 (+07) container เกือบทุกตัวบน NAS ดับพร้อมกัน (exit 137/143/0) เหลือแค่ตัวที่ตั้ง
   `restart: always` (homepage, uptime-kuma, jellyfin, watchtower, notifier) ที่กลับมาเอง ส่วนทุกตัวที่เป็น
   `restart: unless-stopped` ค้างดับยาว 2 ชม.
2. **Root cause:** kernel OOM แบบทั้งเครื่อง ไม่ใช่ cgroup —
   `oom-kill:constraint=CONSTRAINT_NONE ... global_oom, task_memcg=/docker/5eb32457c853...` ซึ่ง container id
   นั้นคือ `secretary-query` ตรง ๆ. ตอนถูกฆ่า process ในนั้นกิน `python` (ingest subprocess) 3.49 GB +
   `uvicorn` (parent) 2.51 GB ≈ 6 GB = เพดาน `deploy.resources.limits.memory: 6G` ที่ตั้งไว้พอดี. เครื่องมี RAM
   12 GB ทั้งเครื่อง เพดาน 6 G จึงกินครึ่งเครื่อง — host หมด RAM ก่อนที่ cgroup limit จะทำงาน. ก่อนหน้านั้นมี
   OOM รอบแรก 12:12:58 ฆ่า uvicorn 1.9 GB (secretary-query restart 1 ครั้ง) และย้อนไปเคยเกิดรัวรายชั่วโมง
   2026-07-23 04:01–08:01 ด้วย.
3. **ทำไมถึงโต 6 GB:** `POST /ingest-trigger` spawn `ingest.py` เป็น subprocess ใน container เดียวกับ query
   server → BGE-M3 โหลดซ้อนสองชุด. n8n "Secretary Auto Sync" ยิง `/ingest-trigger` ทุกชั่วโมงตรง (ยืนยันจาก log
   `192.168.192.2 - "POST /ingest-trigger" 200 OK` ที่ HH:01:4x ทุกชั่วโมง).
4. **ตัวเร่งที่เพิ่งเจอ:** parent ใหญ่กว่าที่ comment ใน compose ประเมินไว้ (1–2 GB) เพราะ ONNX backend
   ใช้ไม่ได้จริง — `/models/bge-m3-int8.onnx` ไม่มีในอิมเมจ (`export_onnx.py` ตอน build fail แบบ soft-fail
   `|| echo WARN`) จึง fallback ไป torch/FlagEmbedding ที่กิน RAM มากกว่า ทั้งที่ตั้ง `EMBEDDING_BACKEND=onnx`.
5. **ผลลูกโซ่:** dockerd (pid 22418) ตายตามไปด้วย แล้วขึ้นใหม่เป็น pid 23414 ตอน 13:12:08. ไม่มี log ว่า
   Container Manager package restart (ContainerManager.log ล่าสุดคือ 2026-07-24) — คือ daemon ตายเอง ไม่ใช่แพ็กเกจถูกสั่งหยุด.
6. **กู้คืน (ทำแล้ว):** `docker start` ทุก container ที่ดับ + `docker update --memory 3g --memory-swap 4g
   secretary-query` เพื่อจำกัดรัศมีความเสียหายให้ OOM เกิดใน cgroup แทนที่จะลาก host ลงทั้งเครื่อง.
   **หมายเหตุ: `docker update` มีผลเฉพาะ runtime — หายทันทีที่ `compose up` รอบหน้า** และเพดาน 3 G แปลว่า
   ingest subprocess รายชั่วโมงจะโดน cgroup OOM kill แทน (ตาม comment ใน compose ที่ระบุว่า 4 G ก็ยังโดน)
   คือยอมให้ ingest พังแทนที่จะให้ NAS ล่ม.
7. **Fix ที่ยังค้าง:** ตามที่ 00_INDEX.md จดไว้อยู่แล้ว — แยก `/ingest-trigger` ไปรันเป็น one-shot container
   `secretary-ingest` ของตัวเอง (คนละ cgroup) แทน subprocess, และ/หรือ ลดความถี่ auto sync จากรายชั่วโมงเป็นทุก 4–6 ชม.,
   และซ่อม ONNX export ให้ได้จริงเพื่อลด RSS ของ parent.

## 2026-07-10 — Fix secretary-n8n crash loop (JavaScript heap OOM)
1. **Root cause**: n8n container had 1G memory limit but V8 heap was hitting its default ~256 MB limit, causing `Ineffective mark-compacts near heap limit Allocation failed - JavaScript heap out of memory` crash loop. RestartCount reached 330.
2. **Fix**: Increased n8n memory limit from 1G → 1536M (1.5G) and added `NODE_OPTIONS=--max-old-space-size=1024` to let V8 use more of the container's memory.
3. **Verified**: RestartCount reset to 0, status running, OOMKilled false, n8n ready on port 5678.

## 2026-07-07 — Fix secretary-n8n crash loop (permission denied on n8n_data volume)
1. **Root cause**: n8n container runs as `node` (UID 1000:1000) but n8n_data volume at `/volume2/docker/secretary/n8n_data/` is owned by `fixhardez` (UID 1026:100). The `config` file had `-rw-------` permissions → n8n couldn't read/write its own config → crash loop with `EACCES: permission denied, open '/home/node/.n8n/config'`.
2. **Immediate fix**: `chmod 644` on the config file via SSH.
3. **Long-term fix**: Added `user: "1026:100"` to the n8n service in `docker-compose.yml`. This ensures n8n runs as `fixhardez:users`, matching the volume ownership. Future n8n updates won't cause permission issues because the container user always matches the volume owner.
4. **Second crash**: `user: "1026:100"` without passwd entry made HOME default to `/` → n8n tried `mkdir '/.n8n'`. Fix: added `HOME=/home/node` env to the n8n service.

## 2026-06-12 — Table cells with soft line-breaks lost during ingest (wuwa topup not retrievable)

### Problem
Bot kept answering "ไม่พบข้อมูล" for Wuthering Waves / wuwa topup template even though the
data clearly exists in Notion (page `Game Top up templates`, page_id `4ea87e92a4b3470cbeaba8469e469430`).
Bot returned the **page URL** (title matched) but never the row body (template steps + topup list).

### Root Cause
The page is a simple `table` block. Cells contain Notion **soft line-breaks** (shift+enter),
which encode as literal `\n` inside `rich_text[].plain_text`. Two ingest functions broke on that:
1. `_table_to_md` emitted each row as `"| c | c | c |"` — a cell with `\n` split the row across
   multiple physical lines → malformed markdown table.
2. `_split_table_to_rows` splits the section by `\n` and `continue`s on any line not starting
   with `|`, so every continuation line (the actual template/topup content) was dropped.
Net: only `"Game: Wuthering Waves / wuwa"` survived as the stored chunk — body gone. Proved with
a standalone repro before editing.

### Fix (`ingest/ingest.py`)
- `_table_to_md`: escape in-cell newlines → `.replace("\n", "<br>")` so each row stays one line.
- `_split_table_to_rows`: restore on parse → `.replace("<br>", "\n")` so chunk text stays readable.

### Deploy + verify
- `deploy.sh -s secretary` (rebuilds `secretary-ingest` image — `ingest.py` is `COPY`'d, not bind-mounted).
- Auto-started incremental ingest **skips** the page (unchanged `last_edited_time`); must force a
  targeted re-ingest: `docker compose run --rm secretary-ingest python ingest.py --page <id>`
  (`--page` ⇒ `full=True`, bypasses the skip, replaces stale chunks). Result: 3 chunks (Genshin /
  Honkai / Wuthering Waves rows).
- Verified via `POST /query` — full template + topup list now returned for wuwa.

## 2026-06-05 — Fix If2 type error in Secretary Bot workflow

### Problem
n8n execution 180 failed at **If2** node:
`Wrong type: 'AxiosError: timeout of 60000ms exceeded' is an object but was expecting a string [condition 0, item 0]`

### Root Cause
`HTTP Request1` (POST /query, `onError: "continueRegularOutput"`) puts `AxiosError` **object** into `$json.error` on timeout/failure.
If2 checked `$json.error` with operator `type: "string", operation: "notEmpty"`.
Even with `typeValidation: "loose"`, n8n v3 conditions cannot coerce an AxiosError object to string for notEmpty check → crash.

### Fix
Changed If2 `leftValue` from `={{ $json.error }}` → `={{ $json.error ? 'error' : '' }}`
Coerces any truthy value (object/string) to `'error'` before notEmpty string check. Works correctly for both normal responses (no error field) and error objects.

### Deployed
`./scripts/n8n_import.sh` → updated workflow id `syPPm4qxmVNENC9U`

---

## 2026-06-02 (evening) — Real fix for secretary-query OOM

### Symptoms
- n8n "Secretary Auto Sync" workflow ส่ง Telegram alert ทุกชั่วโมง:
  `500 - {"status":"error","summary":"...Loading weights: 100%|██████████| 391/391..."}`
- Subprocess `/ingest-trigger` ตายเงียบ (ไม่มี Python traceback) หลัง `qdrant points/delete` สำเร็จ

### Root cause (two layers)
1. **OOM kill ของ subprocess ใน secretary-query container** — dmesg ยืนยัน:
   `Memory cgroup out of memory: Killed process … (python) … anon-rss:3157172kB` ทุก ~1 ชม. ตรงกับ n8n Schedule Trigger
2. **The "fix" commit `bbbe6ac` (2026-06-02 บ่าย) เป็น no-op** — diff แทรก `deploy:` block ที่สอง **ก่อน** `depends_on:` ทำให้มี `deploy:` ซ้ำสองตัวที่ระดับเดียวกัน YAML กิน ตัวสุดท้าย (4G) ทับ. ต่อมา commit `b5405d4 update` มา "ล้าง" duplicate ด้วยการลบ block 6g ที่เพิ่งใส่เข้าไป — กลับสู่ 4G เหมือนเดิม. **secretary-query container ตอน OOM ยังเป็น container เดิมที่ถูกสร้าง 2026-06-01 23:14 + memory limit 4G** (`docker inspect ... HostConfig.Memory` = `4294967296`)
   - Daily log entry ก่อนหน้านี้เขียนว่า "container restart count = 2, RAM 1.5GB steady-state" — ที่จริงนั่นคือสภาพ **ระหว่าง crash loop เอง** ไม่ใช่ผลของ fix; verification อ่านผิด

### Real fix (this session)
- `secretary/docker-compose.yml:73-84` แก้ `memory: 4G` → `memory: 6G` (block เดียว ไม่มี duplicate)
- Deploy: `bash scripts/deploy.sh -s secretary -y`
- Verified post-deploy:
  - `docker inspect secretary-query --format '{{.HostConfig.Memory}}'` = `6442450944` (6 GiB ✓)
  - `POST /ingest-trigger` (ภายใน container) → `{"status":"done", ..., "Summary — pages: 155 | updated: 2 | skipped: 153 | chunks: 19 | errors: 0 | time: 115.3s"}` ใช้เวลา ~2 นาที ไม่ OOM, n8n auto-sync จะไม่ alert อีก

### Architecture note (load-bearing on 6G headroom)
`/ingest-trigger` ยังโหลด BGE-M3 ใน subprocess บน container ที่มี BGE-M3 resident อยู่แล้ว — ปลอดภัยเพราะ 6G ไม่ใช่เพราะ architecture ดี. Daily log 2026-06-01 ระบุชัดว่าเส้นทาง subprocess-in-query เป็น known limitation. ถ้าจะ refactor proper ในอนาคต: ให้ n8n เรียก `secretary-ingest` ผ่าน docker socket (one-shot container 6G dedicated) แทน

### Schedule observation (worth considering)
n8n auto-sync รันทุก 1 ชม. → จ่าย BGE-M3 cold load + Notion polling ทุกชั่วโมง สำหรับ updated pages 0–2 หน้า. ลดเป็น 4–6 ชม. ครั้งจะลด OOM exposure + CPU/network เกือบหมด

## 2026-06-02 (earlier — superseded by entry above)

### Attempted fix that landed as a no-op (kept for traceability)
- Commit `bbbe6ac` ตั้งใจเพิ่ม `deploy.resources.limits.memory: 6g` ใน secretary-query แต่ YAML duplicate-key bug ทำให้ไม่มีผล
- Commit `b5405d4 update` ตามมา ลบ block 6g ที่ duplicate ออก (แต่ลบผิดตัว) — กลับสู่ 4G
- Container ไม่เคยถูก recreate ด้วย 6G; OOM crash loop ดำเนินต่อจนกระทั่ง fix จริงด้านบน

## 2026-06-01

### Code Review of today's table-row chunking PR (commits 0555985…001e52f)
Reviewed 9 commits where hermes-agent (Mimo 2.5 Pro backend) rewrote `chunk_document()` to split Notion markdown tables into per-row chunks for better RAG retrieval. Verified findings (recall mode, 5 angles + verifier):

**CONFIRMED bugs (acceptable for now but worth tracking):**
- **Giant table row >500 tokens bypasses chunk size cap** (`ingest.py` ~528) — table branch in `chunk_document` skips the `_split_by_paragraph` 500-token guard. Single-row chunks with long Notes columns are not split. Low impact for credentials tables (short cells).
- **`keywords` and `category` payload fields are dead data** — `_extract_keywords` writes them in `upsert_chunks`, but `query/main.py` never filters on them. No payload index created in `ensure_collection()` either. Either drop them or actually use them in the query pipeline.
- **Pipe-in-cell breaks `_table_to_md` serializer** (`ingest.py:359-370`) — cells containing literal `|` are emitted unescaped, corrupting the markdown table downstream of the Notion serializer.
- **No timeout on `/ingest-trigger` subprocess** (`query/main.py:202` — `await proc.communicate()`) — this is the cause of the 150-second hang the user saw via the Hermes screenshot.
- **No SQLite write lock on `/ingest-trigger`** — concurrent requests race the state DB.
- **Degenerate breadcrumb when first table column is blank** — `primary_name` stays `""`, breadcrumb falls back to `Title > Heading` only.

**REFUTED candidates** (the change is actually safe):
- Merge-tiny-sections loop accidentally swallowing table rows — guarded by `is_table_row` check first.
- Stale Qdrant points after re-ingest — `delete_page_points(page_id)` is called before upsert.
- Mixed breadcrumb formats (table vs non-table) — breadcrumb is cosmetic, never parsed downstream.
- `subprocess env` losing PATH — `os.environ.copy()` preserves it.

**Suggestions for follow-up commits:**
1. Add `asyncio.wait_for(proc.communicate(), timeout=600)` to `/ingest-trigger` so the request returns a 504 instead of hanging forever.
2. Drop `keywords`/`category` writes OR wire them into the query filter to make them earn their storage cost.
3. Escape `|` → `\|` in `_table_to_md` cell text.
4. Add a single-row-too-large fallback inside `_split_table_to_rows` (paragraph-split the row if `_count_tokens(row_text) > 500`).

### Resource limits on docker-compose.yml
NAS hit 100% CPU during ingest, DSM became unreachable, 15-min load avg was 85. `secretary-query` was holding 5.5 GB at idle (model + leftover ingest subprocess).

First attempt added `cpus: N.M` to all 4 services. **Deploy failed with `NanoCPUs can not be set, as your kernel does not support CPU CFS scheduler`** — Synology DSM kernel ships without CFS quota support, so docker-level CPU limits are simply unavailable on this NAS. Removed all `cpus:` keys.

Working config:
- `deploy.resources.limits.memory` on every service — cgroup-enforced.
- `OMP_NUM_THREADS` / `MKL_NUM_THREADS` / `TOKENIZERS_PARALLELISM=false` on `secretary-query` (=2) and `secretary-ingest` (=3) — PyTorch + FlagEmbedding obey these, so even without docker cpus the model uses at most N threads.
- `logging.options.max-size=10m max-file=3` for all services (was unbounded).

Memory allotment (NAS = 12 GB total):
- qdrant: 1.5 GB
- n8n: 1 GB
- secretary-query: **6 GB** *(bumped from 4 GB on 2026-06-02 after repeated OOM kills)*
- secretary-ingest: 6 GB *(bumped from 4 GB after OOM kill on the User-Password page — FlagEmbedding's batch encode of 20–50 chunks spikes RAM transiently past the resident ~2 GB model footprint.)*

### Known limitation — `/ingest-trigger` OOMs on table-heavy pages
The `/ingest-trigger` endpoint in `secretary-query` spawns `ingest.py` as a subprocess **inside the query container**. The subprocess loads its own BGE-M3 (~2 GB) on top of the parent's already-resident BGE-M3 (~2 GB), so the query container's 4 GB ceiling is exhausted before chunking even starts on a page with many table rows. **Workaround:** run page-targeted re-ingests in the dedicated container instead:
```bash
docker compose run --rm secretary-ingest python ingest.py --page <NOTION_PAGE_ID>
```
This was the path used to backfill the User-Password page. The standalone container has its own 6 GB budget and doesn't compete with secretary-query's resident model.

### End-to-end verification
- Bumped local main to origin/main (was 10 commits behind — first deploy uploaded the OLD `query/main.py` without the `page_id` parameter, which is why `/ingest-trigger?page_id=…` seemed to "ignore" the filter and silently ran a full incremental skip across all 155 pages).
- Removed orphan `secretary-ollama` container (compose dropped the service 2026-05-29 but the container was never `docker rm`'d).
- `docker compose run --rm secretary-ingest python ingest.py --page 5edc5884-7666-4bdd-b758-86cc24c95f0a` → `Updated: User-Password (29 chunks)` in 379 s.
- Qdrant point count for that `page_id`: **29** (was 0 after my earlier failed runs left the page deleted-but-not-upserted).
- `POST /query {"question": "ขอ user pass discord"}` → returns `User: fixkychicky@gmail.com, Password: REDACTED@1` with breadcrumb `User-Password > Website > Discord`. The per-row chunking is working as intended — Hermes's diagnosis was correct, just never successfully applied because every previous re-ingest attempt died on memory or timeout.

## 2026-05-28 (session 2)

### งานที่ทำ
- Diagnosed RAG inconsistency: ปัญหาหลักคือ retrieval ไม่ใช่ system prompt — `top_k_final=3` ตัด chunks ที่ถูกต้องทิ้ง + Thai query vs English content semantic gap
- Fix 1: เพิ่ม `top_k_final` default จาก 3 → 6 ใน `query/main.py:QueryRequest`
- Fix 2: ปรับ SYSTEM_PROMPT — เพิ่ม rule "ถ้า partial context มีอยู่ให้รายงานที่เจอแทนการบอกว่าไม่พบ" เพื่อลด false "ไม่พบข้อมูล"
- สร้าง `secretary/README.md` (ไม่เคยมีมาก่อน) — ครอบคลุม quickstart, services, volumes, env files, LLM providers, API endpoints, ingest commands
- อัปเดต root `README.md`: ลบ `my-secretary/` (ถูกลบออกจาก project แล้ว), เพิ่ม `secretary/` row, อัปเดต Reverse Proxy, env vars, Architecture Notes

### Code Cleanup (session 3)
- Cohere client: ย้ายจาก per-request → `app.state.cohere` init ครั้งเดียวใน lifespan
- `/ingest-trigger`: เปลี่ยน error response จาก HTTP 200 → HTTP 500
- Nous client (`llm_client.py`): cache ตาม token string แทน per-call construction
- Extract `_text_from_openai()` helper — ลบ openrouter/nous duplication
- `asyncio.Lock` ใน `NousTokenManager.get_access_token` — กัน concurrent refresh stampede
- `_TERMINAL_OAUTH_ERRORS` constant + stop polling on terminal errors (access_denied ฯลฯ)
- Deployed และ health check ผ่าน: `{"status":"ok","qdrant_ok":true,"collection_stats":{"points_count":345}}`

## 2026-05-27

### งานที่ทำ
- Restructured secretary stack from single `ingest.py` monolith to multi-service architecture
- Created `secretary/ingest/` service: ingest.py (Notion→Qdrant), Dockerfile (CPU-only torch), requirements.txt, .env.example, README.md
- Created `secretary/query/` service: main.py (FastAPI RAG :5065), llm_client.py (Anthropic/OpenRouter/Norus), Dockerfile, .env.example, README.md
- Updated `docker-compose.yml`: qdrant, ollama, n8n (15678), secretary-query (15065), secretary-ingest (one-shot)
- Added root `secretary/.env.example` for n8n credentials
- Added `secretary/` row to CLAUDE.md stacks table (ports 15065, 15678)
- Created `.notes/00_INDEX.md` with full architecture/volume/env documentation

### Architecture Changes
- Embedding: BGE-M3 via FlagEmbedding (CPU-only, shared `hf_cache` volume)
- Hybrid search: dense (1024d Cosine) + sparse vectors, RRF fusion
- LLM: switchable via `LLM_PROVIDER` (anthropic default)
- Reranking: optional Cohere `rerank-multilingual-v3.0`
- Ingest state: SQLite at `/data/ingest_state.db` for incremental sync

### Next Steps
1. Deploy to NAS: `/deploy`
2. Create NAS directories: `mkdir -p /volume2/docker/secretary/{qdrant_storage,ollama_data,n8n_data,ingest_state,hf_cache,ingest,query}`
3. Copy `.env.example` → `.env` for root, ingest/, query/ and fill real values
4. Run: `docker compose up -d qdrant ollama n8n && docker compose up -d --build`
5. First ingest: `docker compose run --rm secretary-ingest python ingest.py --full`
6. Set up n8n workflows (Phase 6 of checklist)

## 2026-05-28

### งานที่ทำ
- Review & validate secretary-query service against spec
- Fixed two gaps: removed unused `PORT` var from `.env.example`; added blocking-timeout note to README
- Added pytest suite: 9 tests covering llm_client (4 providers) + main endpoints (/health ok/down, /query hybrid, /query top_k_final slice, /query hybrid+rerank)
- Key discovery: `ASGITransport` doesn't fire ASGI lifespan — fixed by direct assignment of `main.qdrant` and `main.app.state.model` in conftest.py
- Written design spec: `docs/superpowers/specs/2026-05-28-secretary-query-design.md`
- Committed all query service files (12 files, commit adb366a)

### Next Steps
1. Deploy to NAS: `/deploy`
2. On NAS: `cp secretary/query/.env.example secretary/query/.env` and fill real keys
3. `docker compose up -d --build secretary-query`
4. Smoke test: `curl http://<NAS_HOST>:15065/health`
5. First ingest: `docker compose run --rm secretary-ingest`

## 2026-05-28

### Fix /ingest-trigger (POST /ingest-trigger → python /ingest/ingest.py not found)
- **Root cause:** `/ingest-trigger` in `query/main.py:185` runs `python /ingest/ingest.py` as subprocess inside `secretary-query` container, but `ingest.py` was moved to `secretary/ingest/` subdirectory as a separate service — file never copied into query container
- **Fix 1:** `docker-compose.yml` — added `./ingest/ingest.py:/ingest/ingest.py:ro` bind mount + `./ingest/.env` env_file to `secretary-query` service (ingest env vars needed by subprocess)
- **Fix 2:** `query/requirements.txt` — added `notion-client>=2.2.0`, `tiktoken>=0.7.0`, `tenacity>=8.2.0` (required by ingest.py but missing from query container)
- **Note:** State DB (`STATE_DB=/data/ingest_state.db`) when triggered via `/ingest-trigger` writes to query-data volume (`/volume2/docker/secretary/query-data/`), separate from `ingest_state` volume used by standalone `secretary-ingest` service — incremental sync works independently per trigger method

### Nous Portal OAuth integration
- Removed `norus` provider from llm_client.py, main.py, .env.example, and tests
- Created `nous_auth.py` (NousTokenManager) — handles OAuth 2.0 Device Code flow, token persistence to /data/nous_token.json, auto-refresh with 60s buffer
- `_poll_for_token` retries on network errors and unexpected status codes (logs warning, continues)
- Added `GET /nous/auth` endpoint — starts device flow, returns {verification_uri, user_code, expires_in, message}; returns 503 if Nous Portal unreachable
- Added `GET /nous/auth/status` endpoint — returns {authenticated: bool, expires_at: int|null}
- Added `nous` provider block in llm_client.py — creates fresh AsyncOpenAI client per call (Bearer token from nous_auth), inference URL: https://inference-api.nousresearch.com/v1
- OAuth endpoints: portal.nousresearch.com/api/oauth/device/code + /token, client_id=hermes-cli
- Token stored in /data/nous_token.json (atomic write), NOUS_TOKEN_FILE env var for override
- Setup: deploy → GET /nous/auth → open verification_uri in browser → enter user_code → approve → token auto-saved

## 2026-05-29

### งานที่ทำ
- ลบ `ollama` service และ `ollama_data` volume ออกจาก `docker-compose.yml` — ไม่เคยมีโค้ดส่วนไหน connect มันเลย ทั้ง ingest ใช้ FlagEmbedding โดยตรง และ query ใช้ external LLM APIs
- อัปเดต `query/.env.example`: เปลี่ยน default `LLM_PROVIDER=openrouter`, `OPENROUTER_MODEL=google/gemini-2.5-flash`
- `query/.env` จริง: user เลือกใช้ `deepseek/deepseek-v4-flash` (ถูกกว่า Gemini Flash)

---

## 2026-05-28 — Fix: filter sources to cited-only

**Problem:** `/query` endpoint returned all top_k_final=6 sources, n8n displayed all as references even when LLM only used 1.

**Root cause:** `sources` was built from all `hits` unconditionally.

**Fix:** After LLM answer, parse citation numbers with `re.findall(r"\[(\d+)\]", answer)`, filter `hits` to only cited indices before building `sources` array (`main.py:147-156`).

**Tests:** Updated 3 test mocks to include `[1]` in LLM return values. Pre-existing `test_query_rerank_path` failure (KeyError 'cohere' in app.state) unchanged.

## 2026-05-31

### Phase B: NORUS → NOUS manifest fix
- **Bug:** `secretary/query/secrets.manifest.yaml` ใช้ `NORUS_API_KEY`, `NORUS_BASE_URL`, `NORUS_MODEL` (มี R) แต่โค้ดจริง (`llm_client.py`, `main.py`) ใช้ `NOUS_MODEL` (ไม่มี R) และใช้ OAuth device code flow ไม่ได้ใช้ API key
- **Fix:** ลบ `NORUS_API_KEY` และ `NORUS_BASE_URL` จาก manifest `env:` (dead vars), เปลี่ยน `NORUS_MODEL` → `NOUS_MODEL` ใน manifest `literals:`
- ผล: `make secrets` + `make test` (43 tests) ผ่าน, `secretary/query/.env` ไม่มี NORUS vars อีก, มี `NOUS_MODEL=xxx` ถูกต้อง

## 2026-06-05

### งานที่ทำ
- สร้าง n8n workflow `Secretary Error Alerter` — Error Trigger → Telegram alert ที่ chat `8663614341`
- Set เป็น global Error Workflow ใน n8n Settings → General → Error Workflow
- ทดสอบด้วย `_Test Error` workflow → ได้รับ alert ใน Telegram ✅
- Fix `scripts/n8n_import.sh`: create-path ส่งเฉพาะ minimal fields ที่ POST API ยอมรับ (ไม่ส่ง read-only fields เช่น `active`, `meta`)

### Architecture Change
- n8n Error Workflow ครอบทุก workflow อัตโนมัติ (Secretary Auto Sync, Secretary Bot, และ future workflows)
- Credential ใช้ "Secretary Bot" telegramApi ตัวเดิม (id: QiUZ8rINLwwPL1qu)

## 2026-06-23

### Bug: secretary-n8n restart loop
- **Bug:** `secretary-n8n` crash-loop ต่อเนื่อง (RestartCount 29+), error `EACCES: permission denied, open '/home/node/.n8n/config'`
- **Root cause:** Container mount จริงคือ Docker named volume `secretary_n8n_data` → `/volume2/@docker/volumes/secretary_n8n_data/_data` (ไม่ใช่ `/volume2/docker/secretary/n8n_data` ตามที่ compose `device:` ระบุ — path นั้นเป็น dir เก่าที่ไม่ได้ใช้แล้ว). Ownership ของ `_data` กลายเป็นไม่ตรง uid container ทำให้ container user `node` (uid 1000:1000) เขียน config/database ไม่ได้
- **Fix:** `chown -R 1000:1000 /volume2/@docker/volumes/secretary_n8n_data/_data` แล้ว `docker restart secretary-n8n` → ขึ้นปกติ, RestartCount reset เป็น 0
- **Note:** `/volume2/docker/secretary/n8n_data` ใน compose `volumes.n8n_data.driver_opts.device` เป็น path ที่ไม่ได้ map จริง (Docker ใช้ named volume ปกติแทน bind mount) — ควรเช็คว่า compose `device:` config ตรงกับ volume name mapping จริงหรือไม่ในรอบ deploy ถัดไป

## 2026-09-08

### Incident: host-wide OOM จาก `secretary-ingest` → QuickConnect ล่ม
- **อาการที่ผู้ใช้เจอ:** เข้า `https://<QC_ID>.quickconnect.to/` ไม่ได้ เด้งไป `/portal/error.html?error=6` ("การเชื่อมต่อล้มเหลวจากปัญหาเครือข่าย") ทั้งที่ container ทุกตัวดูปกติ
- **Root cause:** 14:15:13 (+07) เกิด global OOM ของ host — `oom-kill:constraint=CONSTRAINT_NONE ... global_oom, task_memcg=/docker/ea61abdd49af...` = `secretary-ingest` โดน kill ที่ `anon-rss 4,084,552 kB` (total-vm 7.1 GB) ระหว่างที่ DSM `synofoto-face-extraction` กำลังรัน index ใบหน้าอยู่พอดี
- **ทำไมกระทบ QuickConnect:** OOM ระดับ host ทำให้ `synorelayd` (pid 19268) ตายไปด้วย → relay tunnel ไป `synr-sg4.<ID>.direct.quickconnect.to` หายทั้งตัว. DSM restart daemon เองเป็น pid 24645 เวลา 14:59 (`synorelayd.cpp:2739 daemon stopped` → `2695 daemon started` → `2102 Tunnel ready, add trust proxy`)
- **สถานะหลัง restart (ตรวจแล้ว):** relay ตอบ `pingpong.cgi` 200 (`{"success":true,...}`) 5/5 ครั้ง, `/webman/login.cgi` ผ่าน relay ตอบ 200, DSM listen 5000/5001 ครบ, container รัน 29 ตัว, `secretary-query`/`secretary-qdrant`/`my-secretary` healthy
- **`secretary-ingest` Exited (137)** = โดน OOM kill ไม่ใช่ crash loop (service นี้เป็น `restart: "no"` one-shot อยู่แล้ว)
- **แก้แล้ว:** `secretary-ingest` **มี `limits.memory` อยู่แล้ว = 6G** (ที่รายงานตอนแรกว่า "ไม่มี limit" ผิด) แต่เพดานครึ่งเครื่องบนเครื่อง 12 GB = host ตายก่อน cgroup จะทำงาน — แพตเทิร์นเดียวกับ `secretary-query` ตอน 19/08 → ลดเป็น **4G** เท่ากับ query
- **Trade-off ที่รับไว้:** คอมเมนต์เดิมในไฟล์บันทึกว่า 2026-06-01 เพดาน 4 GB เคย OOM-kill container นี้มาแล้ว ถ้าเกิดซ้ำให้ลด batch size ของ `upsert_chunks` ห้ามดันกลับ 6G — ingest ตายรันใหม่ได้ host ตายไม่ได้
- **หมายเหตุ:** ฟิลด์ `"pingpong":"DISCONNECTED"` ใน `get_server_info` ของ QuickConnect ไม่ใช่ตัวบอกอาการ — มันคือผลตรวจเส้น direct (port forward) ซึ่ง router forward แค่ 443 + 15xxx ไม่เคย forward 5001 (`ext_port: 0`) จึงขึ้น DISCONNECTED ทั้งตอนพังและตอนปกติ ให้ดูที่ relay pingpong แทน
