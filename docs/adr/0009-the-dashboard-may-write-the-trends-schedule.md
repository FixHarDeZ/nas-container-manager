# The dashboard may write the trends schedule, and nothing else

ADR 0007 gave the dashboard two independent guards: `/data` mounted `:ro`, and
`test_no_route_can_write` asserting that no route in `dashboard.app` allows a
method other than GET or HEAD. This decision keeps the first guard exactly as
it was and narrows the second from "no route writes" to "one route writes, and
it is this one".

The thing being changed is when the bot goes looking for a Topic on its own.
That used to be `TRENDS_HOURS` and `AUTO_PICK_MINUTES` in the environment, read
at import, Thai only. Changing an hour meant `make edit-vault`, `make secrets`,
`./scripts/deploy.sh` and a restart — for a number that is a matter of taste
and gets tuned by watching what the channel does. Now that there are two
channels with two audiences in two time zones, it gets tuned twice as often and
the two are not the same number.

## What the write can reach

A second volume, `shorts_factory_config`, holding one file:
`/config/schedule.json`. The dashboard mounts it read-write; the bot mounts it
`:ro`, because the dashboard owns the file and the bot only obeys it. `/data`
is still `:ro` on the dashboard, so Manifests, `state.json`, `say.json` and
`history.json` remain unreachable from the LAN-facing process — verified on the
NAS, not just asserted: writing under `/data` from that container fails with
`Read-only file system`.

The dashboard still carries no `env_file`. That was the strongest claim in ADR
0007 and it is untouched: the Telegram bot token and both channels' YouTube
refresh tokens are absent from the one process reachable from the LAN. The
worst an attacker past nginx's basic auth can do with this route is change what
hours a trends list is posted at, or switch a channel's unattended rounds on.
That is real — an unattended round writes and renders a Clip — but it cannot
publish, because uploading is still a button pressed in Telegram (ADR 0001).

## Why not keep the property intact

Two alternatives were considered and dropped. Putting the schedule behind a
Telegram command keeps ADR 0007 whole, but the schedule is a table of numbers
per channel and Telegram is a bad place to read or edit a table. Having the
dashboard render a command for a human to paste into Telegram keeps the
property and makes the feature worse than doing nothing.

The honest position is that the read-only property was worth having but is not
worth more than the feature. What is worth keeping is the *bounded* version of
it: the exception is one route, on one volume, holding one file whose contents
are three fields per Locale. `WRITING_ROUTES` in `tests/test_dashboard.py`
names it, and a second writing route added anywhere else fails the test rather
than being a judgement call in review.

## Input is treated as hostile

`schedule.validate()` rejects the whole request rather than storing the part
that parsed — a half-applied schedule is the half nobody checked. Hours must be
integers in 0-23, at most 12 a day; the wait must be 1-240 minutes; a Locale
must be one the code knows; and a Locale switched on with no hours is refused,
because it reads as "on" in the browser and behaves as "off" in the bot. The
file is written to a temporary name and renamed into place, since the bot reads
it on every poll tick and must never see half of it. A file that will not parse
falls back to the defaults with a warning: a bot that has lost its trends
rounds is a smaller failure than a bot that will not start.

## The environment is still the default

A container that has never been given a schedule behaves exactly as it did
before this file existed — `TRENDS_HOURS` and `AUTO_PICK_MINUTES` supply Thai's
row, and there is a test that says so. English defaults to `enabled: false`.
Turning a channel's unattended rounds on means Clips appearing on it with
nobody reviewing them, which is a decision for a human and not a side effect of
deploying this ADR.

## What this does not change

The bot still publishes no port and still has no HTTP surface of its own (ADR
0002, as amended by 0007). It still takes one job at a time: a round is started
only from an idle state and only one Locale's round runs at once, marked in
`state["trends_running"]` for its duration, because two overlapping rounds
would leave one `suggested` list in state with the other round's buttons still
on screen — a 💡 button that writes about something the human never saw, now
reachable since two Locales can be scheduled for the same hour. The other
Locale keeps its slot owed and goes out on a later tick.
