# Daily Log — shorts-factory

## 2026-08-28 — The bot goes looking for work on its own

`/trends` no longer needs a human to ask. Three times a day (`TRENDS_HOURS`,
default `8,12,17` in the container's Asia/Bangkok clock) the poll loop runs the
same `on_trends()` path, and the list it posts carries a ✋ button and a
deadline. Nobody taps a number and nobody taps ✋ within `AUTO_PICK_MINUTES`
(default 15) → one of the five suggestions is chosen at random, the script is
written and the clip is rendered unattended. Uploading stays a button under the
finished clip; ADR 0001 has not moved.

Rides the poll loop the way `snapshots.due()` does — still no scheduler thread,
still no port, still ADR 0002 shaped. Two pure functions carry the timing so
they can be tested without a bot: `auto_slot(state, now)` and
`auto_pick_due(state, now)`.

Things that had to be got right, each of which would have been a real bug:

- **Stamp the slot before spawning, not after.** `suggest_topics()` runs off
  the loop and can take minutes; an unstamped slot fires again on the next
  30-second tick and two trends runs race. `take_snapshots()` stamps in a
  `finally` for the same reason.
- **Only the newest passed hour is owed.** A restart at 23:00 produces one
  list (the 17:00 slot, late), not three.
- **An unattended script gets no keyboard and no `message_id`.**
  `do_render()` calls `close_prompt()` on the message id it finds and replaces
  the text wholesale — a tracked review message would have been overwritten
  with "กำลัง render", erasing the only copy of a script nobody was there to
  read. `close_prompt()` no-ops on `None`, so leaving it unset is the fix.
- **Auto-render lives inside `make_script()`, at the end of the success
  path.** `await make_script(); await do_render()` would raise `KeyError:
  'script'` on every LLM failure, because the failure handler returns normally
  with `mode="idle", script=None`.
- **The ✋ branch sits above the `mode != "review"` early return in
  `on_callback()`.** The bot is idle while a list is pending, so the guard
  would have swallowed the tap silently. The button carries `suggested_at` for
  the same reason `picked()` checks it: a tap on yesterday's list must not call
  off today's run. Cancelling edits only the reply markup, so the numbered
  buttons stay pressable.
- **One clear point for the pending pick:** `state.pop("auto_pick")` at the top
  of `make_script()`. Every start path routes through it, so no caller has to
  remember. The deadline fires only while `mode == "idle"`; a human mid-script
  when it passes drops the pick rather than getting a second clip queued behind
  their own.

Tests: `test_only_the_newest_passed_slot_is_owed`,
`test_the_automatic_pick_waits_for_its_deadline`. Full suite: 86 passed, 7
failed — all seven are the pre-existing Pillow/Raqm failures that cannot pass
on macOS (`libraqm0` is a Linux apt package; they pass in the container).

## 2026-08-24 — Design settled, stack scaffolded

Ran a full design interview (`/grill-with-docs`) for a new stack that turns a
one-line Topic into a Thai vertical short. Outcome recorded as vocabulary in
the root `CONTEXT.md` and three ADRs in `docs/adr/`.

Decisions worth repeating here: no YouTube upload in v1 (Google's API
compliance audit locks API-uploaded videos to private, and a `Testing` OAuth
consent screen expires refresh tokens after 7 days); no HTTP surface at all,
so no nginx, no `.htpasswd`, no published port and no port reservation; cards
drawn with Pillow rather than headless chromium; no scheduler — the bot acts
when a Topic arrives.

Verified on the NAS before writing any render code, in a throwaway
`python:3.12-slim` container:

- Pillow 12.3.0's manylinux wheel reports `features.check("raqm") == False`.
  `ImageFont.Layout.RAQM` then falls back to basic layout with only a
  `UserWarning`, and the mai-ek over sara-ii in "ที่" is dropped. This would
  have shipped silently.
- `apt-get install libraqm0` flips it to `True` (Raqm 0.10.5) with no Pillow
  rebuild, and the same string renders correctly. Fonts come from
  `fonts-noto-core`.
- `edge-tts` has both Thai voices (`th-TH-NiwatNeural`,
  `th-TH-PremwadeeNeural`) and synthesised a mixed Thai/English line fine.

Found while writing the manifest that `shared.llm.mimo_api_key` already exists
and holds the same value ops-bot copied into its own namespace, so this stack
needs no new mimo secret and ops-bot stays untouched.

Scaffolded `Dockerfile`, `docker-compose.yml`, `requirements.txt`,
`secrets.manifest.yaml`. The Dockerfile asserts Raqm at build time. `app/` not
written yet.

### Same day — app written, built and verified on the NAS

Wrote `app/script.py` (mimo → validated Script), `app/render.py` (Pillow cards,
edge-tts narration, ffmpeg concat) and `app/main.py` (long-poll loop and the
idle → review → rendering state machine in `/data/state.json`), plus 14 tests.

Four things only showed up on the real hardware:

1. **`cpus: "3.0"` cannot be used.** The daemon refuses the container:
   "NanoCPUs can not be set, as your kernel does not support CPU CFS scheduler".
   Dropped it; `mem_limit: 2g` is fine.
2. **`edge-tts==7.0.2` is dead.** Synthesis returns `403` from the
   `speech.platform.bing.com` websocket — the `Sec-MS-GEC` token scheme moves
   server-side, so old pins rot. 7.2.8 works. Never downgrade this one.
3. **Noto Sans Thai has no Latin glyphs at all.** Verified against its cmap:
   no A-Z, a-z or digits. "เก็บ log ไว้ที่ไหน" rendered the English word as
   three tofu boxes, and Pillow does no font fallback. Every TLWG face covers
   both scripts; switched to `Waree-Bold` from `fonts-thai-tlwg`, which is also
   the heaviest of them and reads best on a phone.
4. **`scripts/deploy.sh` has a hardcoded `ALL_STACKS` list.** A stack missing
   from it gets its `.env` skipped on upload and the restart fails with
   "Failed to load ... .env: no such file or directory". Added `shorts-factory`.

Two review-flow bugs fixed before first run, both found by reading rather than
testing: `make_script` left the previous review message's buttons live, so
approving an older message would have rendered a newer script — it now retires
the old prompt first; and a transient mimo error during a revision reset the
state to idle, throwing away the Script being iterated on — it now re-posts the
pending Script and stays in review.

Verified on the NAS: 14/14 tests pass inside the image; a two-card smoke render
produced a 1080x1920 h264+aac mp4 and the extracted frames show Thai, Latin,
tone marks and the code box all correct; `mimo-v2.5-pro` returned a valid
5-card Script for a real topic on the first try, every line within the 22-char
limit. Generation takes a few minutes — slow, but it happens once per clip.

Not running yet. `/volume1/shorts` does not exist, and compose refuses to start
without it (it does not auto-create the bind path, so there is no cruft to clean
up). The bot `@JaFixShortsBot` also returns "chat not found" until the human
presses Start.

### Same day — Ken Burns motion on every card

First clips looked fine but read as a slideshow, so cards now move. Each card is
drawn oversized (`OVERSCAN = 1.12`, so 1210x2150) and ffmpeg's `zoompan` crops a
1080x1920 window that slowly pushes in or pulls out across exactly the length of
that card's narration; direction alternates per card. Drawing oversized is the
point — zooming a card rendered at frame size would scale text past its native
pixels and soften it.

The zoom is driven off the frame counter (`on`) rather than accumulating into
`zoom`. Accumulation rounds at every step and the drift is visible as stutter on
a slow move. Card duration now needs `ffprobe`, so `audio_seconds()` reads it
before encoding.

Verified on the NAS: 17/17 tests pass in the image, and measuring the hook
card's yellow text across a rendered clip shows it growing 680px wide at t=0.2
to 724px at t=2.0 — the move is really happening, not just configured. Sampling
the final card at six points gives 864, 876, 894, 906, 921, 936 px: a clean
ramp, not the per-input-frame sawtooth `zoompan` produces when it is fed a
looped still the wrong way.

Timed a full-length render because the motion pass makes every frame distinct
where `-tune stillimage` used to coast on identical ones: a real 5-card Script
produced a **36.4s clip in 17.4s** inside the 2g cap, no OOM kill. Roughly half
real-time, so the render button stays a button.

### Same day — stock footage behind every card

Ken Burns alone still read as a moving slideshow, so cards now sit on real
video. Each Card carries a `query` (2-4 English words, something that can
actually be filmed) and `app/footage.py` pulls one portrait clip per Card from
the free Pexels API.

Two rendering paths now, picked per Card:

- **Footage found** — the card is drawn transparent at frame size with a drop
  shadow behind the text, the clip is scaled/cropped to fill 1080x1920, held
  back by a `black@0.5` scrim, and the card is overlaid. `-stream_loop -1`
  covers narration longer than the clip; no zoompan, the footage already moves.
- **No footage** — the original gradient card with the Ken Burns move.

`footage.fetch()` never raises: no key, no result, a timeout or a bad download
all return `None` and the Card quietly falls back to the gradient. That is the
whole failure story for this feature.

Verified on the NAS: 20/20 tests pass, and the same 5-card Script rendered to
36.4s in **54.7s** with footage (17.4s without) inside the 2g cap — three
Pexels downloads and five composites cost about 37 extra seconds. Extracted
frames confirm the footage is really behind the text, the scrim holds it back
far enough to read, and the code box stays legible over it.

`PEXELS_API_KEY` lives at `stacks.shorts_factory.pexels_api_key`. One test had
to stop hardcoding chat id 42 — it now reads `main.CHAT_ID`, because this suite
also runs with the real `.env` mounted.

### Same day — first real topic failed on a one-character overrun

"สายงานใหม่ AIOps กำลังมาหรอ?" came back with `card 4: บรรทัดยาว 23 ตัว เกิน 22`
and the whole clip was refused, twice, since the retry could not fix it either.

The rule was wrong, not the model. `validate()` was enforcing a limit the
renderer already handles — `_fit()` measures real pixel width and shrinks the
font — and character count is a bad proxy for width in Thai, where vowels and
tone marks have no advance at all. mimo naturally writes 17-23 character lines,
so a limit of 22 sat exactly on the boundary and rejected good scripts.

Split the number in two: 22 stays as the target in the prompt, and the enforced
limit is now 30, measured rather than guessed. At the renderer's smallest font
(44px, lowered from 52) thirty full-width Thai consonants measure 947px against
994px of usable card width and 32 overflow, while a natural 34-character Thai
line measures only ~788px. So 30 is the worst case still guaranteed to fit, and
anything under it renders without a hard failure.

Re-ran the same topic: 7 cards, longest line 23 characters — the exact case
that used to fail — with sensible footage queries on every card.

### Same day — narration prosody made configurable

Feedback was that the voice does not sound natural enough. Worth recording that
**edge-tts is Azure's neural voices** — it calls Edge's read-aloud endpoint —
so paying for Azure Speech buys the same audio. A real quality jump would mean
a different vendor (Google's Thai Chirp/Neural2, or a Thai specialist), not a
paid tier of what is already here.

Within edge-tts there are exactly three levers: voice (Thai has only Niwat and
Premwadee), `rate`/`pitch`, and the text itself. `speak()` now passes `TTS_RATE`
and `TTS_PITCH` through, defaulting to `+10%` / `+0Hz` — the stock rate reads
slow and flat.

Wrote ten A/B samples of one real narration line to `/volume1/shorts/tts-samples/`
for the human to listen to: both voices at +0/+10/+15%, pitch ±20Hz, plus two
text variants — one with commas inserted for breath pauses, one with the English
tech terms transliterated into Thai ("ด็อกเกอร์", "ล็อก") to test whether the
mid-sentence switch between Thai and English phonemes is what sounds wrong. If a
text variant wins, the fix belongs in the mimo prompt, not in the audio settings.

### Same day — narration rules moved into the prompt

The listening test picked samples 06 and 07: commas for breath pauses, and
English tech terms transliterated into Thai. Both are text properties, so the
fix went into mimo's system prompt rather than the audio settings, exactly where
the A/B was designed to point.

The important split: transliteration applies to `narration` **only**. On-screen
`lines` keep the English spelling, because "Docker" reads better on a card than
"ด็อกเกอร์" and the flags have to be shown verbatim to be useful. Command flags
that would be nonsense spoken (`--log-opt`) are described in words instead.

No validation added for this. Character-count enforcement had just caused a
hard failure on a good script, and the same trap applies here: a rule like "no
Latin letters in narration" would reject clips over a stray "Production".

Checked against a regenerated Script: cards came back with 1-3 commas each and
narration like "เอไอออปส์ คือการเอาเอไอ มาช่วยจัดการ โอเปอเรชันส์, ใช้เมชีนเลิร์นนิง
วิเคราะห์ข้อมูลปริมาณมหาศาล". Compliance is high but not total — one card still
had "Production" in Latin. Left as is; if it turns out to grate, a small
substitution table for the most common terms would be the deterministic fix.

`TTS_RATE` now defaults to `+10%`, which both winning samples used.

### Same day — narration is now one take, and the images are cut to it

Outside advice, and it was right: speak the whole Script as one continuous file
and mark where the images change, instead of synthesising per Card. Speaking
Card by Card restarts the intonation on every card and leaves silence at each
join, which is a large part of why the delivery sounded stitched together.

Finding the cut points took two probes. **Thai emits no `WordBoundary` events
at all** — no spaces to boundary on — so word-level timing is not available.
But the endpoint does emit **one `SentenceBoundary` per Card** when the Cards
are joined with `".\n\n"`, carrying `offset`, `duration` and `text`; it did not
split on a `?` inside a Card. As a cross-check, `silencedetect` separates the
joins cleanly too: card joins measure ~0.9s of silence against 0.4-0.5s for the
commas inside a sentence.

Architecture change that falls out of it: segments are now **video only**
(`-an`, duration from `-t`), concatenated, and the single narration is muxed
over the result. Nothing cuts the audio, so there is no join to click. Two
traps, both avoided deliberately:

- Sentence `duration` overshoots the file (last event ended at 19.98s on a
  19.30s file), so the final span comes from `audio_seconds()` and the video is
  padded `TAIL_PAD = 0.2s` past the audio, letting `-shortest` trim video
  instead of clipping the last words.
- A matching boundary count is not alignment — one Card split and two merged
  counts right too. Each boundary's `text` is checked against the start of its
  Card, and any mismatch falls back to the old per-Card path.

Verified end to end on a real 6-card Script: narration 38.136s, silent video
38.333s (the pad), final clip 38.136s with both streams — no truncated speech.
`silencedetect` found exactly five long gaps for five card joins. Frame
differencing across each join scored 29-51 against 4-22 for samples inside a
card, so every image change really does land on a sentence start. Render took
64.3s for a 38s clip under the 2g cap.

### Same day — background music, ducked under the narration

Now that the narration is a single continuous track, music is one filter rather
than a per-segment problem. `mux()` optionally takes a track, drops it to 0.35,
fades it out over the last 2s, and runs it through `sidechaincompress` keyed on
the narration itself, followed by `alimiter`. Speech stays on top because the
compressor is driven by the speech, not because a level was guessed.

Music comes from a folder (`BGM_DIR`, default `/output/bgm` = `/volume1/shorts/bgm`)
and one track is picked at random per clip. No folder or no tracks means no
music at all — that is the default state, since the folder does not exist yet.
Deliberately no music API: the risk here is Content ID, not integration.

Measured on two renders of the same Script, one with music and one without:

| window | no music | with music | delta |
| :--- | ---: | ---: | ---: |
| in a card gap | -90.0 dB (silence) | -27.9 dB | +62.1 dB |
| while speaking | -41.4 dB | -39.3 dB | +2.1 dB |

Music fills the gaps between cards and all but disappears under the voice.

Fetched six candidate tracks to `/volume1/shorts/bgm-candidates/` with 30-second
previews and a `LICENSES.txt`. All six are CC0, so no credit line is needed.
Worth recording how they were chosen: archive.org's `licenseurl` metadata is
supplied by uploaders and cannot be trusted on its own — a plain CC0 search
returns Pacman and Sega "game over" jingles tagged CC0, which they plainly are
not. Restricting to `collection:netlabels`, which is netlabel releases published
under CC from the start, gives real licences.

### Same day — YouTube upload behind a button

Closing the gap ADR 0001 left open. Chosen: `public`, a button rather than
automatic, and mimo's own title/description/hashtags.

`app/youtube.py` refreshes the token and does a resumable upload with plain
httpx — no `google-api-python-client`, since that is one POST and two requests.
The **privacy status is read back from the response instead of assumed**, which
turns the untested claim in ADR 0001 into a measurement: if an unaudited
project really does force uploads to private, the bot will say so on the first
upload rather than leaving someone to wonder why the video is not visible.

`scripts/youtube_auth.py` handles the one-time consent on the workstation:
stdlib only (no pip install), loopback redirect on :8765, `access_type=offline`
plus `prompt=consent` — without the latter Google omits the refresh token on
every authorisation after the first, which is a classic hour lost.

The button only appears when all three credentials are set, so the feature is
invisible until it is actually configured. Publishing stays behind a tap
because it is outward-facing and cannot be undone quietly, unlike everything
before it in the pipeline.

Vault keys `stacks.shorts_factory.youtube.{client_id,client_secret,refresh_token}`
exist but are empty, waiting on the Google Cloud setup, which needs the account
owner. 32/32 tests pass. The upload path itself is unrun — it cannot be
exercised until those credentials exist.

### Same day — YouTube credentials in place

First consent attempt failed with `Error 403: access_denied` — "the app is
currently being tested, and can only be accessed by developer-approved testers".
That is the Testing-status trap the script's docstring warns about, hit in its
other form: not the 7-day token expiry but an outright block. Publishing the app
(Google Auth Platform → Audience → Publish app) fixed it. Verification was not
needed; the unverified-app warning is clicked through once by the app's owner.

Also fixed the script's wait loop, which was a `while ... : pass` spin — now a
`threading.Event` with a 10-minute timeout.

Credentials are in the vault and deployed. Verified the refresh token by
exchanging it for an access token inside the container: it works. A follow-up
call to the channels endpoint returned `403 Insufficient Permission`, which is
the correct result — the grant is `youtube.upload` only, so reading channel
data is legitimately out of scope.

Still unproven: whether an upload actually lands, and what `privacyStatus`
YouTube applies to it. That needs a real upload, which stays a human button
press by design.

### Same day — the music mix was quietly attenuating the narration

Review caught what the earlier ducking measurement could not distinguish:
`amix` defaults to `normalize=1`, scaling every input by 1/n, so the presence of
*any* music dropped the voice by roughly 6dB. Comparing total RMS at the same
timestamps cannot tell "music ducked away" apart from "voice turned down and
music filling the hole" — both look like a small delta.

The test that separates them is a **silent** music track: any level change can
then only come from the filter chain. It measured -2.8dB and -5.5dB on the
narration. With `normalize=0`, whole-file RMS is -21.204dB without music against
-21.206dB with a silent track, and the peaks match too — the music path is now
an identity when there is nothing to mix.

Also turned off `alimiter`'s auto-level (`level=false`), which would otherwise
have made a clip with music louder than one without; peak protection was the
only thing wanted from it.

Two smaller fixes alongside: ADR 0003 still told readers to install
`fonts-noto-core` after the switch to Waree, which would have reintroduced the
tofu-box bug; and retiring the upload button used `editMessageText`, which
cannot touch a video message — that carries a caption, not text — so it now
uses `editMessageReplyMarkup`, which works on both.

## 2026-08-26 — thumbnail from the opening frame

After a successful upload the bot now grabs frame one of the clip and sets it
as the video's thumbnail. That frame is the hook card, which is the one screen
written specifically to stop a scroll, so it is already the right picture —
checked against a real clip rather than assumed: the opening frame shows the
hook text legible over its footage, not a black or mid-fade frame.

`thumbnails.set` accepts the `youtube.upload` scope already granted, so no
re-consent. Custom thumbnails do need a **phone-verified channel**; without one
YouTube answers 403. The video is already published by that point, so a
thumbnail failure is reported as a nuisance ("ตั้งเองใน Studio ได้") and never
as a failed upload.

Unrun, like the upload itself — it needs a real video id.

### 2026-08-26 — first upload, and what the thumbnail actually controls

First real upload: video `mOyx9mDhly8`, and it came back **`public`**. That
settles the open question behind ADR 0001 — an unaudited project did *not* have
its upload forced to private, at least for this account. `thumbnails.set`
returned success on the same run.

Then the cover looked wrong, and the investigation is worth keeping because the
code turned out to be correct:

- The JPEG sent is frame 0 exactly — re-extracting it from the finished clip and
  differencing gives a mean absolute difference of 0.00.
- The thumbnail YouTube actually serves
  (`i.ytimg.com/vi/<id>/maxresdefault.jpg`) **is** that image: the hook card over
  its footage, fitted into a 16:9 canvas with the sides filled by a zoomed,
  darkened copy. That letterboxing is why it reads as a different picture.
- **The Shorts feed ignores custom thumbnails entirely.** It picks its own frame,
  and there is no API for that cover — only the YouTube mobile app can set it,
  where the first option in the picker happens to be the opening frame.

So `thumbnails.set` still earns its place (search, channel page, suggestions),
but the Shorts cover cannot be automated. The upload message now says so and
tells the human the three taps, rather than leaving them to conclude the feature
is broken.

### 2026-08-26 — captions, upload history, performance feedback

Three additions, all free, all on demand — **no scheduler was added**, because
ADR 0002's "nothing needs to listen" is load-bearing for the whole shape. A
weekly digest would have broken it; `/stats` plus priming the prompt at
generation time gets the same value.

- **Captions.** `write_srt()` builds a subtitle track from the Card boundaries
  already computed for the video, so the timings cost nothing extra. It uses
  each Card's **raw** narration, not the `_speakable()` form: transliteration is
  right for the voice and wrong on screen, where "Docker" should read as
  "Docker". Attached after upload via `captions.insert`, which is a single
  multipart request rather than the resumable flow used for the video. The
  `.srt` is also kept beside the mp4 in `/volume1/shorts`.
- **History.** Every successful upload appends to `/data/history.json`. It is
  the only record of which videos are ours — YouTube is never enumerated — and
  it feeds the last 30 titles into the prompt as "already covered, do not repeat
  the same angle".
- **Performance.** `/stats` reports views and retention per uploaded clip,
  sorted by **percentage watched rather than views**, which for Shorts is the
  number that says whether the writing worked. The top three titles are fed
  into every subsequent generation as examples to write more like. Failure to
  fetch stats returns an empty list and never blocks writing a script.

Scopes were widened in one consent round (`youtube.force-ssl`,
`yt-analytics.readonly`). The old refresh token was kept until the new one was
proven — verified via `tokeninfo` that all three scopes were granted before
overwriting the vault. The YouTube Analytics API still needs enabling in the
Cloud project; it is separate from YouTube Data API v3 and currently answers
403.

### Same day — the bot froze twice, and the first fix was wrong

Two topics hung at "กำลังเขียนสคริปต์" with no error. The logs looked healthy —
`POST .../chat/completions "HTTP/1.1 200 OK"` — which is the trap: **httpx logs
that line when the headers arrive, not when the body finishes.** The connection
to mimo was still open 14 minutes later.

First fix set `timeout=180` on `AsyncOpenAI`, and it did not work. That value is
httpx's **per-read** timeout: a server trickling bytes resets the clock forever,
so it never fires. What was needed is a wall-clock deadline, so the call is now
wrapped in `asyncio.wait_for`.

Worth stating why this froze *everything* rather than one request: the Telegram
poll loop runs inline on the same task, so a hung model call takes the whole bot
down with it. The deadline bounds that; making generation concurrent with
polling would be the larger fix if it ever matters.

Verified afterwards against the topic that hung: a script came back in 73s with
six valid cards. Non-tech topics are handled fine by the prompt, so the freezes
were a mimo-side stall and nothing to do with the subject matter.

### 2026-08-26 (later) — why scripts kept failing, measured

Reports of frequent "เขียนสคริปต์ไม่สำเร็จ" and blank-looking covers. Both were
investigated rather than guessed at, and the script failures turned out to be
three separate causes stacked.

**The covers were never broken.** All six uploads have thumbnails served by
YouTube (60-115KB each), and downloading the newest one shows our own frame 0.
The grey tiles were the app's grid not having loaded. What *is* true is that the
Shorts feed and channel grid ignore custom thumbnails entirely, so setting one
only reaches search and suggestions. Per the request, `YOUTUBE_SET_THUMBNAIL`
now defaults to `false` — the code stays, the behaviour is opt-in.

**mimo is not down.** Benchmarked from the container: a trivial prompt answers
in 3-10s. The real prompt took 48-86s, and one run took 161s while burning
**10,457 completion tokens for 2,400 characters of output** — mimo-v2.5-pro is a
reasoning model and most of that is thinking. Latency therefore swings with how
long it chooses to think, and the tail was crossing the 180s deadline.

Three fixes, each measured:

1. `reasoning_effort="low"` — 79s / 3,796 tokens against 161s / 10,457 at the
   default, and the shorter run produced a *valid* script where the long one did
   not. `"minimal"` is rejected with a 400.
2. **A timeout now retries instead of failing.** The previous fix raised
   immediately on the deadline, so a single slow response lost the whole script
   even though the next attempt usually succeeds.
3. `HARD_MAX_CHARS_PER_LINE` 30 → 34, with the font floor 44px → 40px to match
   (34 wide Thai consonants measure 976px against 994px of usable width). The
   161s run failed validation on a 33-character line, and of three verification
   runs two produced longest lines of 30 and 31 — the old limit would have
   thrown away a third of otherwise good scripts.

Verified after: three real topics, 3/3 valid, 48-124s.

### 2026-08-26 (later) — Analytics API enabled; `/stats` is correct but early

With the API enabled the query works: channel totals return 75 views over 90
days and a per-video breakdown comes back fine. Filtering to the eight clips
this bot uploaded returns nothing, and the reason is visible in the day
dimension — **the most recent processed day is 2026-08-22 while today is the
26th.** YouTube Analytics runs a few days behind, and every clip in the history
was uploaded today, so there is genuinely nothing to report yet.

Since an empty result and a broken one look identical from Telegram, the empty
report now names the cut-off date ("ข้อมูลล่าสุด ... 2026-08-22"), fetched only
when there are no rows. Numbers should start appearing in a couple of days on
their own.

## 2026-08-27 — เสียงพูดไม่เป็นธรรมชาติ: ตัดความเงียบรอยต่อ card + บังคับทับศัพท์

**อาการที่แจ้ง:** พูดแล้วหยุดแปลกๆ ประมาณ 1 วิ กลางคลิป และพอเจอคำอังกฤษจะพูดรัวจนฟังไม่ทัน ไม่ชัด

**วัดของจริงก่อน** (สังเคราะห์สคริปต์คลิป 20260826-2028 ซ้ำในคอนเทนเนอร์ แล้ว `silencedetect`):

| ตัวคั่น card | SentenceBoundary | ความเงียบรอยต่อ |
| --- | --- | --- |
| `".\n\n"` (ของเดิม) | 1 อันต่อ card | 0.96-1.01 วิ |
| `"\n"` | 1 อันต่อ card | เท่ากันเป๊ะ |
| `". "` / `"."` / `", "` | ได้อันเดียวทั้งคลิป | 0.47 วิ |

สรุป: **ขึ้นบรรทัดใหม่** คือทั้งตัวจุด boundary และตัวที่ทำให้เงียบยาว (paragraph break)
ส่วนจุดไม่มีผลอะไรเลย. จังหวะหายใจปกติของเสียงนี้อยู่ที่ 0.12-0.53 วิ ดังนั้น 1.0 วิคือของแปลกปลอมจริง.
เอาตัวคั่นแบบอื่นไม่ได้ เพราะพอ boundary เหลืออันเดียว `narrate()` จะตีกลับแล้วหล่นไปทาง fallback
พูดทีละ card ซึ่งขาดกว่าเดิม.

**ที่แก้:**
- `render.tighten()` ตัดเสียงตาม boundary → เล็มหางเงียบเหลือ `JOIN_SILENCE` 0.30 วิ (`silenceremove`
  ต้อง `areverse` ครอบ เพราะมันทำงานกับหัวสตรีมอย่างเดียว) → ต่อกลับด้วย `concat_audio()` แบบ
  re-encode PCM (ถ้า `-c copy` mp3 จะลากส่วน padding ของ encoder เข้ามาที่รอยต่อ = เงียบกลับมาใหม่)
  แล้ววัด start ใหม่จากไฟล์ที่เล็มแล้ว ไม่เชื่อ offset ของ endpoint (มันยาวเกินจริง)
  - ⚠️ ต้อง seek ฝั่ง **input** (`-ss` ก่อน `-i`) เพราะ `-ss/-to` ฝั่ง output ทำงานหลัง filter chain
    ที่ `areverse` สลับ timestamp ไปแล้ว → ตัดไม่โดน เงียบสนิท ไม่ error (เสียเวลาไปหนึ่งรอบ)
  - วัดหลังแก้: รอยต่อ 0.92/0.87/0.90 → 0.39/0.43/0.42 วิ, คลิป 23.35 → 21.36 วิ
- การ์ดมี narration 2 ชุด: `narration` (คำอังกฤษคงไว้ → ขึ้นซับ) กับ `spoken` (ทับศัพท์ไทยล้วน →
  ให้เสียงอ่าน). `validate()` ตีกลับถ้า `spoken` มีอักษรละติน (retry loop ของ `generate()` แก้เอง).
  โมเดลเมินกฎทับศัพท์ในพรอมป์มาตลอด — คลิปนั้นเหลือ "Short Vertical Drama", "cliffhanger",
  "Netflix" ในบทพูด ซึ่งคือต้นเหตุที่พูดรัว/ไม่ชัด (เสียงสลับไปสำเนียงอังกฤษกลางประโยค)
- เทสต์: `test_card_joins_lose_their_dead_air` (tone-silence-tone ยืนยันว่าเงียบเหลือ 0.30 และ
  จำนวนช่วงพูดไม่หาย), `latin-in-spoken` / `missing-spoken` ใน parametrize เดิม

**ที่ไม่ได้แก้:** `TTS_RATE` ยัง `+10%` เท่าเดิม (ทดสอบ +12% ลดรอยต่อได้แค่ 1.01→0.90 เทียบกับ
0.40 ที่ได้จาก tighten — ปล่อยไว้เป็นปุ่มให้คนหมุนเอง)

## 2026-08-27 (2) — ออกแบบ learning loop: สัมภาษณ์ + เอกสาร ยังไม่แตะโค้ด

**โจทย์:** อยากให้ระบบเรียนรู้จากยอดวิว/ไลค์/จุดที่คนหนี แล้วแนะนำคลิปถัดไป พร้อมทำ
experiment เพื่อไม่ให้ติดอยู่กับอดีตตัวเอง (เผื่ออนาคตต่อยอดไปคลิปยาว)

**วัดของจริงก่อนออกแบบ** (Data API + Analytics API):
- 9 คลิป อัปภายใน 19 ชม. · views 182/7/5/3/3/3/2/1/0 · รวม 206 · **มัธยฐาน 3**
- likes 5 อันทั้งหมดอยู่บนคลิปเดียว และคลิปนั้นคือ "การเงิน" ซึ่งอยู่นอกบรีฟ DevOps/AI
- `analytics.performance()` คืน `[]` — ยังไม่มีแถวระดับวิดีโอเลย
- ช่องนี้**ไม่ใช่ช่องใหม่**: 197 วิดีโอ, 1,396 views สะสม, 8 subs — 9 คลิปของบอทเป็นส่วนน้อย
- traffic ตั้งแต่ 1 ส.ค.: `SHORTS` 382, `YT_CHANNEL` 48, `YT_SEARCH` 8, อื่นๆ ~20 —
  **ทั้งหมดเป็นของวิดีโอเก่าที่คนทำเอง** ไม่มีคลิปบอทโผล่ใน top-videos เลย. คลิปเก่าพวกนั้น
  retention 44-86% = มี baseline ให้เทียบ (แต่คนละฟอร์แมต ห้ามเอาไปเป็นตัวอย่างให้บอทเลียนแบบ)
- endpoint รับ `audienceWatchRatio`/`relativeRetentionPerformance` (200, schema ถูก, rows ว่าง)

**ข้อสรุปที่ตกลงกัน:** ระบบ**บันทึกทุกอย่างแต่ห้ามสรุป**จนกว่าจะผ่าน Gate
(10 คลิป + 300 views ต่อ Variant, 30 คลิปรวม). ปิด `winning_examples()` ก่อน เพราะตอนนี้
มันป้อน top-3 จากตัวอย่าง ~3 views เข้าพรอมป์ทุกครั้งอยู่แล้ว. การทดลองเป็น between-clip
สุ่มต่อคลิป (ไม่ใช่ A/B/A/B เพราะผูกกับวัน/เวลาโพสต์ และไม่ใช่ bandit เพราะจะล็อกผู้ชนะปลอม).
บันทึก Script ที่**ไม่ได้อัป**ด้วย ไม่งั้นการที่คนทิ้งคลิปห่วยจะทำให้ variant แย่ดูดีเท่าตัวดี.
วัดที่ **วันที่ 7** เสมอ. เอกสาร: `docs/adr/0004`, ศัพท์ใน root `CONTEXT.md`,
แผน 6 ขั้นที่ `.notes/plan-learning-loop.md`. แก้ถ้อยคำ ADR 0002 (เดิมอ้างว่า "ไม่มี scheduler")

**ยังไม่ยืนยัน:** Shorts มีเส้น retention ต่อวินาทีผ่าน API ไหม — rows ว่างตอนนี้ตีความได้
ทั้ง "ยังไม่ประมวลผล" และ "ไม่รองรับ" → แผนข้อ 4 คือไปเช็คก่อนสร้าง ไม่สร้างบนสมมติฐาน

## 2026-08-27 (3) — learning loop ขั้น 1-2: Manifest + ปิดการเรียนจากเสียงรบกวน

- `app/manifest.py` — 1 ไฟล์ต่อ Topic ที่ `/data/clips/<id>.json` เก็บ draft **ทุกรอบ**
  (revision ต่อท้าย ไม่ทับ), outcome (`rendered`/`discarded`/`render_failed`),
  `published`+`video_id`, และ `render` details. เขียนแม้กดทิ้ง — เพราะถ้าเก็บเฉพาะตัวที่รอด
  สายตาคน variant ที่เขียนห่วยจะดูดีเท่าตัวที่เขียนดี (ADR 0004). เขียนไม่ได้ = log แล้วไปต่อ
  ห้ามล้ม render
  - id ใช้ **มิลลิวินาที** ไม่ใช่วินาที — เทสต์จับได้ว่า 2 manifest ในวินาทีเดียวกันทับกันเงียบๆ
- `render.build()` คืน `(clip, details)` — voice/rate/pitch/`JOIN_SILENCE`/bgm/ความยาว/
  start+seconds+มี footage ไหมต่อ card. workdir ถูกลบทุกครั้ง อะไรไม่ส่งกลับตรงนี้คือหายถาวร
- `analytics.gate_note()` + `winning_examples()` คืน `[]` จนกว่าจะครบ 30 คลิป และ `/stats`
  ขึ้นคำเตือนว่ายังห้ามใช้ตัดสินใจ (ยืนยันบนเครื่องจริง: "มี 9/30 คลิป")
- **บั๊กเดิมที่เจอระหว่างทาง**: `do_render()` เคลียร์ `state["topic"]` ใน `finally` ก่อนที่ปุ่ม
  upload จะทำงาน → `history.record()` บันทึก topic เป็น `""` มาตลอด (ยืนยัน: ทั้ง 9 คลิปว่าง).
  ย้ายไปเคลียร์หลังอัปเสร็จแทน
- **บั๊กที่สองที่เจอตอนรีวิว**: ปุ่ม upload อยู่ยาวข้าม Topic — ถ้า render A แล้วส่งหัวข้อ B
  ก่อนกดอัป A, `do_upload()` จะอ่าน `state["clip_id"]` ที่กลายเป็นของ B ไปแล้ว →
  Manifest B โดนประทับ `video_id` ของ A ส่วน A ค้างเป็น `published: false`. แก้ด้วยการ
  snapshot `last_clip_id`/`last_topic` ตอน `deliver()` (แพตเทิร์นเดียวกับ `last_clip`/
  `last_script` ที่มีอยู่แล้วด้วยเหตุผลเดียวกัน) มีเทสต์ไล่ผ่าน state machine จริงคุมไว้
- deploy + `pytest` ในอิมเมจจริง: **55 passed** · ยิง `render.build()` จริงยืนยัน details
  ที่คืนกลับมา (`bgm`, `rate: +10%`, start/seconds ต่อ card)

## 2026-08-27 (4) — learning loop ขั้น 3: snapshot รายวัน + backfill

- `app/snapshots.py` — ดึง views/likes/shares/comments/subs/percent/seconds/minutes ของคลิป
  ที่อัปแล้วอายุ 0-30 วัน เขียน snapshot ลง manifest วันละครั้งหลัง `SNAPSHOT_HOUR` (default 10)
  - **ขี่ poll loop ไม่ใช้ scheduler thread** — `getUpdates` ตื่นทุก 30 วิอยู่แล้ว ไม่ต้องเพิ่ม
    dependency ไม่เปิดพอร์ต (ADR 0002 ยังยืน, ADR 0004 อธิบายไว้)
  - `add_snapshot()` ใช้ **วันที่เป็นคีย์** รันซ้ำวันเดียวกันทับของเดิม ไม่นับซ้ำ
  - `manifest.day7()` = snapshot แรกที่อายุ ≥7 วัน — ตัวเลขทางการของ Experiment
  - `/snapshot` สั่งมือได้
- `app/backfill.py` — กู้ manifest ของ 9 คลิปที่อัปก่อนมี manifest จาก `.txt`/`.srt` ใน `/output`
  (title + ขอบเขต card รอด, ที่เหลือตายไปกับ workdir) ติดธง `reconstructed: true`
  รันตอน startup, idempotent
- **ยืนยันบนเครื่องจริง**: backfill สร้าง 9 manifest (5-7 card ต่อคลิป ความยาว 27-45 วิ),
  snapshot job รัน `wanted 9` ยิง API ผ่าน (200) แต่ `rows []` — YouTube ยังไม่ประมวลผลระดับ
  วิดีโอ จะทยอยเข้ามาเองในไม่กี่วัน
- `pytest` ในอิมเมจที่ deploy: **61 passed**

**รีวิวจับเพิ่ม 3 จุด (แก้แล้วทั้งหมด):**
- ยืนยัน field mapping ของ `_rows()` กับข้อมูลจริง — ยิงคลิปเก่าที่รู้ค่าอยู่แล้ว
  (`v7ljwc_6_jM`) ได้ `[..., 361, 3, 0, 0, 1, 66.09, 13, 15]` = `row[1]` views,
  `row[6]` percent ตรงตามที่ index ไว้ (ก่อนหน้านี้ยังไม่เคยมีแถวจริงวิ่งผ่าน loop เลย)
- `list(wanted)[:MAX_VIDEOS]` เรียงจาก**เก่าไปใหม่** → พอผลิต 3 คลิป/วัน หน้าต่าง 30 วันจะมี
  ~90 คลิปชนเพดาน 50 แล้วคลิป**ใหม่สุด 40 อันจะเงียบหายไป** ซึ่งคือกลุ่มที่ยังขยับและยังไม่ได้
  เก็บ day-7. แก้เป็นเรียงจากใหม่ไปเก่า (50 อัน ≈ 16 วันที่ 3 คลิป/วัน ครอบ day-7 พอดี)
- `take_snapshots()` เดิม stamp `last_snapshot` เฉพาะตอนสำเร็จ → ถ้า refresh token ตาย
  (เคสที่ ADR 0001 เขียนไว้) `due()` จะจริงตลอด แล้วยิง token+Reports **ทุก 30 วิทั้งวัน**
  (~2,900 ครั้ง/วัน). ย้ายไป `finally` — เสียไปวันนึงไม่เป็นไรเพราะ `day7()` เอาอันแรกที่อายุ ≥7

## 2026-08-27 (5) — learning loop ขั้น 5: variant + experiment

- `app/experiment.py` — factor `hook` 2 variant (`shock_number` / `question`)
  - `assign()` สุ่มตอนเปิด Topic **ก่อนเขียนสคริปต์** เขียนลง manifest แล้ว**ไม่ re-roll**
    ตอนสั่งแก้ ไม่งั้นการที่คนสั่งเขียนใหม่จนพอใจ = เลือกผู้ชนะเงียบๆ
  - explore 1 ใน 3 (`EXPLORE_RATE`) ติดธง ไม่เข้าสมการใดๆ — ถ้าเอามารวมคือวัดการแหกแพตเทิร์น
  - เก็บ **ข้อความ clause แบบคำต่อคำ** ลง manifest เพราะพรอมป์หลักจะเปลี่ยนไปเรื่อยๆ
    ชื่อ variant เฉยๆ ไม่บอกว่าวันนั้นมันแปลว่าอะไร
  - `verdict()` ฟันธงต่อเมื่อ **ทั้งสอง arm ครบ 10 คลิป + 300 views** และ median ต่าง ≥5 จุด
    ไม่งั้นตอบ "สรุปไม่ได้" หรือ "เสมอ" — ใช้ **day-7 เท่านั้น** ไม่ใช่ค่าล่าสุด
  - `/experiment` รายงาน: จำนวนคลิป · อัตราทิ้ง · views · median day-7 ต่อ variant
- `script_gen.generate(style=...)` ต่อ clause เป็น system message เพิ่ม ไม่ยัดใน SYSTEM_PROMPT
- ยืนยันบนเครื่องจริง: `/experiment` ขึ้นคำเตือน gate ก่อนตัวเลข + ปฏิเสธฟันธง,
  สุ่ม 9 ครั้งได้ explore 2 ครั้ง variant กระจายทั้งสองฝั่ง
- `pytest` ในอิมเมจที่ deploy: **70 passed**

**รีวิวจับเพิ่ม 3 จุด (แก้แล้ว):**
- **manifest ที่ยังไม่เคยกลายเป็นคลิปถูกนับเป็นคลิป** — `manifest.start()` ตั้ง
  `outcome: "drafting"` แล้วถ้า `generate()` พัง main ไม่เคยแตะ manifest อีกเลย →
  `tally()` นับเข้า arm ทั้งที่ไม่มีสคริปต์ ทำให้ (ก) arm ผ่านเกณฑ์ 10 คลิปได้ด้วยของที่ไม่มีอยู่จริง
  (ข) อัตราทิ้งเพี้ยนกลับด้าน เพราะ failure ไปโป่งตัวหาร. แก้: นับเฉพาะ `rendered`/`discarded`,
  ที่เหลือเข้าช่อง `failed` แยก + main เขียน `outcome: "generate_failed"` ตอนเขียนสคริปต์ไม่สำเร็จ
  (เฉพาะตอนหัวข้อใหม่ ถ้าเป็นการสั่งแก้ต้องไม่แตะเพราะสคริปต์เดิมยังรอรีวิวอยู่)
- **id ชนกันได้จริง** — เทสต์ backfill idempotent จับได้ว่า `manifest.start()` 2 ครั้งใน
  มิลลิวินาทีเดียวกัน (backfill วนลูปถี่มาก) ได้ id เดียวกัน อันหลังทับอันแรกเงียบๆ
  แก้เป็นวนเพิ่ม suffix จนกว่าจะว่าง + เทสต์เปิด 20 อันรวดยืนยันไม่ชน
- `views` ในรายงานรวมจาก snapshot day-7 เท่านั้น คลิปอายุ <7 วันจะขึ้น 0 เสมอ →
  เปลี่ยนป้ายเป็น `views (day-7)` กันเข้าใจผิดว่า job พัง
- **เทสต์ที่ขาดไป**: เพิ่มตัวไล่ผ่าน state machine จริงว่าการ**สั่งแก้ไม่ re-roll variant**
  (บั๊ก 2 ตัวก่อนหน้าอยู่ตรง wiring แบบนี้ทั้งคู่ unit test มองไม่เห็น)
- `pytest` ในอิมเมจที่ deploy: **73 passed**

## 2026-08-27 (6) — learning loop ขั้น 4: เส้น retention + หาจุดที่คนหนี

**ตอบคำถามที่ค้างใน ADR 0004 ได้แล้ว: Shorts มีเส้น retention จริง**
- ยิงกับวิดีโอเก่าของช่องเองที่มีคนดูพอ: `v7ljwc_6_jM` (PT21S, 361 views) คืน **100 แถว**
  ของ `audienceWatchRatio`+`relativeRetentionPerformance` ต่อ `elapsedVideoTimeRatio` (ละเอียด 1%)
- อีก 2 คลิป (27 และ 12 views) คืน 0 แถว → **ประตูคือยอดวิว ไม่ใช่ฟอร์แมต** นี่คือเหตุผลที่
  ยิงกับคลิปบอทตอนแรกแล้วตอบไม่ได้ (ยังไม่มีใครดู)
- ทำไมต้องเก็บ card start ใน manifest: API ให้ค่ามาเป็น**สัดส่วนของคลิป** ต้องคูณความยาวจริง
  ถึงจะรู้ว่าวินาทีนั้นการ์ดไหนอยู่บนจอ

**ที่ทำ:** `app/retention.py` — ดึงเส้น, หา cliff (ชันกว่า step ปกติของคลิปนั้น 2 เท่า **และ**
อย่างน้อย 5% ของความสูงเส้น), รวม bucket ที่ติดกันเป็น cliff เดียว, map กลับเป็น card,
วาด PNG ด้วย Pillow (มีอยู่แล้ว ไม่เพิ่ม dep) ส่งเข้า Telegram ด้วย `sendPhoto`. `/retention`

**หลุมที่เจอตอนทดสอบกับของจริง:**
- เกณฑ์แรกใช้ median ของ "ช่วงที่ตกเท่านั้น" → คลิปที่มี cliff เดียวจะถูกเทียบกับตัวเอง
  แล้วไม่มีวันเข้าเกณฑ์ (เทสต์จับได้) แก้เป็น median ของทุก step + พื้นฐานขั้นต่ำ
- cliff เดียวกินหลาย bucket → ป้ายบนกราฟทับกันจนอ่านเป็น "90007" แก้ด้วยการรวม cluster
- เดิม `fetch()` เปิด client เอง → เดินหาทีละคลิปกลายเป็น refresh token ทุกคลิป
  แก้เป็นรับ client มาใช้ร่วม + จำกัดที่ 10 คลิปล่าสุด
- Reports API ตอบ **500** รายคลิปเป็นครั้งคราว เดิมทำให้ทั้งคำสั่งพังพร้อม JSON ดิบยาวเหยียด
  → ถือเป็น "คลิปนี้ยังไม่มีเส้น" แล้วเดินต่อ
- `pytest` ในอิมเมจที่ deploy: **78 passed** + วาดกราฟจากข้อมูลจริงแล้วเปิดดูด้วยตา

**รีวิวจับเพิ่ม (แก้แล้ว):** `render.build()` คืน card details ที่มีแต่ `start/seconds/footage`
ไม่มี `narration` → `/retention` ของคลิปใหม่ทุกอันจะขึ้น "card 2 — " ว่างเปล่า (คลิปเก่าที่ backfill
มาจาก srt กลับมีข้อความ) = ฟีเจอร์นี้จะใช้ไม่ได้กับคลิปที่สนใจจริงๆ. ใส่ narration เข้าไปใน details
ยืนยันด้วยการรัน `build()` จริง. เพิ่มเทสต์ว่าคำสั่ง (`/retention` ฯลฯ) ที่พิมพ์ตอนสคริปต์รอรีวิว
ต้องไม่ถูกส่งเป็น feedback ให้โมเดล — **79 passed**

## 2026-08-27 (7) — /help

เพิ่ม `/help` (และ `/start` เป็น alias) อธิบาย `/stats` `/snapshot` `/experiment` `/retention`
เป็นภาษาไทย พร้อมบอกเหตุผลสั้นๆ ว่าทำไมถึงเรียงตาม % ไม่ใช่ยอดวิว, ทำไมใช้ตัวเลขวันที่ 7,
ทำไมมีคลิป "ลองของใหม่" 1 ใน 3 และทำไมบางคลิปไม่มีกราฟ retention
- **ไม่ใช้ `parse_mode`** — legacy Markdown ของ Telegram ไม่มี `**` และจะตอบ 400 ทำให้
  หน้า help เข้าไม่ถึงเลย ตัดสัญลักษณ์ออกให้เป็น plain text แทน
- เทสต์ไล่อ่าน `main.py` หาทุก `text.startswith("/...")` แล้วเช็คว่ามีอธิบายใน `HELP` ครบ —
  เพิ่มคำสั่งใหม่แล้วลืมเขียน help จะเทสต์แดงทันที
- **80 passed**

## 2026-08-27 (8) — /trends + ปลดล็อกหัวข้อ

**ที่ตรวจมาก่อนตัดสินใจ** (ยิงจากในคอนเทนเนอร์จริง):
- Google Trends **ใช้ได้** — endpoint เก่า `dailytrends` ตายแล้ว (404) ตัวที่มาแทนคือ
  `https://trends.google.com/trending/rss?geo=TH` ตอบ 200 พร้อม `approx_traffic` + หัวข้อข่าว
- YouTube `chart=mostPopular&regionCode=TH` ใช้ได้ + ดึงชื่อหมวดภาษาไทยได้ 14 หมวด
- HN Algolia 200, GitHub search 200, **Reddit 403**
- ค้น YouTube ไทยสาย DevOps: `devops ไทย` = **0 ผลลัพธ์ใน 30 วัน**, `kubernetes สอน` = 1 คลิป
  123 views → **นิชที่ล็อกไว้ไม่มีคนดู** ล็อกต่อ = Gate ไม่มีวันถึง → ปลดล็อกหัวข้อ (แก้ ADR 0004)

**ที่ลง:** `app/trends.py` (2 แหล่ง, แหล่งไหนพังก็ข้าม ไม่ล้ม) + `script.suggest_topics()`
+ `/trends` (ส่งของดิบมาด้วยเสมอ เพื่อจับได้เวลาโมเดลมั่ว) + `trend_origin()` ติดป้ายใน manifest
ว่าหัวข้อนี้มาจาก trend ไหน (spike/evergreen/หมวด) + `category` เป็นฟิลด์บังคับใน Script

**เรื่องที่ต้องบอก: Q1 ที่แนะนำไว้ (สลับ factor เป็น `category`) ทำไม่ได้จริง** — คนเลือกหัวข้อเอง
การสุ่มหมวดจะถูก override ทันที ซึ่งการสุ่มคือสิ่งเดียวที่ทำให้เป็นการทดลอง →
ทำเป็น `by_category()` อ่านแบบ**สังเกตการณ์** ติดป้ายไว้ในรายงานว่าไม่ใช่การทดลอง
factor ที่สุ่มจริงยังเป็น `hook` เหมือนเดิม

**กันบอทแต่งเรื่องคนจริง:** ตัดหมวด YouTube 25 (ข่าว/การเมือง) กับ 17 (กีฬา) ทิ้ง + พรอมป์ห้าม
หัวข้อทรงข่าวลือ. **ทดสอบจริงรอบแรกโมเดลเสนอ "โทบี้ แม็กไกวร์กลับมาจริงไหม"** — เข้าข่ายพอดี
เลยเติมกฎห้ามคาดเดาเรื่องคนจริงพร้อมตัวอย่าง รอบสองสะอาด (ชิป M6 / Witcher 3 Remastered /
ขีปนาวุธ F-35 / ระบบ UCL 36 ทีม / ซีรีส์ข่มขลัง) และ "นายกเฮง" กับ "ไทย VS เกาหลีใต้" ถูกทิ้งเอง
- **83 passed**

**รีวิวจับเพิ่ม (แก้แล้ว):**
- `category` เป็นฟิลด์บังคับใน `validate()` แต่**ยังไม่เคยยิง `generate()` จริงหลังเพิ่ม** —
  ถ้าโมเดลไม่ใส่มาจะเผา retry แล้วเสียสคริปต์ทั้งอัน (รีโปนี้มี commit "stop losing scripts"
  มาแล้ว). ยิงจริง 3 หัวข้อ: ได้ `'เทค'`, `'สุขภาพ'`, `'สุขภาพ'` ครบตั้งแต่รอบแรก ไม่มี retry
- **เอกสารเคลมเกินโค้ด**: เขียนไว้ว่ากรอง 2 ชั้น แต่ฝั่ง Google Trends **ไม่มีฟิลด์หมวด**
  เลยไม่ได้กรองอะไรเลย — `นายกเฮง` กับแมตช์วอลเลย์บอลเข้าไปถึงโมเดล (โมเดลตัดเอง)
  แก้ถ้อยคำใน ADR + README ให้ตรง: ฝั่งนั้นมีชั้นเดียว คือพรอมป์ + คนเลือก
  ซึ่งคือเหตุผลที่ต้องพิมพ์ของดิบออกมาเสมอ
- `state["suggested"]` ไม่เคยถูกล้างและอยู่ใน `state.json` ข้ามรีสตาร์ท → อีกเดือนหนึ่งหัวข้อที่
  บังเอิญขึ้นต้นเหมือนกัน 16 ตัวอักษรจะถูกเครดิตให้ trend เก่า = ทำลายฟิลด์เดียวที่มีไว้ตอบว่า
  "หัวข้อจาก trend ดีกว่าไหม" → ใส่ `suggested_at` + อายุ 2 วัน
- `BLOCKED_CATEGORIES` ไม่มีเทสต์ทั้งที่ ADR เรียกว่าเรื่องความเสี่ยง → แยกเป็น `trends.keep()`
  แล้วเทสต์ตรงๆ
- **85 passed**

## 2026-08-27 (9) — "mimo ไม่ตอบภายใน 240 วินาที" — วัดจริงแล้วแก้

**อาการ:** หัวข้อ F-35 พังติดกัน 3 ครั้ง (14:41, 15:22, 15:48) รอบละ 8 นาที

**ไม่ใช่บั๊กเรา และไม่ใช่ mimo ล่ม** — วัด 5 ครั้งจากในคอนเทนเนอร์:

| เวลา | completion tokens |
| --- | --- |
| 93s | 3,092 |
| 112s | 4,016 |
| 197s | 7,010 |
| 207s | 5,415 |
| **347s** | **10,585** |

**เวลาแปรตามจำนวน token ที่โมเดลคิด ~30 tokens/วินาที ไม่ได้ค้างมั่ว** เพดาน 240 วิ
ตัดรอบที่คิดยาวทิ้ง แล้ว retry อีกรอบ = เงียบ 8 นาทีแล้วได้ error

**ทางที่ลองแล้วทิ้ง — streaming + idle timeout**: อ่านคำตอบเดียวกันแบบ stream ใช้ **400 วิ**
เทียบกับ **137 วิ** แบบไม่ stream. idle detection ที่ได้มาไม่คุ้มเลยเพราะ endpoint นี้
ไม่เคยเงียบ มันคิดอยู่ (deploy ไปแล้วรอบหนึ่ง เห็นรอบจริงยาวเกิน 12 นาที เลยถอยกลับ)

**ที่ลงจริง:**
- เพดานรวม 600 วิ (`MIMO_TIMEOUT_SECONDS`) และ **แชร์ข้ามความพยายามทั้ง 2 ครั้ง** ไม่ใช่ครั้งละ 600
  → timeout จึงไม่ถูก retry (หมด budget ไปแล้วโดยนิยาม) ส่วนสคริปต์ผิดกติกายัง retry ได้
- **ย้าย make_script / do_render / do_upload / on_trends ออกจาก poll loop** (`spawn()`)
  บอทตอบคำสั่งอื่นได้ระหว่างรอ + รับงานทีละชิ้น (mode `writing`/`rendering` กันชนกัน)
  ก่อนหน้านี้ระหว่าง render บอทเงียบสนิททั้งตัว
- crash กลางทางตอน `writing` รีเซ็ต state เหมือนกรณี `rendering`

**พิสูจน์:** หัวข้อเดิมที่พังซ้ำ 3 ครั้ง ตอนนี้ **OK 283 วินาที · 6 cards** — เกินเพดานเก่า 240
แต่อยู่ใต้ 600 · `pytest` ในอิมเมจที่ deploy: **85 passed**

## 2026-08-27 (10) — mimo ค้างเป็นช่วงๆ ไม่ใช่ "คิดนาน" อย่างเดียว

รอบ 19:25 พังอีกที่ 600 วิ. log บอกชัด: **`200 OK` มาที่ 19:25:45 แล้วเงียบยาว 10 นาที**
= header มาแล้วแต่ body ไม่มา ซึ่งคนละอาการกับ "คิดนาน" ที่วัดไว้ตอนแรก (93-347 วิ)

**ไล่ทดสอบต่อ:**
- คำขอเล็ก (`max_tokens=20`) ตอบใน 9-26 วิ, `models.list()` ตอบทันที → endpoint ไม่ได้ล่ม
  (หมายเหตุ: `max_tokens` เล็ก ได้ `content=''` เพราะ reasoning tokens กินโควตาหมดก่อน
  → **ห้ามใช้ max_tokens คุมเวลา**)
- ยิงหัวข้อเดิมพร้อม hedge แล้ว **ค้างทั้งคู่** จนครบ 600 วิ
- อีก 2 ชั่วโมงต่อมา หัวข้อเดิมเป๊ะ: `mimo-v2.5` **149 วิ**, `mimo-v2.5-pro` **137 วิ**
→ เป็น **tail latency เป็นช่วงๆ ของฝั่ง mimo** ไม่ใช่พรอมป์เรา ไม่ใช่หัวข้อ

**ที่ลง:** hedge — ครบ `HEDGE_AFTER` 240 วิแล้วยิงคำขอที่สองคู่ไปเลย ใครตอบก่อนใช้คนนั้น
และ**คำขอที่สองยิงไปที่ `mimo-v2.5` (`MIMO_FALLBACK_MODEL`) ไม่ใช่ pro ตัวเดิม** เพราะทดสอบแล้วว่า
ยิง pro ซ้ำสองครั้งค้างพร้อมกันทั้งคู่ (episode เดียวกัน) ส่วนตัวเล็กเขียนสคริปต์เดียวกันได้ใน 149 วิ

**88 passed** ในอิมเมจที่ deploy

## 2026-08-28 (1) — ขีดกลางใน spoken = เงียบ 1 วิ กลางประโยค

**อาการ:** คลิปล่าสุดพูดว่า "เอฟ-35" แล้วหยุดเงียบราว 1 วินาทีตรงขีด ฟังเป็นสองคำแยกกัน
ทั้งที่ควรอ่านติดกันเป็น "เอฟสามสิบห้า"

**สาเหตุ:** `validate()` กันเฉพาะอักษรละตินใน `spoken` (`LATIN = [A-Za-z]+`) ขีดกลางกับตัวเลข
ผ่านฉลุย พอส่งเข้า edge-tts มันอ่านขีดกลางเป็นจังหวะหยุด ไม่ใช่ส่วนหนึ่งของคำ

**ที่ลง:**
- `render._speakable()` ตัดขีดกลางทุกแบบ (`-`, U+2010..U+2015) พร้อมช่องว่างรอบๆ ทิ้งก่อนส่ง TTS
  แก้ที่นี่ที่เดียวครอบทั้งสองทาง — `narrate()` (ยิงทีเดียวทั้งคลิป) และ `speak()` (fallback รายการ์ด)
  ล้วนผ่าน `_tts_text()` → `_speakable()`
- พรอมป์ห้ามใส่ขีดกลางใน `spoken` เพิ่มตัวอย่าง F-35 → เอฟสามสิบห้า, GPT-4 → จีพีทีโฟร์

ไม่เพิ่มกฎใน `validate()` เพราะตีกลับทั้งสคริปต์เพื่ออักขระตัวเดียวคือเสียเวลารอ mimo อีกรอบ
ทั้งที่ล้างเองได้ตอนอ่าน (ซับบนจอยังใช้ `narration` ต้นฉบับ ขีดกลางบนจอยังอยู่ครบ)

## 2026-08-28 (2) เลือกหัวข้อจากลิสต์ /trends ด้วยปุ่ม

**ที่มา:** ลิสต์จาก `/trends` บอกให้ "พิมพ์หัวข้อที่ชอบกลับมา" ซึ่งแปลว่าต้องพิมพ์
ประโยคไทยยาวๆ ใหม่ทั้งอัน

**ที่ลง:** ข้อความหัวข้อที่น่าทำแนบ inline keyboard ปุ่มเลข 1..n (`topics_keyboard()`)
กดแล้วเข้า `on_pick()` ยิง `make_script()` ด้วย **ข้อความหัวข้อคำต่อคำ** — `trend_origin()`
จึงยัง match ได้ ต้นทาง trend ไม่หาย พิมพ์เองยังใช้ได้เหมือนเดิม

**จุดที่ต้องระวัง (ทำแล้ว):**
- `callback_data` = `pick:<suggested_at>:<index>` — ลำพัง index ไม่มีความหมาย
  สั่ง `/trends` สองรอบแล้วกดปุ่มของข้อความเก่า index จะชี้เข้าลิสต์ใหม่ = เขียนหัวข้อที่
  ไม่มีใครเลือก. `picked()` เทียบ timestamp กับ `state["suggested_at"]` ไม่ตรง = ตีกลับ
  ("ลิสต์นี้เก่าแล้ว") ขนาดยังไม่ถึงเพดาน 64 ไบต์ของ Telegram
- กดตอน `mode == "review"` = ปฏิเสธ ให้กด 🗑 ก่อน ไม่งั้นสคริปต์ที่ค้างจะถูกทิ้งโดยไม่มี
  `outcome` ค้างเป็น record เปล่าใน manifest
- กดตอน busy = ตอบเหมือน path ข้อความ
- `on_callback` เช็ค prefix `pick:` **ก่อน** guard เดิม (guard นั้นตัดทุก callback ที่
  ไม่ใช่ upload เมื่อ mode ไม่ใช่ review — pick มาตอน idle จะโดนกลืนเงียบ)

**เทส:** เพิ่ม 2 เคส (ปุ่มของลิสต์เก่าโดนตีกลับ / หัวข้อที่กดยังได้ trend origin)
`84 passed, 7 failed` บน macOS — 7 เคสที่ fail คือ Raqm ไม่มีในเครื่อง (ADR 0003)
ไม่เกี่ยวกับงานนี้ ในอิมเมจผ่านหมด. `picked()` ใช้เพดานอายุ `SUGGESTION_LIFETIME`
ตัวเดียวกับ `trend_origin()` — เกินแล้วปุ่มตีกลับ ไม่ใช่เขียนคลิปที่ไม่มีช่อง trend

## 2026-08-29 — "เขียนสคริปต์ไม่สำเร็จ: mimo ไม่ตอบภายใน 600 วินาที" ที่ mimo ไม่ได้พัง

**อาการ:** 07:45 กดเลือกหัวข้อจากลิสต์ `/trends` → 07:55 บอทตอบ "mimo ไม่ตอบภายใน 600 วินาที"

**ของจริงจาก log (`docker logs shorts-factory | grep -v api.telegram.org`):**

```
07:49:05 WARNING mimo-v2.5-pro ยังไม่ตอบใน 240 วินาที ยิงคำขอสำรอง...
07:54:48 WARNING mimo-v2.5-pro ยังไม่ตอบใน 240 วินาที ยิงคำขอสำรอง...
07:55:05 ERROR   mimo ไม่ตอบภายใน 600 วินาที
```

hedge warning **สองครั้ง = `_say()` ถูกเรียกสองครั้ง = รอบแรกได้คำตอบกลับมาแล้ว**
(timeout ไม่มีวัน retry ได้ เพราะ budget แชร์กัน — หมดเวลาแปลว่าใช้ครบ 600 แล้ว
`left < MIN_ATTEMPT` แล้ว break). ไล่เวลา: รอบแรกยิง 07:45:05 hedge +240 = 07:49:05,
คำตอบมา 07:50:48 (343 วิ) → `validate()` ตีกลับ → รอบสองเริ่ม 07:50:48 เหลือ 257 วิ
hedge +240 = 07:54:48 เหลือ slice 17 วิ → หมดเวลา 07:55:05 ตรงเป๊ะทุกจุด

**สาเหตุจริง: สคริปต์รอบแรกผิดกติกา** ไม่ใช่ mimo พัง. ข้อความที่บอทส่งโกหกเพราะ
`last_error` ถูก timeout เขียนทับ และ branch `except ScriptError` ไม่ log อะไรเลย

**แก้ (`app/script.py`):**
- `except ScriptError` → `logger.warning` บอกว่า validate ตีกลับด้วยเหตุอะไร + ความยาว raw
- timeout ไม่ลบ error เดิมทิ้ง ต่อท้าย " — รอบก่อนหน้า: ..." แทน
- `once()` log **model / วินาที / completion_tokens / tokens ต่อวินาที** ตอนสำเร็จ —
  นี่คือตัวแยก "คิดนาน" ออกจาก "endpoint พัง": คิดปกติ ~30 tokens/วินาที ไม่ว่ายาวแค่ไหน
  ส่วนคำขอที่ค้างจะไม่ถึงบรรทัดนี้เลย ขณะที่ตัว hedge ตอบได้

**หมายเหตุ:** บรรทัด `HTTP Request: POST ... 200 OK` ของ mimo ขึ้นหลังยิง ~8 วินาที**ทุกครั้ง**
(httpx log ตอน header มาถึง) ใช้ตัดสินสุขภาพไม่ได้ ตัวแปรเดียวคือเวลาของ body

**ยังไม่ได้แก้ (ตั้งใจ):** รอบ retry ได้เศษเวลาเสมอ — รอบแรกตอบช้า (343 วิ) แล้วผิดกติกา
เหลือให้รอบสอง 257 วิ ซึ่งน้อยกว่าเวลาคิดจริงที่วัดได้สูงสุด. budget แชร์เป็นการตัดสินใจ
ที่จงใจ (`script.py` comment) ทางเลือกถ้าเจอบ่อย: ส่งรอบแก้ schema ไป `mimo-v2.5` ตรงๆ
(149 วิ พอดีกับเศษเวลา) เพราะแก้ JSON ให้ถูกกติกาไม่ต้องใช้ pro

**ยืนยันของจริงหลัง deploy:** `mimo-v2.5-pro ตอบใน 59 วินาที 4135 tokens (70 tokens/วินาที)`
— `usage.completion_tokens` มีจริงในคำตอบ non-streaming ของ mimo บรรทัดนี้ใช้ได้ไม่ใช่ 0 เปล่าๆ

**ตามด้วย (วันเดียวกัน):** รอบ retry นำด้วย `mimo-v2.5` แล้ว hedge กลับไป pro
(`_say(models=(first, hedge_to))`). เหตุผล: retry ได้แค่เศษ budget ที่รอบแรกเหลือไว้
ซึ่งอาจน้อยกว่าเวลาคิดของ pro (257 วิ เทียบ worst case 347 วิ) — แก้ JSON ให้ตรง schema
ที่เพิ่งบอกไปไม่ต้องใช้ pro แต่ "ทำให้จบในเศษเวลา" ต้องใช้ตัวเล็ก. hedge ยังสลับโมเดลเสมอ
ตามเหตุผลเดิม (hedge ที่ใช้ pool เดียวกันไม่ใช่ hedge)

## 2026-08-29 — validate() วัดพิกเซลแทนนับตัวอักษร

**อาการ:** สคริปต์เรื่อง "เตรียมรับมือน้ำท่วม" ตาย 8 นาทีหลังสั่ง ด้วย `card 3: บรรทัดยาว 37 ตัว เกิน 34` — manifest `20260829-170209-185.json` outcome `generate_failed` ไม่มี scripts เก็บไว้เลย

**เหตุ:** `HARD_MAX_CHARS_PER_LINE = 34` นับ "ตัวอักษร" ซึ่งเป็น proxy ที่ผิดสำหรับไทย — สระบน/ล่าง/วรรณยุกต์ไม่กินความกว้าง วัดของจริงจาก manifest ทั้งหมด (209 บรรทัดที่เคยผ่าน) บรรทัดที่กว้างที่สุด = **719px ที่ 33 ตัวอักษร** ขณะที่พื้นที่จริงมี 864px คือทิ้งสคริปต์ทั้งอันเพราะบรรทัดที่วาดได้สบาย

**แก้:** `script._too_wide(line)` โหลด Waree ที่ `render.MIN_TEXT_SIZE` (40px) แล้ววัด `getlength()` เทียบ `render.W - MARGIN*2` = **864px** — ไม่ใช่ 994px ของการ์ด gradient เพราะ path ที่แคบกว่าคือตอนวาดทับ footage (วาดที่เฟรม 1080 ไม่ใช่การ์ด overscan 1210) โหลดฟอนต์ไม่ได้ (นอก container) ถอยไปนับตัวอักษรเหมือนเดิม

- ข้อความ error บอก "ต้องตัดออกอีกราว N ตัว" เพราะข้อความนี้ถูกป้อนกลับเข้า retry รอบ 2 โมเดลวัดพิกเซลเองไม่ได้
- prompt ยังบอก `ห้ามเกิน 34` ไว้เหมือนเดิม เป็นแค่ guidance
- comment `render.py:39` ที่ผูกกับ 994px อัปเดตตาม

**ยืนยันบนเครื่องจริง:** `pytest tests -q` = **97 passed** (รันในอิมเมจ shorts-factory) และหลัง deploy `_too_wide("น้ำท่วมปีนี้มาเร็วกว่าที่คิดไว้มากๆ")` (35 ตัว) = 0, `"ก"*60` = 30

**ข้อจำกัดที่เจอระหว่างแก้ (ยังไม่แก้):** manifest ที่ `outcome=generate_failed` เก็บแค่ `error` ส่วน `scripts` เป็น `[]` — สคริปต์ที่ถูกตีกลับหายถาวร เลยเอาบรรทัด 37 ตัวของจริงมาเทสต์ไม่ได้ ต้องใช้บรรทัดไทยที่แต่งขึ้นแทน ถ้าจะวัดเคสถัดไปได้ต้องเก็บ draft ที่ validate ไม่ผ่านลง manifest ด้วย

## 2026-08-29 (2) — footage จาก Google Flow (คนเจนเอง)

**ที่มา:** user มี Google AI Pro แล้ว ถามว่าเอา Flow มาช่วยได้ไหม — ตรวจแล้ว **Flow credits กับ Gemini API billing เป็นคนละระบบ** subscription ไม่ให้ API access เลย และ Veo API คิดต่อวินาที (~$0.40/วิ Standard = ~$18/คลิป 45 วิ, Lite ~$0.03-0.05/วิ ยังแพงกว่า Pexels ที่ฟรี) → **ไม่ยิง API** ให้คนเจนเองในแอป Flow (Android มีแล้ว เจนเสร็จเด้ง noti, 50 free credits/วัน ทับกับ 1,000/เดือนของ Pro) เขียนเป็น `docs/adr/0005`

**ข้อจำกัดที่กำหนดดีไซน์:** ทุกอย่างต้องจบบนมือถือ (S26 Ultra) — ห้ามมีขั้นตอนที่ต้องเปิด NAS/SSH/เปลี่ยนชื่อไฟล์

**วิธีแมตช์ไฟล์กับ card (คำถามหลักของ user):** บอทส่ง prompt เป็นข้อความของตัวเอง (HTML `<pre>` = Telegram มีปุ่มก๊อป) แล้วคนตอบกลับ (reply) ข้อความนั้นพร้อมแนบ mp4 → `reply_to_message.message_id` คือตัวชี้ card แบบไม่ต้องเดา **ไม่ใช้ชื่อไฟล์/ลำดับ/คีย์เวิร์ด** เพราะไฟล์ที่ไปโผล่ผิด card = คลิปที่ render ผ่านสวยงามแต่เนื้อหาผิดเรื่อง

**สิ่งที่ทำ:**
- `script.flow_prompt(topic, card)` — ยิง mimo แยกตอนกดปุ่มเท่านั้น (budget 180 วิ) ไม่พ่วงในสคริปต์ เพราะสคริปต์คิด 90-350 วิอยู่แล้วและคลิปส่วนใหญ่ไม่ใช้ Flow. prompt บังคับ 9:16 / ห้ามมีตัวหนังสือในภาพ (การ์ดไทยวาดทับ) / ห้ามมีหน้าคนที่ระบุตัวตนได้ / กลางจอโล่ง
- `main`: ปุ่ม 🎨 บน review keyboard, `on_flow()` พักคลิปใน `state["parked"]` (รอด restart เพราะอยู่ใน state.json) แล้วบอทกลับเป็น idle จริงๆ — รับหัวข้อใหม่ได้ระหว่างรอ
- `on_footage()` รับ `video`/`document` ที่ `handle()` เมื่อก่อนทิ้งทั้งหมด → `getFile` + stream ลง `/output/footage/<clip_id>/c00.mp4` (บน `/volume1/shorts` รอด workdir ที่ถูกลบ)
- `render.build(..., supplied={0: path})` — card ที่มีไฟล์แล้วข้าม Pexels, `details.cards[].footage_source` = `flow`/`pexels`/`null` เก็บลง manifest ไว้ตอบทีหลังว่า hook จาก Flow ดันตัวเลขจริงไหม
- `auto_pick_due()` คืน False ตอนมีคลิปพัก (คนกำลังทำงานอยู่ = ข้ามรอบ ตามกฎเดิมของ deadline)
- หมดอายุ 24 ชม. (`FLOW_PARK_HOURS`) → `outcome=abandoned` ตรวจในลูป poll (ไม่มี scheduler ตาม ADR 0002) และ **pop ออกจาก state ในลูปก่อน spawn** ไม่งั้น sendMessage ช้าๆ ทำให้ tick ถัดไปยิงซ้ำ

**กันพลาดที่ใส่ไว้:** ไฟล์ >20MB ตอบว่าให้ส่งแบบวิดีโอธรรมดา (`getFile` ของ cloud API เพดาน 20MB) · reply ผิดข้อความ/ไม่ reply = ปฏิเสธ ไม่เดา · กด 🎨 ซ้ำตอนมีคลิปพัก = ปฏิเสธ (พักได้ทีละ 1) · กด render คลิปที่พักตอนมีสคริปต์ค้างรีวิว = ปฏิเสธ ไม่งั้นทับ `state["script"]` แล้ว manifest ของอันเก่าค้างไม่มี outcome · เขียน prompt ไม่สำเร็จ = สคริปต์เดิมกลับมาพร้อมปุ่ม

**ยืนยัน:** `pytest tests -q` ในอิมเมจจริง = **106 passed** (เพิ่ม 9 เทสต์: พัก/ปฏิเสธ reply ผิด/ไฟล์ใหญ่/ไฟล์เข้าที่/ส่งต่อ supplied ให้ renderer/กันทับสคริปต์รีวิว/หมดอายุ/auto-pick ยืนรอ)

**แก้ระหว่างรีวิว:** ตอนแรกย้ายเงื่อนไข "มีคลิปพัก = ไม่สุ่มทำเอง" เข้าไปใน `auto_pick_due()` ซึ่งอยู่**ก่อน**บรรทัดที่ pop `state["auto_pick"]` ทิ้ง ผลคือรอบที่ควร "ข้าม" กลายเป็น "ค้างไว้" แล้วไปยิงตอนคลิปที่พักจบ — ด้วยลิสต์ /trends จากเมื่อหลายชั่วโมงก่อน แก้เป็น `take_auto_pick(state)` ที่ pop ทิ้งเสมอแล้วค่อยตอบว่าให้ทำหรือไม่ (เทสต์เดิมผ่านเพราะบั๊กพอดี จึงเขียนใหม่ให้เช็คว่า `auto_pick` หายไปจริง)

**เจอระหว่างทาง (ไม่ได้แก้):** log ของ httpx พิมพ์ URL เต็มรวม bot token ลง `docker logs` — ใครอ่าน log ได้ = ได้ token ไปเลย ควรตั้ง `logging.getLogger("httpx").setLevel(WARNING)` หรือกรอง แต่คนละงานกับรอบนี้

## 2026-08-30 — storyboard prompt (Shorts 9:16 + คลิปยาว 16:9)

**โจทย์:** user อยากได้ prompt ไปวางใน ChatGPT ให้มันสร้างภาพ storyboard แล้วเอาภาพไปทำวิดีโอต่อเองใน Google Flow — เอาทั้งแบบ Shorts และคลิปยาว

**เส้นแบ่งที่ตัดสิน (`docs/adr/0006`):** บอทออกแค่ **ข้อความ prompt** ไม่ประกอบคลิปยาว — pipeline ทั้งเส้นคิดบนสมมติฐาน Shorts (validate 5-7 card / 40-50 วิ, การ์ด 1080x1920, TTS ก้อนเดียวตัดตาม SentenceBoundary, retention เทียบเส้น Shorts) ประกอบคลิปยาว = สแตกใหม่ทั้งอัน. ผ่อนกฎ ADR 0005 ข้อเดียว: storyboard มีตัวละครสมมติที่มีหน้าได้ (คนกำกับทุกขั้น ไม่มีอะไรอัปเอง) แต่ยังห้ามบุคคลจริง/คนดัง/แบรนด์จริง

**`app/storyboard.py` ใหม่ 2 เส้น:**
- `for_script(script)` — **ไม่ยิง LLM เลย** 1 card = 1 ฉาก, `SOUND` = `narration` คำต่อคำ (ให้ LLM เขียนใหม่ = ภาพหลุดจากคำพูดที่จะอัดทับ), เติมกฎ 9:16 + กลางจอโล่ง + ห้ามตัวหนังสือในภาพ
- `for_brief(brief)` — mimo คืน **JSON** (`overview`/`character`/`scenes[camera,scene,detail,sound,note]`) → `validate()` → เราประกอบข้อความเอง มี retry 1 ครั้งบอกว่าผิดอะไร (ให้โมเดลเขียนข้อความยาวเองแล้ววันหนึ่งมันลืมหัวข้อ SOUND โดยไม่มีใครรู้). ดีฟอลต์ 4 ฉาก 16:9 จำนวนฉาก/อัตราส่วนอ่านจากบรีฟภาษาคน ไม่มีแฟลกให้จำ

**`main`:** ปุ่ม 📋 บน review keyboard (**ไม่แตะ state เลย** สคริปต์ยังรีวิวอยู่ ปุ่มครบ กดซ้ำได้) · `/storyboard <บรีฟ>` รันด้วย `spawn()` · `send_prompt()` ส่งเป็น HTML `<pre>` (ปุ่มก๊อปบนมือถือ) เกิน 4096 ตัว **หล่นเป็นไฟล์ .txt ไม่ตัดเป็นหลายข้อความ** (ก๊อปทีละใบเรียงลำดับบนมือถือ = พลาดง่าย) · Shorts เก็บ prompt ลง manifest (`storyboard_prompt`) คลิปยาวไม่เก็บ (ไม่มี Clip ก็ไม่มี Manifest)

**ยืนยัน:** `pytest tests -q` ในอิมเมจจริง = **111 passed** (เพิ่ม 5: narration คำต่อคำ / ปุ่มไม่กินสคริปต์ / validate ครบทุกช่อง / prompt คลิปยาวมีตัวละคร+ฉากครบ / ยาวเกินไปเป็นไฟล์)

**ยืนยันเส้นคลิปยาวบนเครื่องจริง (30/08):** `/storyboard "โฆษณาบ้านเดี่ยว 10 ล้าน ... ตัวละครหญิงไทย 25 ปี 6 ฉาก"` → mimo ตอบใน **103 วินาที** ได้ **6 ฉากจริง** (บรีฟ override ดีฟอลต์ 4 ฉากได้) ทุกฉากมี SOUND ครบ prompt ยาว **6,323 ตัวอักษร** = เกิน 4096 → **เส้นส่งเป็นไฟล์คือเส้นปกติของคลิปยาว ไม่ใช่เคสหายาก** (ยิง sendDocument จริงแล้วผ่าน) จึงต้องมี `parse_mode=HTML` ใน caption ด้วย ไม่งั้นคนเห็น `<b>` ดิบๆ — แก้แล้วพร้อมเทสต์

## 2026-08-31 — storyboard เขียนใหม่: เลิกผ่าน ChatGPT ยิงเข้า Google Flow ตรงๆ

**ทำไมเปลี่ยน:** ของเมื่อวานออก prompt ไทยก้อนเดียวไปวางใน ChatGPT ให้มันวาดภาพ — user บอกผลไม่โอเค และไม่อยากผ่าน ChatGPT แล้ว. ตรวจเอกสาร Flow แล้ว: **Flow ไม่มีเมนูชื่อ storyboard** ของจริงคือ **Ingredients to Video** (แนบภาพอ้างอิงได้ 3 ภาพต่อ prompt) + **Frames to Video** + ต่อคลิปใน **Scenebuilder** (คลิปละ ~8-10 วิ) และ Flow มี image model ในตัว (Nano Banana Pro) → ไม่ต้องพึ่ง ChatGPT เลย

**สคีมาใหม่ (ตาม pattern ที่ user ให้):** `overview{title, mood_tone_progression, target_audience, master_character{...locked_prompt_tag}}` + `scenes[{camera, scene_description, visual_details, sound_verbatim, on_screen_text, scene_mood_note, image_gen_prompt, motion}]` — ฟิลด์ที่คนอ่านเป็นไทย ฟิลด์ที่เอาไปป้อนโมเดลภาพเป็นอังกฤษล้วน

**สิ่งที่บังคับด้วยโค้ดไม่ใช่ขอความร่วมมือ (`validate()`):**
- ทุกฉากต้องมี `locked_prompt_tag` **คำต่อคำ** ใน `image_gen_prompt` — ถอดความในฉากเดียว = หน้าคนละคนในฉากนั้น ซึ่งรู้ตัวตอนจ่าย credits ไปแล้ว
- `image_gen_prompt`/`motion` ห้ามมีอักษรไทย, ต้องระบุอัตราส่วน, ต้องมี `no text, no watermark, no UI, no split-screen`
- เส้น Shorts: จำนวนฉากต้องเท่าจำนวน card เป๊ะ
- `sound_verbatim`/`on_screen_text` **ไม่ตรวจแต่เขียนทับ** ด้วยของจริงจาก Script (`lock_to_script()`) — ตรวจแล้ว retry เสียเวลาเปล่า ทั้งที่เรารู้คำตอบอยู่แล้ว

**ที่เปลี่ยนไปจากดีไซน์เดิม:** เส้น Shorts **ยิง LLM แล้ว** (เดิมเป็นเทมเพลตล้วน 0 วินาที) เพราะ Script ไม่มี prompt อังกฤษให้เอามาเรียง · ส่งเป็น **N+2 ข้อความ** (overview + ตัวละคร + ฉากละใบ) ฉากละ 2 กล่องก๊อป (ภาพ / วิดีโอ = image prompt + motion) แทนก้อนเดียวยาวๆ · `manifest.storyboard` เก็บ JSON ทั้งก้อนแทน `storyboard_prompt` ที่เป็นข้อความ · เส้นส่งไฟล์ .txt ไม่ต้องใช้แล้ว (ข้อความยาวสุดที่วัดได้ 1,616 ตัว)

**ยืนยันบนเครื่องจริง:**
- `pytest tests -q` = **114 passed**
- `/storyboard` คลิปยาว: **56 วินาที 4 ฉาก** tag ตัวละครครบ prompt อังกฤษผ่าน validate ทุกข้อ
- เส้น Shorts (สคริปต์น้ำท่วม 5 card): **63 วินาที 5 ฉาก** `sound_verbatim` ตรงกับ narration ทุกฉาก ส่งเข้า Telegram จริง **7 ข้อความ** ข้อความยาวสุด 1,616 ตัว (ลิมิต 4096)
- ระวังตอนแก้ต่อ: `on_storyboard()` อ่าน `clip_id` **ก่อน** เรียกโมเดล เพราะคนเริ่มหัวข้อใหม่ระหว่างรอได้ (มีเทสต์คุม)

**สเตรสเทสต์ locked_prompt_tag (บนเครื่องจริง):** เช็คว่ากติกาที่เข้มสุดในโค้ด (บังคับให้ประโยคยาว ~200 ตัวอักษรโผล่คำต่อคำในทุกฉาก) ทำให้ต้อง retry บ่อยไหม — ยิงบรีฟ "สอนทำกาแฟดริป ... 6 ฉาก" 3 รอบ: **ผ่านหมด 3/3** (6 ฉากทุกรอบ, tag ยาว 184-207 ตัว, ครบทุกฉาก) log ไม่มีบรรทัด `ผิดกติกา` เลย = retry ไม่เคยถูกใช้จริง ถ้าวันหลังเจอ retry บ่อย ให้แก้ที่ validator (แมตช์ substring เด่นๆ ของ tag แทนทั้งประโยค) ไม่ใช่ไปแก้ prompt

## 2026-08-31 — `/say`: แก้เสียงอ่านคำที่โมเดลกับ edge-tts อ่านผิด

**อาการ:** คลิป `20260831-110635-875` (หัวข้อ "TH-AI Passport") เสียงอ่านออกมาเป็น
"ที เอ ไอ พาด" ดูใน manifest แล้ว `spoken` ที่โมเดลเขียนคือ `ทีเอไอพาสปอร์ต` —
มันสะกดทีละตัวอักษรตาม pattern ที่ prompt สอนไว้ (`AI → เอไอ`, `CPU → ซีพียู`)
โดยไม่รู้ว่า TH-AI เป็นการเล่นคำที่ต้องอ่านว่า "ไทย"

**ทำไมแก้ที่ prompt อย่างเดียวไม่พอ:** ปัญหามีสองชั้น ชั้นแรกเป็นของโมเดล
(แก้ด้วย prompt ได้บ้าง แต่บังคับไม่ได้ `validate()` ตรวจไม่ได้ว่าคำไหนเป็นการเล่นคำ)
ชั้นที่สองเป็นของ edge-tts เอง — คำไทยที่สะกดถูกอยู่แล้วบางคำมันก็ออกเสียงเพี้ยน
กดปุ่มเขียนใหม่ก็ได้ข้อความเดิมและเสียงเดิม ต้องมีการแทนที่แบบตายตัวตรงปาก TTS

**ที่ทำ:**
- `render.say_as()` / `render.say_set()` เก็บคำแทนที่ไว้ที่ `/data/say.json`
  คีย์เป็นภาษาไทย เพราะ `spoken` ไม่มีอักษรละตินอยู่แล้ว (validator กันไว้)
- แทนที่ใน `_tts_text()` เท่านั้น **ห้ามย้ายไปทำตอน join ใน `narrate()`** —
  `narrate()` เทียบ `heard.startswith(spoken[:10])` ถ้าสองฝั่งเป็นคนละเวอร์ชัน
  จะ misalign ทุก card แล้วเงียบๆ ถอยไปพูดทีละ card (prosody ขาด) โดยไม่มี error
  แทนที่คีย์ยาวก่อนสั้น กันคีย์สั้นกินคีย์ยาว
- คำสั่ง `/say <ผิด> = <ถูก>` (ไม่ใส่อะไร = ดูรายการ, ปล่อยฝั่งขวาว่าง = ลบ)
- `format_script()` โชว์บรรทัด 🗣 `spoken` เฉพาะ card ที่ต่างจาก `narration`
  เพื่อให้ก๊อปคำที่อ่านผิดจากมือถือได้ ไม่ต้อง SSH เข้าไปดู manifest
- prompt เพิ่ม 2 บรรทัด: คำเล่นคำให้ใช้คำอ่าน ไม่ใช่สะกดทีละตัว (TH-AI → ไทย)
- เทสต์: override เปิดอยู่แล้ว `narrate()` ต้องยัง align ได้ (mock boundary
  ให้สะท้อนข้อความหลังแทนที่) — เทสต์ที่เช็กแค่ `_tts_text()` จับ regression
  ตัวนี้ไม่ได้

**ยังไม่ได้ทำ:** ไม่ได้ย้อนไปแก้คลิปเก่า คลิป TH-AI ต้องสั่งทำใหม่ถึงจะได้เสียงถูก

## 2026-08-31 (2) — หัวข้อทรงผลแข่ง บอทไม่รับแล้ว

**อาการที่ user เจอ:** ส่งหัวข้อวอลเลย์บอล 3 แบบ (ไทยชนะจีนได้ไปโอลิมปิก /
U19 จีนแพ้ไทยเพราะอะไร / วิเคราะห์ผลไทย vs จีน) ได้สคริปต์ "แบบเดิมเป๊ะ" ทุกครั้ง

**หลักฐาน:** ดึงทุก manifest ที่หัวข้อมี "วอลเล" ได้ 6 คลิป 3 วัน โครงเดียวกันหมด —
ตัวเล็ก/เตี้ยกว่า → บอลเร็วบอลสั้น → เกมรับเหนียว → เสิร์ฟกดดัน → "ความสูงไม่ใช่ทุกอย่าง"
คลิป 08-30 20:24 กับ 08-31 08:16 ได้ **title ตรงกันทุกตัวอักษร** ทั้งที่หัวข้อคนละอัน

**สาเหตุ:** mimo ไม่ต่อเน็ต ไม่รู้ผลแข่ง + SYSTEM_PROMPT สั่งให้ถอยไปเล่า
"ความรู้ทั่วไปที่ตรวจสอบได้" เมื่อหัวข้อพาไปทางข่าว/ผลการแข่งขัน → เหลือบ่อความรู้บ่อเดียว
ตักกี่ครั้งก็ได้น้ำเดิม. `avoid` (title 30 อันล่าสุดที่อัป) **ไม่ได้พัง** — ตอน 08-31 08:16
title ของ 08-30 อยู่ในลิสต์ครบแล้วโมเดลก็ยังคืนอันเดิม เพราะสั่ง "หามุมใหม่" กับโมเดล
ที่มีมุมเดียวเป็นคำสั่งที่ทำตามไม่ได้. คลิปที่ variant=None 3 อันคือคลิป explore
(`เขียนออกนอกแพตเทิร์น`) — สั่งให้แหกแพตเทิร์นแล้วยังออกมาโครงเดิม ยืนยันว่าไม่ใช่เรื่อง
prompt variation

**ผลข้างเคียงที่เจอระหว่างตรวจ:** 2 คลิปที่อัปขึ้น YouTube ไปแล้วมีตัวเลขที่โมเดลแต่งเอง
("ลูกเซ็ตไทยเร็วกว่าญี่ปุ่น 0.3 วินาที", "จีนสูงเฉลี่ย 186 ไทย 175") ไม่มีแหล่งอ้างอิงสักตัว

**ที่ทำ (user เลือกข้อ 1 จาก 3 ทางเลือก):** `main.RESULT_TOPIC` regex จับหัวข้อทรงผลแข่ง
แล้วปฏิเสธพร้อมบอกเหตุผล + เสนอมุมที่เล่าได้ ไม่เขียนของกลวงให้
- ด่านอยู่ใน `make_script()` **ไม่ใช่ `on_text`** — ทางเข้าหัวข้อมี 3 ทาง (พิมพ์เอง /
  ปุ่มเลขจาก /trends / auto-pick) ทุกทางลงมาที่นี่หมด ด่านเดียวคุมครบ
- เช็ค **ก่อน** `manifest.start()` และก่อน `state.pop("auto_pick")` — หัวข้อที่ถูกปฏิเสธ
  ต้องไม่ทิ้ง manifest เปล่า ไม่กิน variant และไม่ล้ม auto-pick ที่ค้างอยู่ (มีเทสต์คุม)
- ไม่เช็คตอนแก้สคริปต์ (`previous is not None`) — นั่นคือคลิปที่เปิดไปแล้ว
- ทางออกฉุกเฉินกัน false positive: ใส่ `!` นำหน้าหัวข้อ = ข้ามด่าน (prefix ถูกตัดทิ้ง
  ก่อนส่งเข้าโมเดล มีเทสต์คุม)

**ไม่ได้ทำ:** ทางเลือกข้อ 2 (รับข้อมูลที่คนแปะมาแล้วห้ามโมเดลเติมตัวเลขเอง) — user
ตัดสินใจว่าช่องนี้ไม่ทำหัวข้อผลแข่ง. ไม่ได้ต่อ search API (คีย์ใหม่ + ค่าใช้จ่าย และยังต้อง
เชื่อว่าโมเดลไม่แต่งตัวเลขอยู่ดี). ไม่ได้แตะ `avoid` เพราะไม่ใช่ต้นตอ

## 2026-08-31 (3) — `/redo` + ด่านคำสั่งพิมพ์ผิด, และเรื่องเสียง /s/ ท้ายคำ

**คำถามจาก user:** "AI Pass" อ่านออกมาเป็น "เอ ไอ พาด" ทำไมไม่เป็น "พาส"

**คำตอบ (ไม่ใช่บั๊ก):** ภาษาไทยไม่มีเสียง /s/ ท้ายพยางค์ ตัวสะกดแม่กด (ส ษ ศ ซ ทร ช)
ออกเสียง /t/ ทั้งหมด — "พาส" อ่าน "พาด" ตามกฎ. manifest `20260831-133703-103` เขียน
`spoken` ว่า `เอไอพาสทีเอช` ซึ่งสะกดถูกตามหลักทับศัพท์แล้ว สะกดยังไงก็แก้ไม่ได้
**แก้ที่บันทึกไว้เมื่อเช้าด้วย:** เคสน `พาสปอร์ต → พาด` ไม่ใช่ edge-tts G2P เพี้ยน
เป็นกฎเดียวกันนี้ ("พาด-สะ-ปอด" คือคำอ่านไทยมาตรฐาน)

ทางออกมีแค่ 2 ทาง ทั้งคู่ผ่าน `/say` ไม่ต้อง deploy — ค่าฝั่งขวาของ `/say` ไม่ผ่าน
`validate()` จึงใส่ละตินได้ (`= เอไอ Pass ทีเอช`) หรือเติมสระให้ ส ไปเป็นพยัญชนะต้น
ของพยางค์ถัดไป (`= เอไอพาสะทีเอช`) ครอบคลุมทุกคำที่ลงท้ายเสียง s: Pass, Plus, News, Class

**`/redo`:** เดิมแก้ `/say` แล้วต้องส่งหัวข้อใหม่ = รอ mimo 2-5 นาที และได้สคริปต์คนละตัว
ทั้งที่คำไม่ได้ผิด เสียงต่างหากที่ผิด. `/redo` เอา `state["last_script"]` (ที่ `deliver()`
snapshot ไว้อยู่แล้วสำหรับปุ่มอัป YouTube) ใส่กลับเข้า state แล้วเรียก `do_render()` —
ไม่ยิง LLM เลย, `clip_id`/`topic` ใส่กลับจาก `last_*` ให้ลงใน manifest เดิม ไม่เปิดคลิปใหม่,
เพลงประกอบสุ่มใหม่ตามปกติ, คลิปที่อัป YouTube ไปแล้วไม่ถูกแทนที่

**ด่านคำสั่งพิมพ์ผิด:** user พิมพ์ `/redo` ตอนที่ยังไม่มีคำสั่งนี้ → ตกไปสาขา else ของ
`on_text()` กลายเป็น "หัวข้อ" ยิงเข้า mimo แล้วพังที่ `โมเดลไม่ได้ตอบเป็น JSON`
เคยเกิดมาแล้วกับ `/stat` (30/08 มี manifest ชื่อ `/stat` outcome rendered จริงๆ)
→ ข้อความที่ขึ้นต้นด้วย `/` และไม่ตรงคำสั่งไหน ตอบ "ไม่รู้จักคำสั่ง" ไม่ส่งเข้าโมเดล

เทสต์: `/redo` ต้องไม่แตะ `generate()` + ใส่ `clip_id` เดิมกลับ, `/redo` ตอนไม่มีคลิปล่าสุด,
`/stat` ต้องไม่ถึงโมเดล

## 2026-09-01 — URL ล้วนทำให้ mimo ตอบ prose แทน Script JSON

**อาการ:** ส่ง `https://marketeeronline.co/archives/484466` ใน Telegram แล้วรอเขียน
สคริปต์ ก่อนจบด้วย `เขียนสคริปต์ไม่สำเร็จ: โมเดลไม่ได้ตอบเป็น JSON`

**ต้นเหตุ:** Telegram แสดง title/description/image เป็น link preview ให้คนเห็น แต่ข้อมูล
preview นั้นไม่ได้อยู่ใน `message.text` ที่บอทอ่าน — `on_text()` ได้ URL ล้วนและส่งต่อเป็น
Topic. `RESULT_TOPIC` จับไม่ได้เพราะ URL ไม่มีคำว่า ชนะ/แพ้/ผลแข่ง และ mimo ไม่มี browser
ให้อ่านหน้าเว็บ จึงตอบคำอธิบาย/ขอข้อมูลเพิ่มเป็น prose; `_parse()` หา `{...}` ไม่เจอ
แล้ว retry ก็ได้รับ input เดิมจึงพังแบบเดิม

**แก้:** เพิ่ม `main.BARE_URL` และตรวจในประตูรวม `make_script()` ก่อน `manifest.start()`,
ก่อนสุ่ม Variant และก่อนลบ auto-pick. URL ล้วนถูกปฏิเสธทันทีพร้อมบอกว่าให้พิมพ์หัวข้อหรือ
พาดหัวมาด้วย; ข้อความที่มีหัวข้อและแปะ URL อ้างอิงต่อท้ายยังใช้ได้ตามปกติ

**Regression:** เพิ่มเทสต์ที่ยืนยันว่า URL ล้วนไม่ถึง `script_gen.generate()` และไม่เปลี่ยน
state/สร้างงานค้าง

## 2026-09-01 (2) — Dashboard read-only ที่ port 5069

**ที่ทำ:** เพิ่ม `app/dashboard.py` (FastAPI) + `docker-compose.yml` service
ใหม่ `shorts-factory-dashboard` (image เดียวกับบอท `command` ต่างกัน,
mount `/data:ro`, ไม่มี `env_file`) + `nginx` sidecar publish `5069:80` basic
auth จาก `.htpasswd`. สี่หน้า: `/` (ลิสต์คลิปทุกอันพร้อมตัวเลข day-7),
`/clip/{id}` (manifest เต็มรวม draft ที่กดทิ้ง), `/experiment` (สอง arm +
verdict), `/now` (state.json + say.json + upload ล่าสุด)

**ทำไม:** อยากดูสถานะคลิป/experiment/state จากเบราว์เซอร์แทนไล่ manifest JSON
เอง แต่ไม่อยากเพิ่ม credential ให้ process ที่ LAN เข้าถึงได้ — เลยแยกเป็นคอนเทนเนอร์
คนละตัวจากบอท (import `app.manifest`/`app.experiment`/`app.analytics` ตัวเดียวกับ
บอทใช้ กันเลขไม่ตรงกัน), การันตี read-only สองชั้น: mount `:ro` + เทสต์
`test_no_route_can_write` เช็คว่าทุก route เป็น GET/HEAD เท่านั้น. อ่าน `say.json`
ผ่าน `_say()` เองในไฟล์ ไม่ import `app.render` เพื่อไม่ให้ Pillow/edge-tts ติดเข้ามา
ใน process ที่เปิดพอร์ต. เอกสารเหตุผลเต็มอยู่ `docs/adr/0007` (แก้ ADR 0002 ที่เคย
บอกว่า stack ไม่มี HTTP surface — ยังจริงสำหรับตัวบอท แต่ไม่จริงสำหรับทั้ง stack แล้ว)

### 2026-09-01 — dashboard deploy verified on the NAS

`./scripts/deploy.sh -s shorts-factory -y`, then, checked from the NAS itself
(port 5069 is LAN-only — it is not forwarded, so a workstation on another
network gets no connection at all, which is the intended shape):

- `shorts-factory` Up with no published ports, `shorts-factory-dashboard` Up on
  `8000/tcp` unpublished, `shorts-factory-nginx` Up on `0.0.0.0:5069->80/tcp`
- `docker exec shorts-factory-dashboard touch /data/nope` →
  `touch: cannot touch '/data/nope': Read-only file system`
- `printenv | grep -c 'TELEGRAM\|YOUTUBE\|MIMO\|PEXELS'` inside the dashboard → `0`
- `curl http://localhost:5069/` without credentials → `401`; with them, `/`,
  `/experiment`, `/now` and `/healthz` all `200`, and `/` listed the real clips
- `/clip/<real id>` → 200, `/clip/nope` → 404 (logged `อ่าน manifest nope ไม่ได้`,
  no traceback), `/clip/..%2f..%2fetc%2fpasswd` → 400
- the bot kept polling `getUpdates` through the restart; host free memory
  unchanged within noise (4.0 GB available)

Observation, pre-existing and unrelated to this change: httpx logs the full
Telegram API URL, so the bot token appears in `docker logs shorts-factory`.

## 2026-09-01 (3) — Dashboard port moved 5069 → 5071

Port 5069 collided with `dupe-sweeper` (which already owns it). Moved the
`shorts-factory-nginx` sidecar publish to `5071:80` in `docker-compose.yml`
and updated every "current state" reference (`README.md`, `.notes/00_INDEX.md`,
root `CLAUDE.md`, root `README.md`). Historical docs (ADR 0002/0007, the
2026-09-01 dashboard spec/plan, earlier daily-log entries) were left as-is —
they record the decision as made at the time, not the live port number.
Next deploy: re-run `make secrets` if the vault manifest references the port,
then `docker compose up -d` to recreate `shorts-factory-nginx` on the new port.

## 2026-09-02 — Dashboard redesign (visual, still read-only)

Restyled all four dashboard pages and reorganised what they lead with. No new
route, no new dependency, no change to what the bot does.

- `app/static/style.css` rewritten around the token set `dupe-sweeper` already
  uses (surface / surface-2 / border / muted / shadow / radius) so the two
  dashboards look like one system; the accent stays amber, which is what tells
  the browser tabs apart. Light/dark via `data-theme` following the system,
  toggle remembered in `localStorage`, with a three-line inline script in
  `<head>` — later than that and every navigation flashes the wrong theme.
- Tables become one card per row below 600px (`data-label` on each cell).
- `app/static/app.js` (new, ~20 lines, no dependency): theme toggle and the
  outcome filter. Filtering is client-side on purpose — a server-side filter
  would mean a query parameter on a route that ADR 0007 keeps to plain GET.
- `/` leads with four KPIs: published against the Gate of 30 (with a meter),
  median day-7 retention, total day-7 views, clips on record and how many were
  discarded. `state.mode` is deliberately *not* a KPI: the dashboard only reads
  what the bot wrote, and a stale mode would read as a live one.
- `/clip` draws views over age as inline SVG (`_chart()` returns polyline
  coordinates; needs two snapshots or it returns None). It is **not** drawn
  with `app.retention`'s Pillow code — see the test below.
- `/now` lifts the interesting keys into cards but still prints the whole
  `state.json` underneath, so keys the bot invents later cannot go unseen.
- Tests: `test_no_drawing_library_in_this_process` asserts `PIL` and `edge_tts`
  are absent from `sys.modules` after importing the dashboard. This is the
  property ADR 0007 actually claims; `test_no_route_can_write` only checks HTTP
  methods and would not have caught a Pillow-drawn chart. ADR 0007 amended with
  a paragraph saying so.

Verified locally: `pytest tests/test_dashboard.py` → 16 passed; the app served
against a sample `/data` returned 200 on `/`, `/clip/{id}`, `/experiment`,
`/now`. `tests/test_shorts_factory.py` cannot be collected on the workstation
(`edge_tts` is not installed there) — pre-existing, unrelated.

## 2026-09-04 — generate() gave up at a hard-coded 2 attempts, wasting ~420s of budget

Incident: an auto round's `generate()` attempt 0 (pro model) returned 60 chars
of non-JSON after 167s. The old code appended that garbage to `messages` as
an assistant turn plus a "ส่ง JSON ใหม่" correction and sent it to the weaker
fallback model, which replied with a 496-char fragment missing `title`.
`for attempt in range(2):` then ran out and raised `ScriptError` after only
~172s of the 600s `BUDGET_SECONDS` — a plain retry of the same prompt
succeeds, so the budget was there, the attempt count was not.

Fixed in `app/script.py`:
- `generate()`'s loop is deadline-driven (`while` on `deadline - now >=
  MIN_ATTEMPT`, hard cap 4 attempts) instead of `for attempt in range(2)`.
  Model selection unchanged: attempt 0 leads with the pro model, every later
  attempt leads with the fallback.
- A reply is now parsed *before* deciding whether to feed it back. Only a
  reply that parsed as JSON but failed `validate()` gets appended to
  `messages` with a correction (unchanged from before). A reply that did not
  parse at all is retried with `messages` untouched — feeding garbage back
  as an "assistant" turn only poisoned the next model's context.
- `_say`'s `once()` now reads `finish_reason` (via `getattr`, defensive
  against the test doubles that omit it) and raises `ScriptError` if it is
  `"length"` or the content is blank, instead of quietly returning junk.
  `generate()`'s `_say` call now also catches `ScriptError` (not just
  `asyncio.TimeoutError`) and retries with `messages` unchanged.
- The "model didn't return JSON" warning and the `ScriptError` message now
  carry the first ~300 chars of the raw reply alongside the length, so the
  next occurrence is diagnosable from the manifest's `error` field alone.
- The final `ScriptError` now chains every attempt's failure, most-recent-
  first (same idiom the timeout branch already used), so a fallback model's
  schema slip no longer hides the pro model's earlier failure behind it —
  main.py still truncates at 500 chars, so the newest failure is what survives.

Added two tests to `tests/test_shorts_factory.py`: a garbage-then-good reply
proves `messages` isn't polluted before the retry, and a deadline test proves
more than 2 attempts happen when the budget allows and that the loop stops on
the deadline rather than a fixed count.

Verified in the running container (`/tmp/verify` scratch copy, container
untouched): `pytest tests/test_shorts_factory.py -q` → **129 passed**.

## 2026-09-07 — English Locale, batch 1: a second audience, not a second bot

New `app/locales.py` bundles everything that changes for a different
audience — prompt language, TTS voice, on-screen line width, captions
language, output subfolder, trends country, and (once batch 2 exists) the
YouTube channel — behind `locales.get()`, which degrades an unknown or
missing locale code to `th` so a Manifest written before Locales existed
still loads. Environment is read at call time, not import, so tests can
swap a voice without reloading the module.

- English voice is `en-US-AndrewNeural` via `TTS_VOICE_EN`, added to
  `secrets.manifest.yaml` as a literal; Thai keeps `th-TH-NiwatNeural`.
  Measured on the container today: `en-US-AndrewNeural` emits
  `SentenceBoundary` the same as the Thai voice does (3 cards in, 3
  boundaries out), so English rides the same one-take narration, tighten,
  and join pipeline as Thai — no separate code path was needed.
- `/en <topic>` writes and renders an English clip; `/trends en` pulls
  Google Trends US plus the YouTube US `mostPopular` chart and its number
  buttons produce English clips the same way the Thai ones do. Every bot
  message, button and error stays Thai regardless of the clip's language.
- The `spoken`/`narration` split is mirrored rather than reused as-is: Thai
  still forbids Latin in `spoken`, English now forbids Thai in it, and
  `render._speakable()` joins words with a space for English (Thai has no
  word boundaries to join on).
- English line width is capped at 24 characters (target 18) and that cap is
  **enforced**, not just suggested to the model — `script._too_wide()` reads
  the Locale's `enforce_char_count` flag and rejects an oversized English
  line outright, because at Waree-Bold size 92 a Latin character measures
  about 50px against Thai's 21px, and the pixel floor alone would pass a
  38-character Latin line and then draw it at the 40px minimum. Thai keeps
  measuring pixels only; its 34-character figure stays prompt guidance, not
  a check.
- `experiment.py` keeps the same `hook` factor and the same two variant
  names, but the clause text is written in the clip's own language
  (`experiment.VARIANTS_EN` plus an English explore clause).
- `main.RESULT_TOPIC` now also matches English result phrasings (beat,
  defeated, final score, standings, who won, last night, …) in the same
  regex Thai uses; a leading `!` still bypasses the check either way.
- English files land in `/volume1/shorts/en` (`/output/en` inside the
  container); Thai output is unchanged at `/output`. `app/backfill.py` now
  walks with `rglob` instead of `glob` so it finds both folders, and reads
  the locale back from the folder name.

Deferred to batch 2, all still open: the second YouTube channel itself
(`YOUTUBE_EN_*` env, vault `stacks.shorts_factory.youtube_en.*`, its own
`scripts/youtube_auth.py` run with the consent screen `In production`),
per-locale analytics/retention, per-locale Gate counting in
`experiment.report()`, and a locale column on the dashboard. New:
`docs/adr/0008` records why English needs its own channel rather than a
`locale` field on a shared one.

Being accurate about what was not guarded yet at the time of writing: at the
end of the batch-1 session, `deliver()` showed the upload button whenever
`youtube.configured()` was true, with no branch on the clip's locale —
pressing it on an English clip would have published through the Thai
channel's credentials. Nothing in the code stopped that. Only the bot's Thai
`/help` text told the human that English clips had to be uploaded by hand
from `/volume1/shorts/en` for now; there was no code-level guard, and this
entry did not claim one existed. (See the addendum below — that gap has
since been closed.)

Also left alone on purpose: `storyboard.SHORTS_LAYOUT` still describes the
9:16 layout's negative space as being "for Thai subtitles" — storyboards
were not touched in this batch.

No tests were run in this session; this was a documentation-only pass over
already-implemented code.

**Addendum — upload guard added:** the gap the documentation pass above
flagged has since been closed. `youtube.py` now reads every setting —
client credentials, category id, privacy, caption language — under the
Locale's own env prefix (`YOUTUBE_` for Thai, `YOUTUBE_EN_` for English),
with no shared fallback between them. `deliver()` gates the upload button
on `youtube.configured(locale)` for the clip's own locale, so the button
simply does not appear on an English clip until `YOUTUBE_EN_*` is
provisioned. `do_upload()` carries the locale snapshotted at deliver time
through upload, thumbnail and captions, and `add_captions` tags the caption
track's language from the Locale instead of always writing `th`. History
entries now carry a `locale` field per upload (older entries have none and
read as Thai). Two tests hold the line:
`test_an_english_clip_never_uploads_through_the_thai_channel` and
`test_the_upload_uses_the_locale_the_clip_was_delivered_with`. The full
suite passes 159 in the container. The second channel itself still does not
exist — until it does, English clips are still copied by hand from
`/volume1/shorts/en`.

## 2026-09-07 — English Locale, batch 2: every number is per channel

Batch 1 (commit `b745415`) made the bot able to write, render and route an
English clip. Batch 2 makes the measurements follow the same split, because a
Gate counted across two audiences is not the Gate ADR 0004 describes.

- `history.py`: each entry records its `locale`; `video_ids(locale)` and
  `recent_titles(limit, locale)` filter on it. No argument means every
  channel; an entry written before Locales existed has no field and is read as
  Thai.
- `analytics.py`: `performance`, `latest_data_date`, `gate_note`,
  `format_report` and `winning_examples` all take a Locale and use that
  channel's own OAuth token. The Gate numbers are unchanged (30 clips, 300
  views per variant) — each channel reaches them on its own count.
- `experiment.py`: `for_locale()` filters records first; `report()` labels the
  Locale in its heading and runs `by_category` on the filtered set, or the
  Thai `เทค` and the English `tech` would sit in one table as unrelated rows.
- `snapshots.py`: one Analytics pull per channel, each with its own token and
  its own `MAX_VIDEOS` cap (that cap is a URL-length limit per request, not a
  daily budget). A Locale with no credentials is skipped with a log line, and
  a channel that fails is caught so the other still gets its daily reading.
  This is the one place where mixing would have been silent: asking a channel
  about a video id it does not own is not an error, it just returns no row,
  which reads exactly like "Analytics has not processed it yet".
- `retention.py` + `main.on_retention`: the channel comes off the clip's own
  Manifest. No `/retention <lang>` argument, so no way to pair a video id with
  the wrong channel.
- `main.py`: `/stats` prints one report per channel that has credentials —
  with only Thai configured the output is byte-identical to before.
  `/experiment` prints Thai always and another Locale once it has Manifests.
  `make_script` now feeds `avoid`/`winners` from the clip's own channel
  instead of blanking them for English.
- Dashboard: a ภาษา column on the clip list, and `/experiment` renders one
  section per Locale (heading, gate note, verdict, arms, its own clauses,
  categories). Still GET-only; `test_no_route_can_write` still passes.
- `scripts/youtube_auth.py` docstring now names both vault paths and warns
  that writing the English token over `stacks.shorts_factory.youtube.*`
  silently redirects every Thai upload.

**Deliberately not done:** `YOUTUBE_EN_CLIENT_ID` / `_CLIENT_SECRET` /
`_REFRESH_TOKEN` are still absent from `secrets.manifest.yaml`.
`scripts/render_env.py` raises `manifest references missing vault path` for a
key the vault does not hold, and `make secrets` then fails for *every* stack
in the repo. The mapping is added after the vault has the values, as a step of
the human OAuth handoff — the ordering is written out in the README under
"Provisioning the second channel".

Tests: 169 passing in the container (`docker compose run --rm --entrypoint
pytest shorts-factory tests/`), including new coverage for per-channel history
filtering, per-channel Gate counting, per-channel snapshot pulls (including a
channel with no credentials and a channel that fails), and `/retention`
reading the locale off the Manifest.

## 2026-09-07 — one Topic, both channels

Asked for: pick a topic off `/trends` once, review both scripts, render both,
upload to the two channels separately.

Shape chosen after grilling: **sequential, not simultaneous.** `/both <topic>`
(or the new 🌏 row under a `/trends` list) sets `state["pair"]`, writes the
Thai half and hands it to the existing review flow untouched. `do_render()`
sets a `delivered` flag and, outside its `finally`, spawns `continue_pair()`,
which writes the English half from an idle bot exactly as a typed Topic would.
Two scripts on screen at once would have to be read side by side on a phone,
and every revision would have to name which one it meant.

- The English half is written natively with the approved Thai Script passed as
  context (`script.generate(sibling=...)` → `_sibling_note()`), explicitly
  told not to translate: 24 characters a line against Thai's 34, and hooks
  that do not carry across.
- `pair_id` on both Manifests is the Thai half's clip id.
- A render failure or a 🗑 on the first half pops `state["pair"]` and says so
  in the chat; a generate failure does the same.
- Variants stay independently drawn per half. Locking them together would make
  each channel collect its two arms half as fast.
- The numbered `/trends` buttons and both automatic rounds are unchanged and
  still Thai-only — a Thai search spike is usually not a US topic, and an
  unattended round with nobody reviewing should not double its output.

**Bug fixed on the way, older than this feature:** the upload button carried
no Clip id, and `deliver()` kept a single `last_*` slot. Render two clips back
to back — which a pair does by design — and the first clip's button uploaded
the second clip. Buttons now carry `upload:<clip_id>` and `deliver()` records
each finished Clip in `state["uploads"]` (capped at 10). Buttons sent before
this still mean "the last clip", which is what they meant when they were sent.

Tests: 179 passing in the container.
