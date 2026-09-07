# English clips publish to a second YouTube channel, not the Thai one

shorts-factory now writes English clips as well as Thai ones — a second
Locale, `en`, with its own voice, its own line-width budget, its own trends
feed, and its own output folder (`app/locales.py`). The one thing it does not
yet get is its own destination: the locale-aware routing that keeps an
upload on its own channel now exists, but the second OAuth client does not,
so the upload button is simply absent on English clips until that credential
is provisioned. This ADR is the decision to build that second client rather
than publish English clips on the Thai channel, and it is written now, before
the credential exists, so the shape is settled before batch 2 wires it in.

YouTube does not serve a channel to a neutral audience. It reads the
channel's own language and its subscriber and viewing history and feeds the
channel to the people who already watch it, and it feeds new clips from that
channel to people who look like those viewers. The Thai channel's audience is
Thai speakers who found it through Thai Shorts. An English clip posted there
does not reach a new, English-speaking audience; it reaches the same Thai
viewers, in a language most of them did not ask for, and it dies exactly as
described in ADR 0004 — few views, no retention curve, nothing for
`/retention` to walk back through. The failure is not neutral, either: a clip
that under-performs on a channel drags on how the platform reads that
channel's next recommendation. Post enough English clips to a Thai channel
and the Thai clips' own distribution gets worse, not just the English ones'.
Two audiences were never going to share one feed for free.

The sharper reason is arithmetic, and it is ADR 0004's arithmetic. The Gate
that keeps this bot from drawing conclusions from a handful of clips is
stated per channel: 30 clips before `winning_examples()` may turn back on, 10
clips and 300 views before an experiment's two variants are allowed to
declare a winner. Those thresholds assume every clip counted is answering the
same question — does this channel's audience prefer variant A or variant B.
Mix in clips written for a different audience, in a different language, and
the count still climbs but the question underneath it stops being singular.
Thirty clips split across two audiences is not thirty data points about one
audience; it is two smaller, noisier experiments wearing one shared counter,
and the Gate would open on a number that no longer means what ADR 0004 built
it to mean. A channel per locale keeps each Gate honest at the cost of it
opening later.

The alternative actually on the table was cheaper: keep one channel, write
`locale` onto the Manifest — which already happens, it is how the dashboard
and `backfill.py` would tell an English clip from a Thai one — and let the
existing upload path publish both. It would have saved a second OAuth consent
screen, a second set of secrets to provision, and the per-locale credential
routing that has since been built. It was
rejected for the reason above: it does not fix the audience-mixing problem,
it only makes it invisible, because the numbers `/stats`, `/experiment` and
`/retention` are already reading would need to learn to filter by locale
themselves before they meant anything again, and nothing forces that
filtering to actually happen. A field on a Manifest is data; it is not a
policy, and the easy version of this decision was to ship the field and stop,
which is exactly the version that leaves every existing number quietly wrong
for two languages at once.

So the consequence is a second credential, not a second flag. `scripts/
youtube_auth.py` has to run again, against a project whose consent screen is
already `In production` — ADR 0001 is the record of what happens when that
step is skipped: a refresh token that dies in seven days because the consent
screen was left in `Testing`, discovered only when the upload that had worked
all week suddenly stopped. The env prefix is already reserved
(`locales.py`'s `youtube_prefix` is `YOUTUBE_EN_` for the `en` Locale, mirroring
the Thai `YOUTUBE_` prefix) and the vault path is `stacks.shorts_factory.
youtube_en.*`, alongside the existing `stacks.shorts_factory.youtube.*`.
Past that credential, `experiment.report()` needs to count clips and views
per locale rather than pooling them, `/stats` and `/retention`'s history need
the same split, and the dashboard needs a locale column so a human reading it
does not have to guess which channel a row belongs to.

The locale-aware routing itself has since landed: `youtube.py` reads every
setting — client credentials, category id, privacy, caption language — under
the Locale's own env prefix (`YOUTUBE_` for Thai, `YOUTUBE_EN_` for English),
with no shared fallback between them. `deliver()` only shows the upload
button when `youtube.configured(locale)` is true for that clip's own locale,
and `youtube.upload()` refuses outright if called for a locale with no
credentials configured. `do_upload()` carries the locale snapshotted at
deliver time through upload, thumbnail and captions, so the track language
follows the clip rather than defaulting to `th`. Publishing an English clip
on the Thai channel is now impossible by construction rather than prevented
by convention or the `/help` text — two tests hold that line,
`test_an_english_clip_never_uploads_through_the_thai_channel` and
`test_the_upload_uses_the_locale_the_clip_was_delivered_with`. What remains
is the credential itself: until `YOUTUBE_EN_*` is provisioned, the button
never appears on an English clip at all, and the file is still copied by
hand from `/volume1/shorts/en` and uploaded manually.
