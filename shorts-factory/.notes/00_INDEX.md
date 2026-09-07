# shorts-factory — Index

Telegram bot that turns a one-line Topic into a 40-50s vertical Thai
DevOps/AI clip. Design decisions live in the repo root: `CONTEXT.md`
(vocabulary) and `docs/adr/0001..0003` (why no YouTube upload, why no HTTP
surface, why Pillow). Those ADRs are binding — read them before changing shape.

## Shape

- **Locale (2026-09-07).** `app/locales.py` bundles everything that moves
  together for a different audience — prompt language, TTS voice, on-screen
  line width, captions language, output subfolder, trends country, and the
  YouTube channel (its env prefix; the second channel's credentials are still
  outstanding) — behind `locales.get()`, which degrades an
  unknown or missing `locale` code to `th` so a Manifest written before
  Locales existed still loads. Environment is read at call time, not import.
  `/en <topic>` writes and renders an English clip; `/trends en` pulls Google
  Trends US plus the YouTube US chart and its number buttons produce English
  clips. Bot messages, buttons and errors stay Thai regardless of the clip's
  language. English files land in `/output/en` (`/volume1/shorts/en` on the
  NAS); Thai output is unchanged at `/output`. `app/backfill.py` walks with
  `rglob` instead of `glob` to reach both folders and reads the locale back
  from the folder name. See `docs/adr/0008` for why English publishes to a
  second channel instead of sharing the Thai one.
- **Numbers are per channel (2026-09-07, batch 2).** `history.json` carries a
  `locale` per upload; `history.video_ids(locale)` / `recent_titles(locale)`
  filter on it (no argument = every channel, no field = Thai).
  `analytics.performance/latest_data_date/gate_note/format_report/winning_examples`
  all take a Locale and use that channel's token, so ADR 0004's Gate is
  reached per channel with the same thresholds. `experiment.for_locale()`
  filters before `tally`/`by_category`, and `report(records, locale)` prints
  one section per channel. `snapshots.run()` groups the day's clips by Locale
  and does one Analytics pull per channel — a video id the channel does not
  own returns no row rather than an error, which would read as "not processed
  yet" — skipping a Locale with no credentials and catching a channel that
  fails so the other still gets its reading. `retention.fetch(..., locale)`
  takes the channel from the clip's own Manifest, so there is no command
  argument that could name the wrong one.
- **Locale pairs (2026-09-07).** `/both <topic>` and the 🌏 row under a
  `/trends` list make the same Topic in both Locales, sequentially: Thai is
  written, reviewed and rendered through the existing path, and `do_render()`
  spawns `continue_pair()` only after the Clip is actually delivered. The
  English half is written natively with the approved Thai Script as context
  (`script._sibling_note()`), never translated — the line budgets and the
  hooks differ. Both Manifests carry `pair_id` (the Thai half's clip id). A
  failed render or a discarded Script cancels the queued half and says so.
  Variants are drawn independently per half. Numbered buttons and the
  automatic rounds stay Thai-only.
- **Upload buttons carry their Clip id** (`upload:<clip_id>`), with the
  pending set in `state["uploads"]` capped at 10. Before this the button meant
  "whatever was rendered last", so two clips rendered back to back — which a
  pair does by design — left the older button uploading the newer clip. A
  button with no id still means the last clip, which is what it meant when it
  was sent.
- One container for the bot itself: no ports, no scheduler thread. A single
  Telegram `getUpdates` long-poll loop is its entire interface; the two
  recurring jobs (daily snapshots, `/trends` three times a day) ride that
  loop. The stack as a whole now has an HTTP surface — see the Dashboard
  bullet below and `docs/adr/0007` — but the bot process itself is unchanged:
  still portless, still Telegram-only.
- Flow: Topic → mimo returns a Script → human reviews it in Telegram →
  button → the whole narration is spoken in **one** edge-tts call, footage is
  fetched per Card, cards are drawn with Pillow → silent video segments cut to
  the sentence boundaries, concatenated, then the narration muxed over the
  whole thing → mp4 delivered to Telegram and to `/output`.
- **Every Card has two narrations.** `narration` = screen/subtitle form
  (English stays English), `spoken` = Thai-script transliteration, the only one
  edge-tts reads. `validate()` rejects any Latin character in `spoken`, and
  `render._speakable()` strips hyphens/dashes before synthesis — the voice reads
  one as a ~1s pause ("เอฟ-35" became "เอฟ" … "35"), so model names are said whole.
  **The rule is mirrored for English (2026-09-07):** Thai `spoken` still
  forbids Latin, but an English clip's `spoken` forbids Thai instead, and
  `render._speakable()` joins words with a space for English — Thai has no
  word boundaries to join on, English does.
- **Pronunciation overrides live in `/data/say.json`.** `validate()` cannot see
  that a word is said wrong: the model letter-spelled "TH-AI Passport" into
  `ทีเอไอพาสปอร์ต` (2026-08-31), and edge-tts mangles some correctly spelled
  Thai on its own. `/say <wrong> = <right>` writes the substitution; it is
  applied in `render._tts_text()` and **must stay there** — later than that and
  `narrate()`'s boundary check compares pre- against post-substitution text,
  fails on every Card, and falls back to per-Card speech silently.
- **`/redo` re-renders the last Script.** Everything `deliver()` snapshots
  into `last_*` is put back into `state` and `do_render()` runs again — same
  words, new synthesis, same Manifest. It exists because a `/say` fix would
  otherwise cost a full rewrite (minutes of model time, and a different
  Script). Anything else starting with `/` is answered as an unknown command:
  a typo used to open a Clip (`/stat` 2026-08-30, `/redo` 2026-08-31).
- **Result-shaped Topics are refused.** `main.RESULT_TOPIC` matches ชนะ/แพ้/
  สกอร์/ตกรอบ/a scoreline and turns the Topic away inside `make_script()`,
  before a Manifest or a Variant is claimed, so every entry point (typed,
  trend button, automatic pick) goes through one door. The model cannot know a
  result — it writes the same generic essay and invents figures (six
  volleyball clips, 2026-08-29..31, two published with made-up numbers).
  A leading `!` skips the check; revisions are never checked.
- **A bare URL is not a Topic.** Telegram's rich link preview is not included
  in `message.text`; the bot receives only the URL, while mimo has no browser
  and commonly answers prose instead of Script JSON. `make_script()` rejects
  URL-only input before claiming a Manifest/Variant and asks for a headline or
  topic. A topic plus an optional reference URL still goes through normally.
- **Line length is checked in pixels for Thai, but the character count is
  enforced for English (2026-09-07 clarifies what changed).** `script._too_wide()`
  measures the line with Waree at `render.MIN_TEXT_SIZE` against 864px — the
  1080px frame less margins, which is the narrower of the two draw paths (text
  over footage; the gradient card is 1210px wide). For Thai, that pixel
  measurement is still the only gate: a character count is a bad proxy for
  Thai, measured across 209 accepted lines the widest was 719px at 33
  characters, and a 37-character line lost a whole Script on 2026-08-29, so
  `HARD_MAX_CHARS_PER_LINE = 34` survives only as prompt guidance (the model
  cannot measure pixels) and as the fallback where the font is unavailable.
  English does not get that free pass: at full size (size 92) Waree-Bold
  renders a Latin character at about 50px against Thai's 21px, so the same
  pixel floor would pass a 38-character Latin line and draw it at the 40px
  minimum — unreadable on a phone. The English Locale sets
  `enforce_char_count: True`, and `_too_wide()` rejects any English line over
  24 characters (target 18) regardless of what the pixel measurement says.
  Keep these two figures attached to their own language: 719px/33 chars is
  Thai's pixel measurement, 50px-vs-21px is the full-size per-character figure
  behind English's hard character cap — they are not the same measurement and
  do not merge into one number.
- **Footage can come from the human.** 🎨 on a Script makes the bot write an
  English Flow Prompt for the Hook card and park the Clip in `state["parked"]`;
  the human generates it in the Google Flow app and replies to that message
  with the mp4. The reply's `reply_to_message.message_id` is the only thing
  that binds a file to a Card — never the filename, never the arrival order.
  Files land in `/output/footage/<clip_id>/c00.mp4` and reach the renderer as
  `render.build(..., supplied={0: path})`. One parked Clip at a time, 24h to
  live, `auto_pick_due()` stands down while one exists. The bot never calls a
  video model itself: `docs/adr/0005`.
- **Storyboards are written for Google Flow.** 📋 plans one for the Script
  under review (9:16, one Scene per Card), `/storyboard <brief>` plans a 16:9
  one for long-form. Both cost a model call and both stop at prompts — the bot
  assembles nothing (`docs/adr/0006`). Sent as one message per Scene: Thai to
  read, English in copy blocks to paste. The master character is locked by a
  phrase `validate()` requires verbatim in every scene's image prompt; the
  narration and on-screen lines are written back in from the Script
  (`lock_to_script`) rather than trusted to the model. 📋 mutates no state, and
  reads `clip_id` before the model call because the human can move on mid-run.
- **Card joins are trimmed.** The paragraph break that produces the boundary
  events also produces ~1.0s of dead air per join (measured; clause breaks are
  0.12-0.53s). `render.tighten()` slices at the boundaries, trims each slice's
  tail back to `JOIN_SILENCE` (0.30s) and re-joins, recomputing the starts by
  measuring the trimmed slices — the endpoint's offsets overshoot the file.
- **Card timing comes from `SentenceBoundary` events.** Thai emits no
  `WordBoundary` (no spaces), so per-word timing does not exist. If the
  boundaries do not line up with the Cards, it falls back to speaking each Card
  separately.
- Two card looks: over footage (transparent card + scrim, footage supplies the
  motion) or, when no footage came back, the gradient card with a Ken Burns
  move. The fallback is silent by design.
- **Learning loop (ขั้น 1-2 ลงแล้ว 27/08).** แผน 6 ขั้นที่
  `.notes/plan-learning-loop.md`, เหตุผลที่ `docs/adr/0004`, ศัพท์ที่ root
  `CONTEXT.md`. หัวใจ: บันทึกทุกอย่างลง Manifest ต่อคลิป (รวม Script ที่ไม่ได้อัป)
  แต่**ห้ามสรุปหรือปรับพรอมป์เอง**จนกว่าจะผ่าน Gate. `winning_examples()` ต้องปิด
  ก่อนถึง Gate. **ลงแล้ว**: `app/manifest.py` (1 ไฟล์ต่อ Topic ที่ `/data/clips`,
  เก็บ draft ทุกรอบรวมที่กดทิ้ง + `render` details ที่ `build()` คืนกลับมา) และ
  `analytics.gate_note()` ที่ปิด `winning_examples()` จนกว่าจะครบ 30 คลิป.
  `app/snapshots.py` (job รายวันขี่ poll loop ไม่ใช้ scheduler thread, `/snapshot`
  สั่งมือได้, day-7 = ตัวเลขทางการ) และ `app/backfill.py` (กู้ manifest 9 คลิปเก่าจาก
  `.txt`/`.srt` ใน `/output` ตอน startup, idempotent, ติดธง `reconstructed`).
  `app/experiment.py` (factor `hook` 2 variant สุ่มต่อคลิปตอนเปิด Topic ไม่ re-roll,
  explore 1 ใน 3 ไม่นับผล, `/experiment` รายงาน + ปฏิเสธที่จะฟันธงก่อนถึงเกณฑ์).
  `app/retention.py` (เส้น retention + หา cliff + map กลับเป็น card + วาด PNG ด้วย Pillow,
  `/retention`) และ `app/trends.py` (Google Trends RSS ไทย + YouTube chart ไทย → mimo
  แปลงเป็นหัวข้อ, `/trends` — ลิสต์หัวข้อแนบปุ่มเลข กดแทนพิมพ์ได้ `callback_data`
  = `pick:<suggested_at>:<index>` เทียบ timestamp กันกดปุ่มของลิสต์เก่า). **หัวข้อไม่ล็อก DevOps/AI แล้ว** (ADR 0004 ท้ายไฟล์) —
  `category` เป็นมิติที่บันทึกไว้อ่านแบบสังเกตการณ์ ไม่ใช่ variant ที่สุ่ม.
  **ยังไม่ลง**: recommender (ขั้น 6 รอ Gate)
- **The bot starts Topics itself (28/08).** `auto_slot()` owes the newest
  passed hour of `TRENDS_HOURS` (default `8,12,17`, TZ Asia/Bangkok) and the
  slot is stamped *before* the run spawns — `suggest_topics()` takes minutes and
  an unstamped slot re-fires on the next 30s tick. The automatic list carries a
  ✋ button (callback `cancel:<suggested_at>`, stamp-checked like the 💡 ones,
  and its branch must stay **above** the `mode != "review"` return in
  `on_callback()` or the tap dies silently). No tap within `AUTO_PICK_MINUTES`
  (default 15) and while `mode == "idle"` → a random suggestion is written and
  **rendered unattended**. That Script is posted with no keyboard and no
  `message_id`: `do_render()` rewrites the message it tracks. Auto-render sits
  at the end of the success path *inside* `make_script()` — the failure handler
  returns with `script=None`. Uploading is still a button (ADR 0001).
- State: `/data/state.json`. Working files under `/data`, wiped after each
  render. Finished clips and their metadata `.txt` land in `/output`
  (`/volume1/shorts` on the NAS, reachable over SMB).
- **Dashboard (2026-09-01).** `app/dashboard.py` runs as a second container
  (`shorts-factory-dashboard`, same image, different `command`) behind an
  nginx sidecar publishing 5071 with basic auth. Read-only twice over: `/data`
  mounted `:ro` and the app declares no non-GET/HEAD route (`test_no_route_can_write`).
  No `env_file` — it holds neither the Telegram bot token nor the YouTube
  refresh token. Four pages: `/` (clip list + day-7 numbers), `/clip/{id}`
  (full manifest incl. discarded drafts), `/experiment` (arms + verdict),
  `/now` (state.json + say.json + recent uploads). Reads `say.json` itself via
  a local `_say()`, does not import `app.render`. See `docs/adr/0007`.
- **Dashboard look (2026-09-02).** Token-based CSS shared in spirit with
  `dupe-sweeper` (amber accent here), light/dark from the system with a
  `localStorage` toggle and an anti-flash inline script in `<head>`, tables
  collapsing to one card per row below 600px. `app/static/app.js` (~20 lines,
  no dependency) does the toggle and a client-side outcome filter — filtering
  stays in the browser so no route grows a query parameter. `/` leads with four
  day-7 KPIs; `state.mode` is not one of them (a stale value would read as
  live). `/clip` charts views over age as **inline SVG** built by
  `dashboard._chart()`: `app.retention`'s Pillow rendering must never be
  imported here, and `test_no_drawing_library_in_this_process` asserts `PIL`
  and `edge_tts` are absent from `sys.modules`.
- **Port 5071, not 5069 (2026-09-01).** The dashboard originally shipped on
  5069; that collided with `dupe-sweeper` which already owned it. Moved to
  5071 in `docker-compose.yml` — ADR 0002/0007 and the original design
  spec/plan still say 5069, they're left alone (decision record, not live
  config). External access: DSM Reverse Proxy `15071 → localhost:5071` plus a
  homepage tile (`homepage/config/services.yaml`, no ping/widget — the whole
  path is behind basic auth so a ping would 401 and show as down).

## Settings

| Key | Source | Note |
| :--- | :--- | :--- |
| `MIMO_API_KEY` | `shared.llm.mimo_api_key` | shared with news-feed; ops-bot keeps its own copy |
| `TELEGRAM_BOT_TOKEN` | `stacks.shorts_factory.telegram.bot_token` | dedicated bot, not ops-bot's |
| `TELEGRAM_CHAT_ID` | `stacks.shorts_factory.telegram.chat_id` | only trust boundary — all other senders dropped |
| `TTS_VOICE` | literal | `th-TH-NiwatNeural` |
| `TTS_VOICE_EN` | literal | `en-US-AndrewNeural`; English Locale's voice, added 2026-09-07 |
| `PEXELS_API_KEY` | `stacks.shorts_factory.pexels_api_key` | free key; absent = every card falls back to the gradient |
| `BGM_DIR` | literal `/output/bgm` | drop CC0 tracks in; empty or missing = no music |
| `MIMO_REASONING_EFFORT` | literal `low` | mimo-v2.5-pro is a reasoning model; the default budget doubles latency for no better script |
| `YOUTUBE_SET_THUMBNAIL` | literal `false` | the Shorts feed ignores custom thumbnails, so it is opt-in |
| `MIMO_TIMEOUT_SECONDS` | literal `600` | wall-clock deadline per model call — httpx's own timeout is per read and will not fire on a trickling server |
| `YOUTUBE_*` | `stacks.shorts_factory.youtube.*` | empty until `scripts/youtube_auth.py` is run; no credentials = no upload button |

## Gotchas

- **A model call must have `asyncio.wait_for` around it.** httpx logs
  `200 OK` on headers, so a stalled body looks like success in the log, and its
  `timeout` is per read, not a total budget. The poll loop is inline, so a hung
  call freezes the entire bot.
- **"mimo ไม่ตอบภายใน 600 วินาที" does not mean mimo was down.** The retry
  shares one deadline, so an attempt that answers slowly *and* fails
  `validate()` leaves the second attempt only the remainder. Read the log
  before blaming the endpoint: two hedge warnings mean the first attempt came
  back and was rejected. The `%d tokens (%.0f tokens/วินาที)` line from
  `once()` is the discriminator — a healthy think runs at about 30 tokens a
  second however long it takes, and a stalled request never logs at all while
  its hedged twin does. The `HTTP Request: POST ... 200 OK` line lands ~8s
  after every mimo call (httpx logs on headers) and says nothing about health.
- **`generate()` retries until the deadline, not twice.** Until 2026-09-04 the
  loop was `for attempt in range(2)`: an attempt that failed fast burned an
  attempt rather than time, and the 08:16 auto round gave up after 172s with
  ~420s of the 600s budget still unspent, on a topic that succeeds on a plain
  retry. It now loops while `deadline - now >= MIN_ATTEMPT`, capped at 4 so a
  client that always fails fast cannot spin.
- **Only a reply that parsed as JSON is fed back to the model.** A reply that
  did not parse (the 60-character answer that started the above) is retried
  with `messages` untouched — appending garbage plus a correction turn only
  poisons the next model's context, and the small model answered that with a
  fragment missing `title`. A parsed-but-invalid reply still gets the
  append-and-correct turn, which is what that mechanism was for.
- **A truncated reply is a failure, not an answer.** `once()` reads
  `finish_reason` and raises on `"length"` or blank content, so the hedge and
  retry handle it instead of `_parse()` choking on half a JSON object. The
  reason is logged on the `ตอบใน ... วินาที` line, and both failure branches
  log `raw[:300]` — the old warning logged only the length, which is why
  nobody could say what those 60 characters were.
- **The schema retry leads with `mimo-v2.5`, not the pro model.** It inherits
  only the remainder of the shared deadline, which can be shorter than a pro
  think (measured: 257s left against a 347s worst case). The hedge still goes
  to the *other* model either way.
- **`/stats` and prompt priming only know about clips uploaded through the
  bot** (`/data/history.json`). Anything published by hand is invisible to
  them.

- **Pillow needs `libraqm0` from apt.** The wheel does not bundle Raqm, and
  `ImageFont.Layout.RAQM` fails *silently* without it — Thai tone marks vanish
  with only a `UserWarning`. The Dockerfile asserts `features.check('raqm')` at
  build time so this can never ship broken again.
- **Use `Waree-Bold` (`fonts-thai-tlwg`), never Noto Sans Thai.** Noto Sans
  Thai's cmap has no Latin letters or digits, so any English word inside a Thai
  sentence renders as tofu boxes; Pillow does no font fallback.
- **ffmpeg `drawtext` cannot render Thai** (no shaping). Not an escape hatch.
- **Do not downgrade `edge-tts`.** 7.0.2 gets `403` from the synthesis endpoint;
  the `Sec-MS-GEC` token scheme moves server-side. 7.2.8 works.
- **No `cpus:` in compose.** DSM's kernel has no CFS bandwidth control and the
  daemon refuses to create the container. `mem_limit` works fine.
- Host RAM is under pressure (swap fully consumed; whole-box OOM on
  2026-08-19), hence `mem_limit`/`cpus` in the compose file.

## Gaps

- Built and verified on the NAS but **not running**: `/volume1/shorts` must be
  created as a shared folder in DSM first, and the human must press Start on
  `@JaFixShortsBot` (a bot cannot open a chat; `sendMessage` returns "chat not
  found" until then).
- **Answered 2026-08-26:** the first upload came back `public`, so an unaudited
  project did not force it to private — the caution in ADR 0001 did not bite.
- The Shorts feed cover cannot be set through the API; `thumbnails.set` only
  affects search, the channel page and suggestions.
- The Google API-audit and OAuth-refresh-token claims behind ADR 0001 were
  never checked against Google's own docs. Confirm before building any upload.
- **English Locale: closed 2026-09-07.** Both channels are live — Thai
  `FixHarDeZ` (`@fixhardez`), English `Just Decoded It` (`@justdecodedit`) —
  with `stacks.shorts_factory.youtube_en.*` in the vault and the three
  `YOUTUBE_EN_*` names mapped in `secrets.manifest.yaml`. That mapping goes in
  **after** the vault holds the values, never before: `render_env.py` raises
  `missing vault path` and `make secrets` then fails for every stack in the
  repo, not just this one. Which channel a token actually belongs to is only
  visible by asking (`channels.list?mine=true` per Locale) — worth re-checking
  after any re-auth. The rest landed the same day:
  per-channel history/analytics/Gate, one snapshot pull per channel,
  `/retention` reading the channel off the Manifest, `/experiment` and the
  dashboard split per Locale, and the dashboard's ภาษา column. The
  upload-routing guard is credential-gated: `youtube.py` reads every setting under the Locale's own
  env prefix with no shared fallback, `deliver()` only shows the button when
  `youtube.configured(locale)` is true for that clip's locale, and
  `add_captions` tags the track from the Locale — so until `YOUTUBE_EN_*` is
  provisioned the button simply never appears on an English clip, and the
  human still uploads it by hand from `/volume1/shorts/en`.
  `storyboard.SHORTS_LAYOUT` also still describes its negative space as being
  "for Thai subtitles" — left alone in this batch.
