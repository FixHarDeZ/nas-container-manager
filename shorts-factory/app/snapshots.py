"""The daily pull: how every published Clip is doing, written into its Manifest.

YouTube Analytics runs days behind, so a Clip's numbers are only final long
after the human has stopped looking at it. Asking on demand would mean the
figures depend on which day someone happened to press /stats; a Clip measured
once a day for its first month gives every Experiment the same yardstick — the
day-7 snapshot (docs/adr/0004).

This is the scheduler ADR 0002 said the stack did not have. It makes outbound
calls only, on the poll loop's own thread: no port, no listener, no new
dependency.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

import httpx

from app import analytics, locales, manifest, youtube

logger = logging.getLogger(__name__)

# Analytics is several days behind, so pulling before mid-morning local time
# mostly re-reads yesterday's numbers.
HOUR = int(os.environ.get("SNAPSHOT_HOUR", "10"))
# A Clip stops being interesting long before this, but nothing is gained by
# forgetting it earlier.
MAX_AGE_DAYS = 30
METRICS = (
    "views,likes,shares,comments,subscribersGained,"
    "averageViewPercentage,averageViewDuration,estimatedMinutesWatched"
)


def due(state: dict, now: datetime | None = None) -> bool:
    """Once a day, after HOUR. Missing a day means running at the next tick."""
    now = now or datetime.now()
    if now.hour < HOUR:
        return False
    return state.get("last_snapshot") != now.date().isoformat()


def _age(record: dict, today: date) -> int | None:
    stamp = record.get("published_at") or record.get("created_at")
    try:
        return (today - datetime.fromisoformat(stamp).date()).days
    except (TypeError, ValueError):
        return None


def _wanted(records: list[dict], today: date) -> dict[str, dict]:
    """Published Clips young enough to still be moving, keyed by video id."""
    out = {}
    for record in records:
        video_id = record.get("video_id")
        age = _age(record, today)
        if video_id and age is not None and 0 <= age <= MAX_AGE_DAYS:
            out[video_id] = record
    return out


async def _rows(client: httpx.AsyncClient, ids: list[str],
                locale: str = locales.DEFAULT) -> list[list]:
    token = await youtube._access_token(client, locale)
    reply = await client.get(
        analytics.REPORTS_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "ids": "channel==MINE",
            "startDate": (date.today() - timedelta(days=MAX_AGE_DAYS + 5)).isoformat(),
            "endDate": date.today().isoformat(),
            "metrics": METRICS,
            "dimensions": "video",
            "filters": "video==" + ",".join(ids),
        },
    )
    if reply.status_code != 200:
        raise analytics.AnalyticsError(
            f"ดึง snapshot ไม่ได้ ({reply.status_code}): {reply.text[:300]}"
        )
    return reply.json().get("rows", [])


async def run() -> int:
    """Write today's snapshot for the youngest published Clips. Returns how many.

    A Clip with no rows yet is not an error — Analytics simply has not
    processed it — so it is skipped and picked up on a later day.
    """
    today = date.today()
    wanted = _wanted(manifest.load_all(), today)
    if not wanted:
        return 0

    # One pull per channel, never one pull for both: each Locale publishes to
    # its own channel (docs/adr/0008), and asking a channel about a video id it
    # does not own is not an error — the row simply does not come back, which
    # reads as "Analytics has not processed it yet".
    by_locale: dict[str, list[str]] = {}
    for video_id, record in wanted.items():
        by_locale.setdefault(record.get("locale", locales.DEFAULT), []).append(video_id)

    rows: list[list] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for locale, ids in by_locale.items():
            if not youtube.configured(locale):
                # A Locale whose channel has no credentials yet. Its Clips were
                # uploaded by hand, so there is nothing here to ask about.
                logger.info("ข้าม snapshot ของภาษา %s — ยังไม่มี credential", locale)
                continue
            # Youngest first: the filter is a comma-joined URL and only so many
            # ids fit, and at 3 clips a day the 30-day window outgrows that cap
            # within a fortnight. The newest Clips are the ones still moving and
            # the ones whose day-7 reading has not been taken yet, so they are
            # the ones that must not be dropped. The cap is per request, so each
            # channel gets its own 50.
            # ponytail: one batch per channel. Chunk if the 30-day tail matters.
            youngest = sorted(
                ids, key=lambda v: wanted[v].get("published_at") or "", reverse=True
            )
            try:
                rows += await _rows(client, youngest[: analytics.MAX_VIDEOS], locale)
            except Exception:
                # One channel failing must not cost the other its daily reading.
                logger.exception("ดึง snapshot ของภาษา %s ไม่สำเร็จ", locale)

    written = 0
    for row in rows:
        record = wanted.get(row[0])
        if record is None:
            continue
        manifest.add_snapshot(record["id"], {
            "date": today.isoformat(),
            "age_days": _age(record, today),
            "views": row[1],
            "likes": row[2],
            "shares": row[3],
            "comments": row[4],
            "subscribers_gained": row[5],
            "percent": row[6],
            "seconds": row[7],
            "minutes_watched": row[8],
        })
        written += 1
    return written
