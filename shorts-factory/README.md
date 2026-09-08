# shorts-factory

Telegram bot that turns a one-line topic into a 40-50 second vertical short.
Thai by default; `/en` or a tap on a `/trends en` suggestion writes and
renders the same clip in English instead — see [Locales](#locales).

Send it a topic. It asks mimo for a script, shows you the script, and waits.
Press **render** and it draws the cards, speaks them, assembles the clip, and
sends the mp4 back — plus a copy on the NAS at `/volume1/shorts` with the
title/description/hashtags in a `.txt` beside it. Press **เขียนใหม่** and it
asks what to change instead.

It does not upload to YouTube, and it has no web interface — no port, no
nginx, no dashboard. See `docs/adr/0001` and `docs/adr/0002` at the repo root
for why.

## Waiting for the model

Writing a script takes minutes, and how many depends on how long the model
decides to think rather than on the network. Measured on the NAS, same prompt:
93s for 3,092 completion tokens, 112s/4,016, 197s/7,010, 207s/5,415 and
347s/10,585 — roughly 30 tokens a second every time. The old 240s cap cut off
the long thinks and then retried, so a topic that needed 283s produced eight
minutes of silence and an error; the budget is now 600s and it is **shared
across every attempt**, because two full-length tries is twenty minutes of
someone staring at "กำลังเขียนสคริปต์...". A timeout is therefore never
retried — it has spent the whole budget by definition — while a script that
comes back malformed is, since that leaves time on the clock.

How many retries that buys is decided by the clock, not by a count. It used to
be exactly two tries, which confuses "the budget ran out" with "the attempts
ran out": on 2026-09-04 an unattended round got 60 characters of non-JSON from
the pro model, spent its one retry on the small model, and gave up 172s in with
420s unspent — on a topic that answered correctly on a plain retry minutes
later. The loop now keeps trying while at least `MIN_ATTEMPT` of the budget is
left, capped at four so an endpoint that fails in milliseconds cannot spin.

Only a reply that parsed as JSON is quoted back to the model. A malformed
*script* is worth answering with "this is what broke, send it again" — the
model can see its own JSON and fix the field. A reply that was not JSON at all
is not: appending it puts garbage in the context the next attempt reads, which
is how that 2026-09-04 retry produced a 496-character fragment with no `title`.
Those attempts retry with the conversation untouched. A reply the endpoint cut
off (`finish_reason == "length"`) or left blank is rejected inside `_say()`
before it can be parsed at all, and both failure branches log the first 300
characters of what came back — logging only its length is why nobody could say
what those 60 characters were.

There is a second failure shape underneath that one. A request can take the
headers and never deliver a body: observed 2026-08-27 at 19:25:45, "200 OK"
logged instantly, silence until the deadline — and the same topic answered in
137s two hours later. Waiting a hang out costs ten minutes; cutting every slow
call off costs the long thinks that do finish. So after 240s a second request
goes out alongside the first and whichever answers first wins. It goes to
`mimo-v2.5` rather than to `mimo-v2.5-pro` again: an identical twin was tried
and both requests hung together in the same episode, while the smaller model
wrote the same script in 149s.

One hedge turned out not to be enough. On 2026-09-07 an English script request
hung, its 240s hedge hung as well, and both were still silent when the 600s
budget ran out — 360 seconds spent waiting on two requests that were never
going to answer. What settles it is what happened around them: an unrelated
call sent at 17:00 answered in 43s, and the identical topic, asked again three
minutes after the failure, came back in 62s. The stall is per request, not per
endpoint, so a third request is worth more than more waiting. A second hedge
now goes out 120s after the first (`HEDGE_AGAIN`), back to the pro model,
which is the better writer and by then usually healthy. A hedge is skipped
when less than `HEDGE_MIN_ROOM` (150s) of the budget remains, since the
fastest healthy answers measured are 30-70s and one fired into the last
seconds cannot come back.

Then the hedges were counted, and none of them had ever rescued anything. Two
episodes exist in the log. On 2026-09-07 19:02 the hedge fired at 240s and then
the *primary* answered at 268s — a long think that crossed the threshold, not a
save. On 2026-09-08 08:02 the primary and both hedges were silent together at
the deadline, and an unrelated request half an hour later answered in 51s. Read
again with that in hand, the 2026-09-07 counter-example does not say what it
was taken to say: the 43s call at 17:00 had *finished* before the window
opened. Both episodes are a correlated sick window of a few minutes, so a
fourth concurrent request would have died with the other three.

What worked both times was asking again once the window had passed. `generate()`
now raises `ScriptStalled` — a `ScriptError` subclass — when the budget expires
with nobody answering, and `make_script()` catches that one shape, says so in
the chat, waits `STALL_COOLDOWN` (`MIMO_STALL_COOLDOWN_SECONDS`, default 180s)
and writes the same topic again. Once only: past that it is not a window. A
script that came back malformed gets no such retry, because it will come back
malformed again. The worst case is now about 23 minutes to give up instead of
10, and the bot answers other commands throughout. The hedges are left in
place — they cost one spare request, and the 268s case shows 240s sits close to
a genuine long think.

Streaming was tried and abandoned: reading the same answer as a stream took
400s against 137s unstreamed. It would have let silence be told from slowness,
but this endpoint does not go silent — it thinks — so the trade bought nothing
and cost three times the wall clock.

Long jobs run off the Telegram poll loop, so `/help`, `/stats` and the rest
still answer while a script is being written or a clip rendered. The bot takes
one job at a time: a second topic during either is refused rather than queued.

## Commands

`/help` prints the lot in Thai, inside the chat, which is where anyone would
look for it — in as many messages as it takes, since Telegram refuses anything
over 4096 characters outright and delivers nothing rather than truncating. That
is how the help page went silent for days once it grew to 4690 (2026-09-08), so
every message the bot sends is now chunked on paragraph breaks, with any
keyboard on the last piece. The others: `/stats` (how published clips did), `/snapshot` (pull
today's numbers now rather than waiting for the daily run), `/experiment` (the
running A/B and whether it can be called yet), `/retention` (one clip's curve,
with the drop-offs named by card), `/trends` (what Thailand is searching for
and watching, turned into topics), `/storyboard` (a long-form storyboard from a
one-line brief), `/say` (fix how a word is pronounced), `/redo` (render the
last script again, unchanged), `/en <topic>` (write and render the same clip
in English — see [Locales](#locales)). Anything that is not a command is a
Topic — except text starting with `/`, which is answered as an unknown command
rather than written about, because a typo used to cost minutes of model time
and left a manifest named after it.

A Topic that hinges on a result — who won, a score, what was announced — is
turned away with an explanation rather than written. The model has no source
for any of it: no web access, and the prompt sends it to general knowledge
instead. Six volleyball topics between 2026-08-29 and 08-31 came back with one
essay (small team, fast sets, tight defence, heart) and two reached YouTube
carrying invented figures — "Thai sets are 0.3s faster", "China average 186cm
against Thailand's 175". `/trends` has always dropped news and sport before
suggesting anything; this is the same rule applied to what the human types.
The same guard matches the same trap phrased in English — "beat", "final
score", "who won", "last night" and the like — in one regex rather than one per
Locale. `!` in front of the topic overrides it, for when the human knows
better.

## Locales

A Locale is the whole bundle that changes when a Clip is written for a
different audience, not just the words: which voice reads it, how many
characters fit a line on the card, which country's trends feed `/trends`,
where the finished file lands, and which YouTube channel it publishes to —
the second channel's credentials are the only piece still to come. Moving
one of those without the others produces a Clip that is
wrong in a way nobody notices until it is on YouTube, so `app/locales.py`
moves them together as one unit, keyed on a short code (`th`, `en`).

An unknown or missing code degrades to Thai rather than raising, because a
Manifest written before Locales existed carries no `locale` field at all —
old clips keep working without a backfill.

`/en <topic>` writes and renders the clip in English, spoken by
`en-US-AndrewNeural`. `/trends en` pulls Google Trends US and the YouTube US
`mostPopular` chart instead of the Thai ones, and the numbered buttons under
that list produce English clips without typing `/en` yourself. A language
code the bot doesn't know after `/trends` is refused rather than guessed at;
bare `/trends` and `/trends th` are the same thing. All of the bot's own
messages, buttons and errors stay in Thai regardless
of which Locale the clip is in — only the Clip's own script, on-screen text,
captions and voice change.

Latin runs about 50px a character in Waree-Bold against Thai's 21px, so the
same line width would either overflow or shrink an English card's font to the
40px floor. English lines are capped at 24 characters (18 is the target given
to the model), and unlike Thai the count is *enforced*: the renderer's pixel
measurement alone would pass a 38-character Latin line and then draw it at the
minimum size, so the Locale also holds the model to a hard character count and
the renderer applies whichever cut — pixels or characters — is larger. Thai
keeps the pixel measurement only; its 34-character guidance to the model has
never been a hard limit.

The `spoken` field — the transliteration the voice actually reads, separate
from the `narration` shown as subtitles — mirrors the same rule the other way:
Thai `spoken` may hold no Latin characters, English `spoken` may hold no Thai
characters, because either voice mishandles the other script mid-sentence.
edge-tts reports a `SentenceBoundary` for `en-US-AndrewNeural` just as it does
for the Thai voice (3 cards in, 3 boundaries out, measured 2026-09-07), so
English narration goes through the same one-take-then-trim pipeline described
under [Pipeline](#pipeline) — no separate code path, just a different voice
and a space rather than nothing joining the words.

English files land in `/volume1/shorts/en` (`/output/en` inside the
container), Thai clips keep landing straight in `/output` as before.
`app/backfill.py` now walks `/output` with `rglob` instead of `glob` so it
finds both, and reads a reconstructed clip's Locale off its folder name
(`en` vs anything else).

`/experiment`'s hook factor carries the same two Variant names in both
Locales, but the clause text handed to the model is written in the clip's own
language rather than translated, so an English clip is not visibly writing
from a translated Thai instruction.

Every number is read per channel as well. `history.json` records the Locale
of each upload, and `history.video_ids()` / `recent_titles()` filter on it, so
the Gate, `/stats`, the snapshots and the prompt's own examples all count one
audience at a time — thirty clips split across two audiences is not thirty
data points about either of them (see `docs/adr/0008`). Entries and Manifests
written before Locales existed carry no field and are read as Thai.

Both channels are live as of 2026-09-07: Thai publishes to **FixHarDeZ**
(`@fixhardez`) and English to **Just Decoded It** (`@justdecodedit`), each
with its own OAuth credentials in the vault. Verified by asking each Locale's
token which channel it belongs to (`channels.list?mine=true`) — the check
worth repeating after any re-auth, since a token issued against the wrong
channel is otherwise indistinguishable from a right one until a clip is
already published.

## Both languages from one Topic

`/both <topic>`, or the 🌏 row under a Thai `/trends` list, makes the same Topic
in both Locales. The row only appears on a Thai list: a pair is always written
Thai first, so offering it under `/trends en` would answer a US search spike
with a Thai clip. A number button on an English list makes one English clip and
nothing else. Thai is written first and reviewed exactly as any other clip;
once its Clip is delivered the bot writes the English half on its own and
hands it back for review. One at a time rather than both at once — two Scripts
on screen would have to be read side by side on a phone, and every revision
would have to say which one it meant.

The English half is **written, not translated**. The approved Thai Script is
handed to the model as context — same angle, same facts — but English lines are
capped at 24 characters against Thai's 34 and the hooks that land differ, so a
translation comes back overflowing and flat. Both halves record the Thai half's
Clip id as `pair_id`, which is what makes "the same Topic, two audiences"
answerable later.

Either half failing stops there: a render that fails, or a Thai script you
discard with 🗑, cancels the English half and says so. The Variant is still
drawn independently for each half — locking them together would halve how fast
each channel collects its own arms.

The numbered buttons under `/trends` are unchanged and still make one Thai
clip; pairing is opt-in per Topic, because a Thai search spike is often about
something a US audience has no reason to care about, and a pair costs two
Scripts and two renders. The automatic rounds are per channel and configured
in the dashboard (see below); English ships switched off.

## Pipeline

```
topic → mimo → script (hook + cards + metadata) → your review
      → Pexels, or footage you made in Flow  ┐
      → Pillow (one card image per card)     ├→ ffmpeg composite + concat → mp4
      → edge-tts (one audio per card)        ┘
```

Cards sit on real footage, dimmed by a scrim so the text reads. When Pexels has
nothing for a card — or the key is missing entirely — that card falls back to a
gradient background with a slow Ken Burns move, and the render carries on.

The whole script is spoken in one edge-tts call, which is what keeps the
delivery continuous — but the paragraph break that makes the endpoint report
one sentence boundary per card also buys a paragraph-length pause: measured
~1.0s at every card join against 0.12-0.53s at the voice's own clause breaks.
The render cuts each card out at those boundaries, trims its trailing silence
back to 0.30s and joins the pieces again, so the joins sit inside the voice's
natural rhythm instead of reading as a series of announcements.

Every card carries its narration twice: `narration` keeps English spelled as
English and goes to the subtitles, `spoken` is the same sentence transliterated
into Thai script and is the only one the voice ever reads. A Latin word left in
`spoken` makes the voice switch to English mid-sentence, where it reads at
English pace — a rushed, unclear burst inside Thai speech — so the validator
rejects it.

Two pronunciation faults survive that. The model letter-spells a coined word it
does not read as a pun — "TH-AI Passport" came back as `ทีเอไอพาสปอร์ต`, said
"tee-ay-eye", where it is meant to be `ไทยพาสปอร์ต` — and the voice reads a
handful of correctly spelled Thai words wrong on its own. Neither is fixable by
rewriting the script: a rewrite returns the same spelling and the same audio.
`/say <wrong> = <right>` writes a substitution to `/data/say.json`, keyed on
Thai because Latin never reaches the voice, and it applies to every later clip.
The script shown for review prints a 🗣 line wherever `spoken` differs from
`narration`, which is the text to copy into the command. An entry only reaches
a clip that is synthesised after it, so `/redo` re-renders the last script with
the fix and without a rewrite; a clip already on YouTube is not replaced.

Thai has no final /s/ at all — ส ษ ศ ซ in a coda are all said /t/ — so "พาส"
is read "พาด" by the rules, not by a fault in the voice, and no correct
spelling will fix it. The way out is an override that either keeps the English
word or moves the s into the next syllable (`เอไอพาสะทีเอช`); `/say` values are
never validated, so a Latin one is allowed there even though `spoken` forbids
it. Substitution happens
inside `_tts_text()`, before `narrate()` joins the cards: doing it later would
leave the boundary check comparing pre-substitution text against what the voice
reports, failing alignment on every card and dropping the clip to per-card
speech without saying why.

Card timing follows the length of each card's audio, so the clip runs as long
as the narration takes. Each card also drifts — a slow zoom in or out that
spans exactly its narration, alternating direction card to card, so the clip
does not read as a slideshow. Cards are drawn 12% larger than the frame and the
zoom crops into that margin, which keeps the text at native resolution.

## Footage you generate yourself

Pexels is instant and generic. When the hook deserves better, press
**🎨 ทำ footage เอง** on the script instead of 🎬: the bot writes an English
Flow Prompt for the hook card, sends it in a code block you can copy with one
tap, and then gets out of the way. You paste it into the Google Flow app
(9:16), generate, download, and **reply to that same message with the mp4**.

The reply is what makes the matching exact — the message id says which card the
file belongs to, so nothing is inferred from filenames or the order things
arrive in. The bot files it under `/volume1/shorts/footage/<clip_id>/c00.mp4`,
which outlives the workdir, and offers a 🎬 button. Cards without supplied
footage still go to Pexels as usual.

While a clip is parked the bot is idle: send another topic, run any command.
What it will not do is park a second clip, or pick a topic on its own — you are
already busy with this one. A clip nobody sends footage for is written off
after 24 hours (`FLOW_PARK_HOURS`) and recorded as `abandoned`.

Telegram's Bot API will not serve the bot a file over 20MB, so send the video
normally and let Telegram compress it rather than sending it as a file.

The bot never calls a video model itself: Flow credits and the Veo API are
separate systems, and the API costs about $18 per clip at Veo 3.1 Standard
rates. See `docs/adr/0005`.

## Storyboards

Sometimes the shot list matters more than the stock clip. **📋 prompt ทำ
storyboard** on a script plans one for it and sends it back one scene at a
time: Thai above (what happens, what is heard, what the mood is), English
below in copy blocks — one prompt to make the frame, one to make the video
from it. The first message is the master character: generate that image in
Flow before anything else and use it as an ingredient, or the face changes
scene to scene.

The character lock is enforced, not requested. The storyboard names the
character once in a `locked_prompt_tag`, and validation rejects any scene whose
image prompt does not repeat that phrase word for word — along with any prompt
that is not English, omits the aspect ratio, or drops the `no text, no
watermark, no UI, no split-screen` tail.

The narration and on-screen lines are copied in from the script after the model
answers, never taken from its reply: those words are what the renderer will
speak and draw, and a storyboard that paraphrases them is a set of images for a
video that does not exist.

`/storyboard <บรีฟ>` does the long-form version: a one-line brief becomes a
16:9 storyboard (four scenes unless the brief asks for another number). There
is no script to lock against, so the voiceover is written by the model.

Both stop at prompts — this stack does not assemble long-form video, and
`docs/adr/0006` says why. Pressing 📋 changes nothing about the clip: the
script keeps its buttons, and you can press it again.

## What it records

Every Script the bot writes gets a Manifest under `/data/clips/<id>.json` —
the drafts in order (a revision is appended, never overwritten), the render
parameters, the Card start times, and later the publication and its numbers.
It is written whether or not the clip is ever uploaded: keeping only the
scripts that survived review would flatter whichever way of writing produces
the ones you happen to throw away (`docs/adr/0004`).

The workdir is deleted after every render, so anything not captured there is
gone for good.

Once a day, after `SNAPSHOT_HOUR` (default 10:00), the bot pulls views, likes,
shares, comments, subscribers gained and retention for the youngest published
clips inside a 30-day window — the id filter is a URL and only 50 fit, so the
newest are kept and the oldest dropped — and appends a dated snapshot to each
Manifest. A failed pull still marks the day rather than retrying on every poll
tick; a missed day costs nothing, since the day-7 reading is the first one
taken at age seven or later. It rides the
Telegram poll loop rather than a scheduler thread — `getUpdates` already wakes
every 30 seconds — so the stack still has no port and no listener. `/snapshot`
runs it on demand. The **day-7** snapshot is the official figure: retention
keeps moving as views accrue, and comparing "latest" numbers compares old clips
to new ones instead of one way of writing to another.

The nine clips published before Manifests existed are reconstructed at startup
from the `.txt` and `.srt` left in `/output` — title and card boundaries
survive there, nothing else does — and flagged `reconstructed`.

Reading the numbers back is deliberately gated. Until the channel has 30 clips
(and, once experiments start, 300 views per variant), `winning_examples()`
returns nothing and `/stats` says in words that the figures cannot be used to
decide anything. Measured on 2026-08-27: 9 clips, 206 views, 182 of them on a
single clip — feeding that back into the prompt is learning from one sample.
The "do not repeat these titles" list keeps working; deduplication is not
inference.

## Where viewers leave

`/retention` draws one clip's retention curve with its card boundaries on it,
marks the cliffs, and names the card that was on screen at each one. It is the
reason the Manifest records card start times: `elapsedVideoTimeRatio` is a
fraction of the clip, and turning that back into a card needs the clip's own
duration and boundaries.

A cliff is a fall at least twice the clip's typical step and at least 5% of the
curve's height; neighbouring buckets are merged, since one cliff usually spans
two or three. Everything below that is the ordinary slope every clip has.

YouTube builds these curves only once a clip has been watched enough —
measured on this channel: 361 views yes, 27 views no — so `/retention` walks
back from the newest published clip until it finds one with data.

## Finding something to make

`/trends` reads two outside signals — Google Trends' RSS feed for Thailand
(what people search for, with volumes and the headline behind each spike) and
YouTube's own `mostPopular` chart for TH (what they watch) — and asks the model
to turn them into five topics you could actually be given. The raw rows are
sent too, so a suggestion that drifted from its source can be caught against
it. `/trends en` reads the same two signals scoped to the US (Google Trends
US, YouTube's `mostPopular` for US) and suggests English topics instead — see
[Locales](#locales).

The suggestions carry a numbered button each, so picking one is a tap rather
than retyping a Thai sentence; typing a topic still works and is still the only
way to send one the model did not suggest. The button carries the list's
timestamp as well as the index, because the index alone means nothing: run
`/trends` twice and button 3 on the older message points into the newer list,
which would start writing a topic nobody chose. A tap on a superseded list is
refused. A tap while a script is waiting for review is refused too — starting a
new topic there would abandon the pending one without marking it discarded,
which is what 🗑 is for.

News, politics, sport results and anything about a real person are kept out.
YouTube rows carry a category, so 25 (News & Politics) and 17 (Sports) are
dropped before the model sees them; Google Trends rows carry no category, so
there the prompt and your own choice are the only filter — a politician and a
live match both reached the model in testing, and it declined them. That is why
the raw rows are always printed: on that path a bad suggestion is only
catchable against its source. This is not squeamishness — it is the one place
where a model writing confidently about a live story publishes an invented
claim about a named person under your channel's name.

Topics are no longer locked to DevOps/AI. A search of Thai short-form for
`devops ไทย` over 30 days returns nothing at all, so the lock was buying clean
experiments on an audience that does not exist. Each script now names its own
category, and `/experiment` reports how the categories did — labelled as an
observation, because you choose the topics and nothing about that is
randomised.

### Running itself

The bot also calls `/trends` on its own, on a schedule you set per channel at
`/settings` in the dashboard: which hours (Asia/Bangkok), how long the list
waits, and whether that channel runs unattended rounds at all. That list
carries one extra button, ✋, and a deadline: leave it alone and the bot picks
one of the five suggestions at random, writes the script and renders the clip
unattended. ✋ calls off that round and leaves the numbered buttons pressable,
in case you change your mind about a topic but not about the schedule.

The schedule lives in `/config/schedule.json` on a volume of its own, which the
dashboard writes and the bot reads `:ro`. It is the only thing in this stack the
dashboard may write — docs/adr/0009 explains what that costs and what it does
not. An edit takes effect on the next poll tick; nothing restarts. English
defaults to off, because switching it on means clips appearing on that channel
with nobody reviewing them. `TRENDS_HOURS` and `AUTO_PICK_MINUTES` remain the
defaults for a container that has never been given a schedule, so an untouched
deployment behaves exactly as it did before this existed.

Nothing about this reaches YouTube. Uploading is still a button under the
finished clip — outward-facing, irreversible, and the one step ADR 0001 keeps
in a human's hands.

Details that are load-bearing rather than cosmetic:

- Only the newest passed hour is ever owed, and the slot is stamped *before*
  the run starts. A bot that was down all day comes back and produces one list,
  not three, and a run that takes minutes is not started again 30 seconds later.
- An unattended script is posted without the 🎬/🗑 keyboard and its message id
  is not tracked. `do_render` rewrites the message it tracks, which would erase
  the only copy of the script nobody was there to read.
- The deadline fires only while the bot is idle. If you are mid-script when it
  passes, the pending pick is dropped rather than queued behind your clip.
- Starting any topic — typed, tapped or automatic — cancels a pending pick.
- The ✋ button carries the list's timestamp, like the numbered ones: a tap on
  yesterday's message must not call off today's run.

## The experiment

One factor is varied at a time, currently the hook: a Clip opens either with a
shock number or with a question. The Variant is drawn at random when a Topic
arrives — before the Script exists — and never re-rolled, so rewriting a script
you dislike cannot quietly pick the winner. One Clip in three is an **Explore
clip** instead: written deliberately outside the pattern, flagged, and left out
of every calculation. A loop that only ever learns from its own past stops
improving.

The clause that defines a Variant is stored verbatim in the Manifest, because
the base prompt drifts and a Variant name alone would not say what it meant on
the day.

`/experiment` reports clips, discard rate, views and median day-7 retention per
Variant. It names a winner only when both arms have 10 clips and 300 views and
their medians differ by at least 5 percentage points; below that it says
*inconclusive*, which is a result and not a failure. The discard rate is a
signal in its own right — a Variant whose scripts you keep throwing away is
losing, whatever its retention says. English clips run the same factor with a
clause written in English (see [Locales](#locales)), and each Locale is
counted on its own: `/experiment` prints one section per channel, and the
thresholds (10 clips, 300 views, 5 points) are unchanged — each channel
simply reaches them separately. Thai is always shown; another Locale appears
once it has Clips of its own, so the report reads exactly as it did until the
second channel produces something. Categories are split the same way, or the
Thai `เทค` and the English `tech` would sit in one table as two unrelated
rows.

## Uploading to YouTube

Once configured, the bot puts an "อัปโหลดขึ้น YouTube" button under each
finished clip. Publishing is the one step that stays behind a tap: it goes
outward and cannot be taken back quietly.

Each button carries the id of the Clip it was sent under, so several finished
clips can wait with live buttons at once — a Locale pair produces two minutes
apart — and pressing the older one uploads the clip it belongs to, through
that clip's own channel. (Buttons sent before ids travelled in the callback
data still mean "the last thing rendered", which is what they meant when they
were sent.)

Set it up once with `python3 scripts/youtube_auth.py <client_id> <client_secret>`
— that file's docstring lists the Google Cloud console steps. The consent screen
must be set to **In production**; left in "Testing", refresh tokens expire after
7 days and uploads start failing with `invalid_grant`.

The clip's first frame — the hook card — is set as the thumbnail straight
after upload. Note what that does and does not cover: it is the thumbnail on
search, the channel page and suggestions, but **the Shorts feed ignores custom
thumbnails** and picks its own frame. There is no API for the Shorts cover; only
the YouTube mobile app can set it (Edit → Cover, where the first option is the
opening frame). The bot says so after each upload. That needs a phone-verified channel; without one YouTube refuses
with a 403 and the video keeps its auto-generated thumbnail, which is reported
but does not count as a failed upload.

With no credentials in the vault, the button simply never appears.

There is no second channel yet, but the code now knows an English clip needs
one: every YouTube setting — client credentials, category id, privacy,
caption language — is read under the Locale's own env prefix (`YOUTUBE_` for
Thai, `YOUTUBE_EN_` for English), with no shared fallback between them.
Setting `YOUTUBE_EN_*` is exactly what makes the upload button appear on
English clips; while it stays unset, `deliver()` never shows the button under
an `/en` clip, and `youtube.upload()` refuses outright if it were called for
that locale anyway. `do_upload()` uses the locale snapshotted at deliver
time, so the button — once it exists — always uploads through the same
channel the clip was written for. Category id and privacy are per-Locale
too (`YOUTUBE_EN_CATEGORY_ID` / `YOUTUBE_EN_PRIVACY`, same defaults `28` and
`public` as Thai). With no `YOUTUBE_EN_*` set, `/en` clips get no button at all and the file has
to be copied from `/volume1/shorts/en` and uploaded by hand; that was the
state until 2026-09-07.

### Provisioning a channel (done once per Locale)

The order matters, because `scripts/render_env.py` refuses to build *any*
stack's `.env` while a manifest points at a vault path that does not exist —
adding the env names before the values would break `make secrets` for the
whole repo, not just this stack. So:

1. In the Google Cloud console, create the project (or reuse one), enable
   **YouTube Data API v3**, and set the OAuth consent screen to **In
   production**. Left in "Testing", the refresh token dies after 7 days
   (`docs/adr/0001`).
2. Run `python3 scripts/youtube_auth.py <client_id> <client_secret>` **signed
   in as the account that owns the English channel** — Google issues the token
   for whichever channel completes the consent screen.
3. `make edit-vault`, and put the three values under
   `stacks.shorts_factory.youtube_en.{client_id,client_secret,refresh_token}`.
   Writing them over `stacks.shorts_factory.youtube.*` silently redirects
   every Thai upload to the English channel.
4. Only now add the mappings to `shorts-factory/secrets.manifest.yaml`:
   `YOUTUBE_EN_CLIENT_ID`, `YOUTUBE_EN_CLIENT_SECRET`,
   `YOUTUBE_EN_REFRESH_TOKEN`.
5. `make secrets && ./scripts/deploy.sh -s shorts-factory -y`.

The upload button appears on English clips from the next render, and `/stats`,
`/experiment` and the daily snapshots start reporting the English channel
beside the Thai one on their own.

## Subtitles, history and `/stats`

Each upload gets a caption track tagged for the clip's own Locale — English
gets `language: en`, Thai gets `th` — built from the same sentence boundaries
the video is cut on, and the `.srt` is kept beside the mp4.

Uploads are recorded in `/data/history.json`, each with the Locale it went out
under. Recent titles go into the prompt so the bot stops repeating itself, and
`/stats` reports views and retention per clip — sorted by how much of each clip
was actually watched, which is the number that matters for Shorts. The top
performers are fed back in as examples. All of it is read per channel: the
titles a Thai audience watched are not shown to the model writing for a US one,
and `/stats` prints one report per channel that has credentials, which today is
the Thai one alone. Everything here only sees clips uploaded through the bot.

The daily snapshot does one Analytics pull per channel, each with that
channel's own token — a video id belonging to the other channel does not
error, it simply returns no row, which would read as "not processed yet". A
channel with no credentials is skipped, and one that fails does not cost the
other its reading. `/retention` takes the channel from the clip's own
Manifest, so there is no way to ask the wrong one.

## Background music

Put royalty-free tracks in `/volume1/shorts/bgm/` and one is picked at random
per clip, ducked under the narration by a sidechain compressor keyed on the
speech itself. An empty or missing folder means no music, which is the default.

Nothing downloads music automatically — the risk with background music is
Content ID, not plumbing, so the tracks are yours to vet.

## Configuration

`.env` is generated from the vault — see `secrets.manifest.yaml`. Never edit
it by hand.

```bash
make secrets                    # render .env from vault + manifest
./scripts/deploy.sh -s shorts-factory -y
```

| Key | Default | What it does |
| :--- | :--- | :--- |
| `TRENDS_HOURS` | `8,12,17` | Thai's default hours, until a schedule is saved in the dashboard |
| `AUTO_PICK_MINUTES` | `15` | default wait before the list picks itself, same |
| `CONFIG_DIR` | `/config` | where `schedule.json` lives; the dashboard's only writable mount |
| `FLOW_PARK_HOURS` | `24` | how long a clip waits for footage you generate in Flow |
| `FLOW_PROMPT_TIMEOUT_SECONDS` | `180` | cap on writing one Flow Prompt |
| `STORYBOARD_TIMEOUT_SECONDS` | `300` | cap on planning one storyboard |
| `TTS_VOICE_EN` | `en-US-AndrewNeural` | the English Locale's voice, used by `/en` and `/trends en` clips |

The `/volume1/shorts` shared folder must exist on the NAS before first run;
create it in DSM (Control Panel → Shared Folder), it is not created by the
stack.

## Dashboard

A read-only view of everything the bot has already written down, served at
`http://<NAS_HOST>:5071` behind nginx basic auth (credentials from the vault,
see below). It runs as its own container from the same image as the bot with
`/data` mounted `:ro`, and the app itself declares no route other than GET/HEAD
— read-only twice over, by mount and by code (`docs/adr/0007`). It carries no
`env_file`, so the Telegram bot token and the YouTube refresh token never
reach this LAN-facing process.

Four pages:

| Page | Answers |
| :--- | :--- |
| `/` | every Clip, newest first, with day-7 views/retention once available |
| `/clip/{id}` | one Clip's full Manifest — every Script draft (including discarded ones), render detail, snapshots |
| `/experiment` | the two hook arms, their medians, and the verdict once enough data exists |
| `/now` | the bot's live `state.json`, its `say.json` overrides, and recent uploads |

The pages lead with the day-7 figures — Gate progress, median retention, total
views — and the clip list filters by outcome in the browser, so no route grew a
query parameter. `/clip` draws views over age as an **inline SVG built in the
template**: `app/retention.py` renders its PNGs with Pillow, and keeping Pillow
out of the LAN-facing process is a property `docs/adr/0007` asserts, guarded by
`test_no_drawing_library_in_this_process`. Theme follows the system with a
toggle kept in `localStorage`; the assets ship inside the image (no CDN, no web
fonts), so a visual change needs a rebuild and deploy, not a file copy.

Set it up once with:

```bash
htpasswd -cB shorts-factory/nginx/.htpasswd <username>   # gitignored; use the vault password
```

Nothing on this surface can start a render, edit a Script, or touch YouTube —
that all still happens in Telegram, on the phone.

`/healthz` sits behind the same basic auth as every other path on this
dashboard, so an Uptime Kuma monitor pointed at it must be given the vault
credentials — unlike ops-bot's `/webhook/uptime-kuma`, there is no
unauthenticated path here by design.

## Thai text rendering

Pillow's wheel does not include Raqm. Without the `libraqm0` package,
`ImageFont.Layout.RAQM` quietly degrades to basic layout and Thai tone marks
are dropped — no error, just wrong pixels. The Dockerfile installs it and then
asserts `features.check('raqm')` so a broken image fails the build instead of
shipping. ffmpeg's `drawtext` does no shaping at all and is not an alternative.

The face is Waree Bold from `fonts-thai-tlwg`, chosen because it covers Thai
and Latin in one file. Noto Sans Thai does not: its cmap has no A-Z or digits,
so `เก็บ log ไว้` comes out with the English word as empty boxes.
