# n8n — Project Index (Memory Blueprint)

> อัปเดตล่าสุด: 2026-09-08 (แยกออกจาก secretary เป็น stack ของตัวเอง)
> ใช้ไฟล์นี้เป็น cold-start memory ก่อนเริ่มงานทุกครั้ง

---

## Overview

n8n เป็น workflow automation กลางของทั้ง NAS เดิมอยู่ใน `secretary/` (container
`secretary-n8n`) ถูกแยกออกมาเป็น stack ของตัวเองเมื่อ 2026-09-08 เพื่อให้ stack
อื่นเรียกใช้ร่วมกันได้โดยไม่ผูกกับ lifecycle ของ secretary

- image `n8nio/n8n:latest`, container `n8n`, port 5678 → DSM RP :15678
- ไม่มี `depends_on` ไม่มี build ไม่มี volume ร่วมกับใคร

## การเรียก stack อื่น

Workflow ยิงผ่าน published port ของ host:
`http://host.docker.internal:<port>/...` (มาจาก `extra_hosts:
host.docker.internal:host-gateway`)

**ไม่ได้ใช้ shared docker network** — ชื่อ DNS แบบ `secretary-query` resolve ได้
เฉพาะใน compose network ของ stack นั้น ย้ายมาที่นี่แล้วใช้ไม่ได้

ปลายทางที่ใช้อยู่: `http://host.docker.internal:5065/query`,
`http://host.docker.internal:5065/ingest-trigger` (secretary)

## Data

`n8n_data` bind ไปที่ `/volume2/docker/n8n/n8n_data` — ย้ายมาจาก
`/volume2/docker/secretary/n8n_data` ตอนแยก stack (rename ภายใน volume2 =
atomic ไม่ copy) เก็บ workflow, credentials และ encryption key ที่ใช้ถอด
credentials

⚠️ n8n เจอ `.n8n` ว่างเมื่อไหร่ = สร้าง encryption key ใหม่ credentials เก่าถอด
ไม่ออกถาวร **`ls` ยืนยันว่าไดเรกทอรีไม่ว่างก่อน start เสมอ**

`scripts/deploy.sh` chown ไดเรกทอรีนี้เป็น `1000:1000` เพราะ image รันเป็น
`node:node` ไม่ใช่ NAS user

**bind ทำงานจริง (ยืนยัน 2026-09-08)** — `docker volume inspect` โชว์
`Mountpoint` เป็น `/volume2/@docker/volumes/secretary_n8n_data/_data` ซึ่งเคยทำให้
เข้าใจผิดว่า compose `device:` ไม่ได้ map (บันทึก secretary 2026-06-23). วัดแล้ว
device+inode ตรงกันทั้งสอง path = ไฟล์ชุดเดียวกัน mountpoint เป็นแค่จุดที่ docker
เอา bind ไปแปะ

## Reverse proxy / webhook

DSM RP เปลี่ยนเมื่อ 2026-09-08: จาก `n8n.<domain>` port 443 → `<domain>` port
**15678**. `N8N_WEBHOOK_URL` ใน vault (`stacks.secretary.n8n.webhook_url`) กับ
`stacks.homepage.var_n8n_https` แก้ตามแล้ว

⚠️ **n8n ลงทะเบียน webhook กับ Telegram ตอน activate workflow เท่านั้น** เปลี่ยน
`N8N_WEBHOOK_URL` แล้วต้องปิด-เปิด workflow ที่มี webhook trigger ใหม่ ไม่งั้น
Telegram ยิงไป URL เก่าต่อไป บอทเงียบโดยไม่มี error ให้เห็นทั้งฝั่ง n8n และ
Telegram

## Secrets

`n8n/secrets.manifest.yaml` → vault path ยังเป็น `stacks.secretary.n8n.*`
(path ใน vault ไม่จำเป็นต้องตรงชื่อ stack เปลี่ยนแล้วต้อง edit-vault เพิ่มโดย
ไม่ได้อะไรกลับมา)

`N8N_API_KEY` ไม่ได้ใช้ใน compose — `scripts/n8n_export.sh` / `n8n_import.sh`
อ่านจาก `n8n/.env`

## Workflow backup

`n8n/workflows/*.json` git-tracked, export/import ผ่าน REST API + SSH
(`scripts/n8n_export.sh`, `scripts/n8n_import.sh`)

## Cutover checklist (ทำครบแล้ว 2026-09-08 — เก็บไว้เป็นลำดับอ้างอิง)

เรียงตามลำดับ ห้ามสลับ — ขั้น 3-5 คือช่วงที่ data ถูกย้าย ห้ามมี container
ตัวไหนถือ `n8n_data` อยู่

1. `make secrets` (ทำแล้ว — ได้ `n8n/.env`)
2. `./scripts/deploy.sh -y` **อัปไฟล์อย่างเดียว ยังไม่ restart** — จะได้
   `/volume2/docker/n8n/` พร้อม compose บน NAS
   (chown 1000:1000 ในสคริปต์จะยังไม่เจอ `n8n/n8n_data` เพราะยังไม่ย้าย —
   มันกลืน error ด้วย `|| true` ปกติ ขั้น 4 chown ให้เอง)
3. `docker compose --project-directory secretary/ -f secretary/docker-compose.yml down`
   — **`down` เฉยๆ ห้ามใส่ `-v`** ต้อง down ก่อนเพราะ (ก) container
   `secretary-n8n` ตัวเก่าถือ port 5678 และ (ข) มันถือ `n8n_data` อยู่
4. ย้าย data + ยืนยัน + chown:
   ```
   mv /volume2/docker/secretary/n8n_data /volume2/docker/n8n/n8n_data
   ls /volume2/docker/n8n/n8n_data        # ต้องเห็น config, database.sqlite
   chown -R 1000:1000 /volume2/docker/n8n/n8n_data
   ```
   **`ls` ต้องไม่ว่าง** ว่างเมื่อไหร่หยุดทันที อย่า start n8n
   (start ทับ = encryption key ใหม่ credentials ตายถาวร)
5. สร้าง project `n8n` ใน DSM Container Manager ชี้ที่ `/volume2/docker/n8n`
   — **ถ้ามันเสนอให้ pull image ให้ปฏิเสธ** `n8nio/n8n:latest` ไม่ pin version
   ถ้า version กระโดดพร้อมกับตอนแยก stack จะแยกไม่ออกว่าพังเพราะอะไร
   (`up -d` ไม่ re-pull ถ้า image มีอยู่ในเครื่องแล้ว)
6. `up` secretary กลับ
7. **ยืนยัน host-gateway ก่อนแตะ workflow** (docker บน NAS = 24.0.2 รองรับ
   `host-gateway` แต่ยังไม่ได้พิสูจน์ว่า DNAT ของ published port ใช้ได้จริงจาก
   bridge):
   ```
   docker exec n8n getent hosts host.docker.internal
   docker exec n8n node -e 'fetch("http://host.docker.internal:5065/health").then(r=>console.log(r.status))'
   ```
   ใช้ `node -e` ไม่ใช่ curl — image เป็น Alpine อาจไม่มี curl
   ถ้าไม่ผ่าน = ถอยไปใช้ IP ของ NAS ตรงๆ ใน URL แทน (**ยังไม่ import**
   จะได้ไม่ดัน URL พังเข้า instance จริง)
8. `./scripts/n8n_import.sh` ดัน URL ใหม่เข้า instance
9. **ปิด-เปิด workflow ที่มี webhook trigger ใหม่ทุกอัน** (Secretary Bot) —
   `N8N_WEBHOOK_URL` เปลี่ยนเป็น `<domain>:15678` แล้ว แต่ n8n บอก Telegram ว่า
   ให้ยิงมาที่ไหน **เฉพาะตอน activate workflow** เปลี่ยน env เฉยๆ ไม่ย้าย webhook
   ที่ลงทะเบียนไว้แล้ว ไม่ deactivate/activate = Telegram ยิงไป URL เก่าต่อ
   แล้วบอทเงียบโดยไม่มี error ที่ไหนเลย
10. **แก้ Uptime Kuma monitor id 28** (`Secretary N8N`) — เป็น monitor แบบ
   `docker` ผูกกับชื่อ container `secretary-n8n` ซึ่งเปลี่ยนเป็น `n8n` แล้ว
   ไม่แก้ = monitor แดงตลอด. ไม่มี API ต้องแก้ใน DB โดยหยุด container ก่อน:
   ```
   docker stop uptime-kuma
   sqlite3 <kuma.db> "update monitor set docker_container='n8n', name='n8n' where id=28;"
   docker start uptime-kuma
   ```
11. ทดสอบบอท Telegram จริง 1 คำถาม (ต้องผ่านทั้ง credentials เก่าและ
    URL ใหม่ = พิสูจน์ทั้งการย้าย data และ host-gateway ในนัดเดียว)

## หลุมที่ต้องระวังตลอดไป

- **`n8n_data` = single point of failure** เก็บ encryption key ที่ถอด
  credentials ทั้งหมด ไม่มี backup อัตโนมัติ start ทับตอนว่าง = ตายถาวร
- **volume `secretary_n8n_data` ตัวเก่ายังค้างใน docker** หลังย้าย (metadata
  ชี้ path ที่ไม่มีแล้ว) ลบทิ้งได้ด้วย `docker volume rm secretary_n8n_data`
  **หลังยืนยันว่า n8n ตัวใหม่ขึ้นและ credentials ใช้ได้แล้วเท่านั้น**

## Gaps / TODO

- **Uptime Kuma monitor id 28** ยังผูกชื่อ container `secretary-n8n` → แดงตลอด
  (ข้อ 10 ยังไม่ได้ทำ ต้องแก้ใน `kuma.db` ตอน stop container)
- `docker volume rm secretary_n8n_data` ทิ้งได้ (metadata ค้างชี้ path เก่า)
- ขยะบน NAS ที่ deploy ไม่ลบให้: `/volume2/docker/secretary/{n8n-workflows,
  secrets.manifest.yaml,ollama_data}`
- **coupling: homepage-nginx เป็นทางเข้า webhook** homepage ล่ม = บอทไม่ได้รับ
  ข้อความ ตัดได้ด้วยการ forward 8443 ที่ router
- `secretary-ingest` เคยขึ้นค้างทั้งที่เป็น one-shot `restart: "no"` แล้วโดน
  host OOM killer (exit 137) ทำ NAS ล่ม 4 ชม. — ยังไม่ได้ตามว่าใครสั่งมันรัน
