# n8n — Daily Log

## 2026-09-08 (รอบหก) — import workflow ที่แก้ URL แล้ว (ขั้นสุดท้ายที่ค้าง)

webhook ผ่านแล้ว บอทรับข้อความได้ แต่ node `HTTP Request 1` พัง:

```
getaddrinfo ENOTFOUND secretary-query   (code: ENOTFOUND)
```

= workflow ใน instance ยังถือ URL เก่า `http://secretary-query:5065` อยู่ —
แก้ไว้ในไฟล์ repo แล้วแต่ยังไม่เคย `n8n_import.sh` (ขั้น 8 ใน checklist)
ชื่อนั้น resolve ได้เฉพาะใน compose network ของ secretary ซึ่ง n8n ออกมาแล้ว

**เช็คก่อน import ว่าจะไม่ทับของที่ drift:** ดึง workflow ทั้ง 3 จาก API มา
เทียบกับไฟล์ใน repo แบบ normalise URL ก่อน (`name`/`nodes`/`connections`)
→ **ตรงกันทั้ง 3 อัน** ต่างแค่ URL อย่างเดียว = ทับได้ปลอดภัย
(ถ้าไม่ตรงต้องไป PUT ทีละ node แทน ไม่ใช่ import ทับ)

`./scripts/n8n_import.sh` → 0 created, 3 updated

**ยืนยันหลัง import**
- URL ใน instance: `host.docker.internal:5065` 4 จุด (Bot) + 2 จุด (Auto Sync),
  ไม่เหลือ `secretary-query` เลย
- workflow ยัง active ครบ 3 (PUT ไม่ได้ปิดการทำงาน)
- webhook ยังตอบ `"not registered for GET requests"` = ยังลงทะเบียนกับ Telegram อยู่
- log ไม่มี `bad webhook` / `did fail`

## 2026-09-08 (รอบห้า) — ให้ webhook วิ่งผ่าน homepage-nginx บน 443 แทน (แก้ได้ ยืนยันแล้ว)

DSM UI เข้าไม่ได้ (SSH ยังเข้าได้) เลยแก้แบบไม่แตะ DSM เลย

**ทำไมไม่ไปแก้ RP เอง:** RP อยู่ที่ `/usr/syno/etc/www/ReverseProxy.json` +
nginx ที่ gen ออกมาที่ `/usr/local/etc/nginx/sites-available/<uuid>.w3conf` +
tls-profile แยกอีกไฟล์ — แก้มือได้แต่พลาดทีเดียว **443 ล่มทั้งเครื่อง**
(homepage, query, ทุกอันบน 443) แล้วไม่มี DSM UI ให้กู้ ความเสี่ยงไม่คุ้ม

**สิ่งที่เห็นใน `ReverseProxy.json`:** entry `N8N` (uuid `8cd8c645...`) ถูก
**แก้ทับ** จาก subdomain:443 เป็น `fixhardez.synology.me:15678` — ของเดิมหายไปเลย
ไม่ได้เพิ่ม entry ใหม่. อีกอย่างที่เจอ: `Secretary Query` ใช้
`query.fixhardez.synology.me:443` = DSM ทำ hostname-based routing บน 443 อยู่แล้ว

**ทางแก้ที่ใช้:** 443 มี homepage ถืออยู่ ซึ่ง nginx ตัวนั้น**เป็นของเราเอง**
เลยเพิ่ม location ใน `homepage/nginx/nginx.conf` ส่ง `/webhook/`,
`/webhook-test/`, `/webhook-waiting/` ต่อไปที่ `host.docker.internal:5678`
(prefix location ชนะ `location /` เลยไม่ติด basic auth) แล้วตั้ง
`N8N_WEBHOOK_URL=https://fixhardez.synology.me` (**ไม่มี path ต่อท้าย** n8n เติม
`/webhook/<id>/webhook` เอง ใส่ path ไปจะกลายเป็น `/webhook/webhook/...`)

- `homepage/nginx/n8n_webhook.conf` แยกไฟล์ mount เข้า container เพราะ 3
  location ใช้ชุด proxy_set_header เดียวกัน
- `extra_hosts: host-gateway` ต้องใส่ที่ service `nginx` ของ homepage ด้วย
  (คนละ compose project กับ n8n ชื่อ container resolve ข้ามไม่ได้)
- `N8N_PROXY_HOPS` 1 → **2** เพราะตอนนี้ผ่าน DSM nginx แล้วต่อ homepage-nginx
  ทั้งคู่ append `X-Forwarded-For`
- vault `webhook_url` → `https://fixhardez.synology.me`, `editor_url` ยัง
  `:15678` เหมือนเดิม (entry นั้นยังอยู่ ยังเข้า dashboard ได้)

**ยืนยันแล้ว (deploy `-s homepage,n8n` แล้ว)**

| เช็ค | ผล |
|---|---|
| `curl localhost:5678/webhook/<id>/webhook` | n8n JSON 404 |
| `curl localhost:3000/webhook/<id>/webhook` | n8n JSON 404 (ผ่าน homepage-nginx) |
| `curl https://<domain>/webhook/<id>/webhook` จาก WAN | **`{"code":404,"message":"This webhook is not registered for GET requests. Did you mean to make a POST request?"}`** |
| `https://<domain>/` | 401 (basic auth ของ homepage ยังทำงาน ไม่โดน bypass) |
| log `bad webhook` / `did fail` | **0 ครั้ง** |
| `Activated workflow` | ครบ 3 |

ข้อความ 404 เปลี่ยนจาก `"The requested webhook ... is not registered"` เป็น
`"not registered for GET requests. Did you mean POST?"` = **n8n รู้จัก path นี้
สำหรับ POST แล้ว** แปลว่า `setWebhook` ผ่าน Telegram รับ URL ใหม่เรียบร้อย

**ผลข้างเคียงที่ต้องรู้:** homepage-nginx กลายเป็นทางเข้าของ webhook n8n —
**homepage ล่ม = บอทไม่ได้รับข้อความ**. ถ้าจะตัด coupling นี้ ต้อง forward พอร์ต
**8443** ที่ router แล้วทำ RP `<domain>:8443` → `localhost:5678` ชี้ทั้งสองตัวแปร
ไปที่นั่น แล้วลบ location block ออกจาก homepage nginx

## 2026-09-08 (รอบสี่) — สาเหตุจริงที่บอทเงียบ: Telegram ไม่รับ webhook บนพอร์ต 15678

deploy รอบสองแล้วทักบอทยังเงียบ ไล่ log เจอของจริง:

```
400 - {"ok":false,"error_code":400,"description":"Bad Request: bad webhook:
Webhook can be set up only on ports 80, 88, 443 or 8443"}
Activation of workflow "Secretary Bot" did fail | retry in 4 / 8 / 16 / 32 / 64 seconds
```

**Telegram ยอมรับ webhook เฉพาะพอร์ต 80 / 88 / 443 / 8443** — `:15678` ลงทะเบียน
ไม่ได้ตั้งแต่แรก ไม่เกี่ยวกับ env, ownership, หรือ host-gateway เลย ที่ subdomain
เดิมใช้ได้ก็เพราะบังเอิญอยู่บน 443 พอดี

**ทำไมรอบก่อนหน้าหาไม่เจอ:** ตอนนั้นดู log ด้วย `grep "editor is|activated"` ซึ่ง
match บรรทัด `Try to activate` กับ `Editor is now accessible` ที่ดูปกติดี ส่วน
บรรทัด 400 อยู่คนละท่อน ต้อง grep `error|fail|bad request` ถึงโผล่ —
`Activated workflow` (สำเร็จ) กับ `Try to activate workflow` (กำลังลอง แล้วพัง)
ต่างกันแค่คำเดียว

**พอร์ตที่ router forward จริง (วัดจาก WAN):** 443 → 401, 15678 → 200,
**8443 / 88 / 80 → timeout** = ไม่ได้ forward. เหลือ 443 ทางเดียวที่ใช้ได้โดย
ไม่ต้องแตะ router

**ทางแก้ที่เลือก — แยก editor กับ webhook คนละ RP entry:**

| ตัวแปร | ค่า | ใช้ทำอะไร |
|---|---|---|
| `N8N_EDITOR_BASE_URL` | `https://<domain>:15678` | dashboard/UI ที่คนเปิด (ตามที่ตั้งใจย้ายมา) |
| `N8N_WEBHOOK_URL` | `https://n8n.<domain>` (443) | ที่ Telegram ยิงเข้ามา — **ต้องอยู่บนพอร์ตที่ Telegram ยอม** |

vault: `stacks.secretary.n8n.webhook_url` กลับไปเป็น subdomain,
เพิ่มคีย์ใหม่ `stacks.secretary.n8n.editor_url`.
`stacks.homepage.var_n8n_https` **คงเป็น `:15678`** เพราะเป็นลิงก์ให้คนกด

**ยังไม่ deploy — ต้องเอา DSM RP entry ของ subdomain กลับมาก่อน** ไม่งั้น
`setWebhook` สำเร็จแต่ Telegram ยิงไปโดน DSM portal เหมือนเดิม

**ทางเลือกสำรอง** ถ้าอยากได้ hostname เดียวจริงๆ: forward พอร์ต **8443** ที่
router แล้วตั้ง RP `<domain>:8443` → `localhost:5678` แล้วชี้ทั้งสองตัวแปรไปที่นั่น
— ตัดเรื่อง subdomain ทิ้งได้เลย แต่ต้องแตะ router

## 2026-09-08 (รอบสาม) — cutover เสร็จ, บอทเงียบ: หาเจอว่า container ถือ WEBHOOK_URL เก่า

**อาการ:** ย้าย stack + ย้าย data เสร็จ n8n UI เข้าได้ workflow/credential มาครบ
แต่ทักบอท Telegram แล้วเงียบ ไม่มี execution เข้า ไม่มี error ที่ไหนเลย

**ที่วัดได้ (บน NAS)**

| จุด | ค่า |
|---|---|
| `/volume2/docker/n8n/.env` | `N8N_WEBHOOK_URL=https://<domain>:15678` ✅ ถูกแล้ว |
| `docker exec n8n printenv` | `WEBHOOK_URL=https://n8n.<domain>:443` ❌ **ค่าเก่า** |
| log ตอน start | `Editor is now accessible via: https://n8n.<domain>:443` |
| `https://<domain>:15678/rest/settings` | 200 = n8n จริง |
| `https://n8n.<domain>/` | 200 แต่ body 494 ไบต์เป็นหน้า **DSM portal** ไม่ใช่ n8n |

**สาเหตุ:** container ถูกสร้างก่อนที่ `.env` ตัวใหม่จะขึ้นไป (ไฟล์ 06:51,
container start ~06:45) — compose อ่าน `.env` **ตอนสร้าง container เท่านั้น**
n8n เลย activate workflow ทั้ง 3 อันด้วย URL เก่า แล้วสั่ง Telegram
`setWebhook` ไปที่ subdomain เดิม ซึ่งตอนนี้ RP ไม่ได้ชี้มาที่ n8n แล้ว
Telegram ยิงไปโดน DSM portal ตอบ 200 กลับ = **Telegram ถือว่าส่งสำเร็จ
ไม่ retry ไม่มี error** บอทเลยเงียบสนิททั้งสองฝั่ง ตรงกับกับดักที่จดไว้เป๊ะ

**ที่ทำ:** เพิ่ม `N8N_PROXY_HOPS=1` ใน compose (log ขึ้น
`ERR_ERL_UNEXPECTED_X_FORWARDED_FOR` — อยู่หลัง RP แต่ express ไม่ trust proxy
ทำให้ rate-limit นับรวมทุกคนเป็น IP เดียวกัน) แล้ว `./scripts/deploy.sh -s n8n -y`
→ container ถูก recreate สำเร็จ (deploy จบ 80 วิ)

**NAS ล่มระหว่างนั้น — คนละเรื่องกับบอทเงียบ:** หลัง deploy ไม่กี่นาที NAS ไม่ตอบ
จาก WAN ทั้งเครื่อง (2222/443/15678 TCP SYN ผ่าน แต่ไม่มี application data,
sshd ไม่ส่ง banner) ~4 ชม. กลับมาเอง 15:12. สาเหตุ: **host OOM/thrash** —
ตอนกลับมา load average 272/353/234, swap เต็ม 1991/2047 MB,
`secretary-ingest` `ExitCode=137 OOMKilled=false` = โดน **host OOM killer**
ไม่ใช่ cgroup limit ของตัวเอง (ซ้ำรอย 19/08). n8n ไม่ได้โดนด้วย
(`RestartCount=0`, `OOMKilled=false`)

**ยืนยันหลังเครื่องกลับมา**

| เช็ค | ผล |
|---|---|
| `docker exec n8n printenv WEBHOOK_URL` | `https://fixhardez.synology.me:15678` ✅ |
| `N8N_PROXY_HOPS` | `1` ✅ |
| log ตอน start 07:00 | `Editor is now accessible via: https://<domain>:15678` ✅ |
| workflow active | ครบ 3 อัน, `Secretary Bot` re-activate ตอน start = สั่ง `setWebhook` ใหม่ด้วย URL ใหม่ |
| GET `https://<domain>:15678/webhook/<id>/webhook` | 404 **จาก n8n เอง** (JSON `"is not registered"`) = RP ยิงถึง webhook handler จริง, GET 404 เป็นคำตอบที่ถูกเพราะ Telegram trigger ลงทะเบียนเฉพาะ POST |
| executions ของ `Secretary Bot` | ยังว่าง — รอคนทักบอทถึงจะรู้ว่า Telegram ยอมรับ URL ใหม่แล้วจริง |

**เจอเพิ่ม:** n8n 2.x เตือน `WEBHOOK_URL -> Use N8N_WEBHOOK_URL instead` —
ชื่อไม่มี prefix deprecated แล้วและตั้งได้แค่ production URL ไม่รวม test URL
แก้ใน compose แล้ว (`N8N_WEBHOOK_URL`) **ยังไม่ deploy** ตั้งใจ ไม่อยากรีสตาร์ต
ซ้ำตอนเครื่องเพิ่งฟื้น ค่าเดิมยังทำงานได้ ไป deploy รอบหน้าพร้อมของอื่น

**เช็คอื่นที่ผ่านแล้ว**
- `host.docker.internal` ใช้ได้จริง — `node -e fetch(...:5065/health)` ได้ **HTTP 200**
  (`getent` ไม่มีใน image ใช้เช็คไม่ได้ ต้องใช้ `node -e`)
- ownership ใน `n8n_data` เป็น 1000:1000 ครบแล้ว (EACCES ใน log เป็นของช่วง
  ระหว่างย้าย ไม่ใช่ปัญหาค้าง)

## 2026-09-08 (รอบสอง) — DSM reverse proxy ย้ายจาก subdomain มาเป็น port

RP เปลี่ยนจาก `n8n.<domain>` port 443 → `<domain>` port **15678**

- vault: `stacks.secretary.n8n.webhook_url` และ `stacks.homepage.var_n8n_https`
  → `https://<domain>:15678` (แก้ด้วย `sops set` ไม่ต้องเปิด editor)
- `make secrets` แล้ว — `n8n/.env` กับ `homepage/.env` อัปเดตแล้ว
- `HOMEPAGE_VAR_N8N_HTTP` ไม่แตะ (`http://<lan-ip>:5678` ยิงตรงใน LAN)
- homepage `services.yaml` description "Workflow Automation (Secretary Stack)"
  → "(shared)" เพราะไม่ได้อยู่ใต้ secretary แล้ว
- ไม่มีไฟล์ไหนใน repo hardcode hostname ไว้ (ทุกที่ผ่าน vault) เลยไม่ต้องไล่แก้อีก

**⚠️ ต้องปิด-เปิด workflow ที่มี webhook trigger ใหม่หลัง deploy** — n8n บอก
Telegram ว่าให้ยิงมาที่ไหนเฉพาะตอน activate workflow เปลี่ยน env เฉยๆ ไม่ย้าย
webhook ที่ลงทะเบียนไว้แล้ว ใส่เป็นขั้น 9 ใน cutover checklist แล้ว

## 2026-09-08 — แยก n8n ออกจาก secretary เป็น stack ของตัวเอง

**เป้าหมาย:** ให้ stack อื่นใช้ n8n ร่วมกันได้ ไม่ผูกกับ lifecycle ของ secretary

**ที่ทำ**

- สร้าง `n8n/docker-compose.yml` ย้าย service block + volume def มาจาก
  `secretary/docker-compose.yml` (ตัดออกจากของเดิมแล้ว)
- container `secretary-n8n` → `n8n`
- `n8n_data` ย้ายจาก `/volume2/docker/secretary/n8n_data` →
  `/volume2/docker/n8n/n8n_data` (rename ภายใน volume2 = atomic ไม่ copy
  ทำตอน n8n stop อยู่แล้วในขั้น cutover) เพื่อไม่ให้ project ของ n8n ขึ้นกับ
  โฟลเดอร์ project ของ secretary — **`ls` ต้องไม่ว่างก่อน start** ไม่งั้น n8n
  สร้าง encryption key ใหม่ credentials เก่าตายถาวร. `deploy.sh` chown
  1000:1000 ตาม path ใหม่แล้ว
- เพิ่ม `extra_hosts: host.docker.internal:host-gateway` แล้วแก้ URL ใน
  workflow JSON จาก `http://secretary-query:5065` → `http://host.docker.internal:5065`
  (เลือกวิธีนี้แทน shared network เพราะทุก stack publish port บน host อยู่แล้ว
  ไม่ต้องให้ stack ปลายทาง opt-in และไม่ต้องใส่ IP จริงลง repo)
- ย้าย `secretary/n8n-workflows/` → `n8n/workflows/`
- สร้าง `n8n/secrets.manifest.yaml` (vault path คงเดิม `stacks.secretary.n8n.*`),
  ลบ `secretary/secrets.manifest.yaml` ทิ้ง — หลังตัด n8n ออก secretary ไม่มี
  `${...}` เหลือใน compose แล้ว (ingest/query มี manifest ของตัวเองอยู่แล้ว)
- `scripts/deploy.sh`: เพิ่ม `n8n` ใน `ALL_STACKS`
- `scripts/n8n_export.sh` / `n8n_import.sh`: อ่าน key จาก `n8n/.env`,
  workflow dir → `n8n/workflows`
- อัปเดต `secretary/README.md`, root `README.md`, root `CLAUDE.md`

**ยังไม่ทำ (รอ user)**

- ยังไม่ `make secrets` / ยังไม่ deploy — user ต้องสร้าง project ใน DSM
  Container Manager เองก่อน
