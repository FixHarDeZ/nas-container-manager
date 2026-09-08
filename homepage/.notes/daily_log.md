# Daily Log — homepage

---

## 2026-09-08 (รอบสอง) — homepage-nginx รับ webhook ของ n8n ต่อให้

Telegram รับ webhook เฉพาะพอร์ต 80/88/443/8443 และ router forward แค่ 443
(กับ 15xxx) ซึ่ง 443 stack นี้ถืออยู่ — nginx ตัวนี้เป็นของเราเองเลยเอามาใช้เป็น
ทางเข้า webhook ให้ n8n แทนการไปเพิ่ม DSM RP entry (ตอนนั้น DSM UI เข้าไม่ได้)

- `nginx/nginx.conf` เพิ่ม 3 prefix location: `/webhook/`, `/webhook-test/`,
  `/webhook-waiting/` → `host.docker.internal:5678`
  **prefix location ชนะ `location /`** เลยไม่ติด basic auth (ตั้งใจ — webhook
  ต้องให้ Telegram ยิงได้ และ path มี UUID ของ workflow กันอยู่แล้ว
  n8n เองก็ไม่เคยเอา basic auth ครอบ path พวกนี้)
- `nginx/n8n_webhook.conf` ไฟล์แยก mount `:ro` — 3 location ใช้ proxy header
  ชุดเดียวกัน
- service `nginx` เพิ่ม `extra_hosts: host.docker.internal:host-gateway`
  (n8n อยู่คนละ compose project ชื่อ container resolve ข้ามไม่ได้)
- ยืนยัน: `https://<domain>/` ยัง 401 เหมือนเดิม, `/webhook/<id>/webhook`
  ได้ JSON ของ n8n กลับมา

**coupling ใหม่: homepage ล่ม = บอท n8n ไม่ได้รับข้อความ** ตัดได้ด้วยการ
forward 8443 ที่ router แล้วให้ n8n มี RP entry ของตัวเอง

## 2026-09-08 — n8n: RP ย้าย + แยกเป็น stack ของตัวเอง

- `stacks.homepage.var_n8n_https` → `https://<domain>:15678` (เดิม
  `https://n8n.<domain>` บน 443) ตาม DSM reverse proxy ที่ย้ายจาก subdomain
  มาเป็น port
- `HOMEPAGE_VAR_N8N_HTTP` (ping) ไม่แตะ — ยังยิงตรง `http://<lan-ip>:5678`
  ผ่าน LAN ไม่ผ่าน RP
- `config/services.yaml` description "(Secretary Stack)" → "(shared)" เพราะ n8n
  แยกออกจาก secretary เป็น stack ของตัวเองแล้ว (ดู `../../n8n/.notes/`)
- ยังไม่ deploy

## 2026-07-07 — เพิ่ม ink-reader widget

เพิ่ม ink-reader (Doujin Library, port 5068/15068) เข้า dashboard:

**ไฟล์ที่เปลี่ยน:**
- `config/services.yaml` — เพิ่ม ink-reader tile ใน 📥 Downloads & Monitoring
  (customapi widget แสดง New/Kept/Last Scrape จาก `/api/status`)
- `secrets.manifest.yaml` — เพิ่ม `HOMEPAGE_VAR_INK_READER_URL`

**Vault key ที่ต้องเพิ่ม:**
- `stacks.homepage.var_ink_reader_url` = `http://192.168.50.200:5068`
- ใช้ `make edit-vault` → เพิ่ม key → `make secrets && make sync-test-vault`

---

## 2026-06-07 — ลบ Glances sidecar ออกจาก stack

ผู้ใช้ตัดสินใจไม่ใช้ Glances แล้ว ลบออกทั้งหมด:

**ไฟล์ที่แก้:**
- `docker-compose.yml` — ลบ `glances` service block ทั้งหมด (nicolargo/glances:latest-full)
- `config/widgets.yaml` — ลบ 2 glances top-bar widget blocks (gpu:0 + process)
- `README.md` — อัปเดต file structure + ลบ Glances section
- `CLAUDE.md` (root) — อัปเดต homepage row ลบ glances port + note
- `.notes/00_INDEX.md` — อัปเดต file map + change log

---

## 2026-06-06 — Phase 1+2 enhance: Glances + bookmark reorg

### งานที่ทำ

**Phase 1 — Glances system monitor**
- เพิ่ม `glances` service ใน `docker-compose.yml` (image `nicolargo/glances:latest-full`, expose 61208, `pid: host`, `privileged: true`, NVIDIA env mirror Jellyfin)
- `widgets.yaml`: เพิ่ม 2 glances top-bar widgets — `metric: gpu:0` + `metric: process` (API v4)
- `services.yaml`: เพิ่ม Glances tile ใน `📥 Downloads & Monitoring` (type: glances, metric: info)

**Phase 2 — Bookmark reorganization**
- `bookmarks.yaml` rewrite — 5 groups: Quick Access / NAS Admin / Network / Dev Tools / Reference
- เพิ่ม shortcut: Anthropic Console, OpenRouter, Synology DSM/Container Manager direct link, regex101, JSON Crack, Homepage Docs, Jellyfin Docs

**ไฟล์ที่เปลี่ยน:**
- `homepage/docker-compose.yml` — เพิ่ม glances service
- `homepage/config/widgets.yaml` — rewrite ใส่ glances blocks
- `homepage/config/services.yaml` — เพิ่ม Glances tile
- `homepage/config/bookmarks.yaml` — rewrite 5 groups
- `homepage/README.md` — อัปเดต file map + Glances section

**Next:**
- Deploy: `make secrets && ./scripts/deploy.sh` แล้ว restart homepage stack ใน Container Manager
- Verify: เปิด dashboard, ดู GPU widget แสดง NVIDIA stats (ต้องมี Jellyfin transcoding active เพื่อเห็น %)
- ถ้า glances UI ไม่ขึ้น: check container log `docker logs glances` — Synology อาจไม่ allow `pid: host` หรือ `privileged: true` ตาม security policy

---

## 2026-05-28 — เพิ่ม n8n widget

### งานที่ทำ

เพิ่ม n8n (Secretary Stack, port 5678/15678) เข้า dashboard ใน section **📝 Tools & Notes**

**ไฟล์ที่เปลี่ยน:**

`homepage/config/services.yaml`:
- เพิ่ม n8n entry ต่อท้าย Hermes Agent — ใช้ `type: n8n` widget + ping

`homepage/.env.example`:
- เพิ่ม 2 ตัวแปร: `HOMEPAGE_VAR_N8N_HTTP`, `HOMEPAGE_VAR_N8N_HTTPS`, `HOMEPAGE_VAR_N8N_KEY`
- สร้าง API key ใน n8n → Settings → API → Create API Key แล้วใส่ใน `.env`

---

## 2026-05-24 — ย้ายกลับ basic auth (ลบ Authelia)

### งานที่ทำ

**เหตุผล:** ลบ Authelia auth stack ออก → homepage nginx กลับมาใช้ basic auth เหมือนเดิม

**ไฟล์ที่เปลี่ยน:**

`homepage/nginx/nginx.conf`:
- ลบ `location /authelia` (forward-auth endpoint) ออก
- ลบ `auth_request /authelia` และ `error_page 401 =302 http://<AUTHELIA_HOST>:9091` ออก
- เพิ่ม `auth_basic "Restricted"` + `auth_basic_user_file /etc/nginx/.htpasswd`
- mount path เปลี่ยนจาก `templates/default.conf.template` → `conf.d/default.conf` (ไม่ต้องใช้ envsubst แล้ว ไม่มี env vars ใน config)

`homepage/docker-compose.yml`:
- ลบ `auth_net` external network ออกทั้งหมด (nginx ไม่ต้อง join auth_net อีก)
- ลบ `AUTHELIA_HOST` env var ออกจาก nginx service
- เพิ่ม volume mount: `./nginx/.htpasswd:/etc/nginx/.htpasswd:ro`

`homepage/nginx/.htpasswd` (ใหม่, ไม่ commit):
- APR1 hash สำหรับ user `fixhardez`

`homepage/.env.example`:
- ลบ `NAS_HOST` + `AUTHELIA_HOST` block ออก (ไม่ใช้อีกต่อไป)

`homepage/.env`:
- ลบ `NAS_HOST`, `AUTHELIA_HOST`, `NGINX_BASIC_AUTH_USER`, `NGINX_BASIC_AUTH_PASS` ออก

**Deploy:** `scripts/deploy.sh -s homepage -y` — Container homepage-nginx recreated ✅

### Bug หลัง deploy — 500 Permission denied

**อาการ:** หลัง deploy ได้ 500 Internal Server Error ทันที

**Root cause:** `tar --no-same-permissions` extract `.htpasswd` ออกมาเป็น permission `600` (root-only) → nginx worker (non-root) อ่านไม่ได้ → `open() "/etc/nginx/.htpasswd" failed (13: Permission denied)`

**Fix ทันที:** `sudo chmod 644 /volume2/docker/homepage/nginx/.htpasswd` + `docker restart homepage-nginx`

**Fix ถาวร:** เพิ่ม chmod loop ใน `scripts/deploy.sh` — หลัง upload ทุกครั้งจะ `chmod 644` ทุก `nginx/.htpasswd` ใน stacks อัตโนมัติ

---

## 2026-07-26 — เพิ่ม tile ops-bot

`homepage/config/services.yaml` — เพิ่มรายการใน group `📥 Downloads & Monitoring` (วางไว้เหนือ Portainer):

```yaml
- ops-bot:
    icon: mdi-robot-love
    href: "{{HOMEPAGE_VAR_DDNS_BASE_HTTPS}}:15070/dashboard"
    description: AI Incident Response Bot
```

**ทำไม href ต้องมี `/dashboard`:** app ไม่มี route ที่ `/` — เข้า root แล้ว 404 จริงๆ (ไม่ใช่ bug)

**ทำไมไม่ใส่ `ping` และไม่ใส่ widget:** ops-bot อยู่หลัง nginx basic auth ทุก path (ยกเว้น `/webhook/uptime-kuma`) → ping จะได้ 401 แล้ว homepage แสดงเป็น down สีแดง เหมือนเหตุผลที่ dupe-sweeper กับ friendly-reminder ก็ไม่ใส่ ping

ไม่ต้องเพิ่ม `HOMEPAGE_VAR_*` ใหม่ — ใช้ `DDNS_BASE_HTTPS` ที่มีอยู่แล้ว

**Deploy:** `scripts/deploy.sh -s homepage -y` — verify ด้วย `docker exec homepage grep -A3 ops-bot /app/config/services.yaml` + เช็ค log ไม่มี error

## 2026-09-01 — เพิ่ม tile shorts-factory

`homepage/config/services.yaml` — เพิ่มรายการใน group `📥 Downloads & Monitoring` (ใต้ ops-bot):

```yaml
- shorts-factory:
    icon: mdi-movie-open-outline
    href: "{{HOMEPAGE_VAR_DDNS_BASE_HTTPS}}:15071"
    description: YouTube Shorts Dashboard
```

ไม่ใส่ `ping`/`widget` ด้วยเหตุผลเดียวกับ dupe-sweeper/ops-bot — อยู่หลัง nginx basic auth ทั้งเส้นทาง ping จะได้ 401 แล้วโชว์ down ผิดๆ ไม่ต้องเพิ่ม `HOMEPAGE_VAR_*` ใหม่ ใช้ `DDNS_BASE_HTTPS` เดิม

บริบท: ช่วงเดียวกันแก้ dashboard port ของ shorts-factory เอง `5069 → 5071` (ชนกับ dupe-sweeper) และผู้ใช้แจ้งว่าตั้ง DSM Reverse Proxy `15071 → localhost:5071` แล้ว จึงอัปเดต root `README.md`/`CLAUDE.md` จาก "LAN only" เป็น `https://…:15071` ด้วย — **ยังไม่ verify จริงว่า RP rule มีอยู่จริง** แค่เชื่อคำผู้ใช้ ต้องเช็ค `curl -o /dev/null -w "%{http_code}" https://fixhardez.synology.me:15071` (คาด 401) ตอน deploy รอบหน้า

**Deploy:** `scripts/deploy.sh -s homepage -y` — verify ด้วย `docker exec homepage grep -A3 shorts-factory /app/config/services.yaml`
