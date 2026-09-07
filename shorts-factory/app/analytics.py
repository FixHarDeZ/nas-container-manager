"""How the published clips actually performed.

Read-only, and only ever asked about videos this bot uploaded — the channel is
never enumerated. Needs the `yt-analytics.readonly` scope.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from app import history, locales, youtube

logger = logging.getLogger(__name__)

REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
LOOKBACK_DAYS = 90
MAX_VIDEOS = 50   # the filter is a comma-joined list; keep the URL sane

# The Gate (docs/adr/0004): below it the numbers are noise and the system is
# forbidden to feed its own results back into the prompt. Measured 2026-08-27
# with 9 clips: 206 views total, 182 of them on one clip, median 3. Learning
# from that is fitting a single data point.
GATE_CLIPS = 30
GATE_VIEWS_PER_VARIANT = 300   # enforced once experiments assign Variants


class AnalyticsError(RuntimeError):
    """Stats could not be fetched. Never fatal — nothing else depends on them."""


async def performance(locale: str = locales.DEFAULT) -> list[dict]:
    """Views and retention per uploaded video, best retention first.

    Sorted by how much of the clip people actually watched rather than by
    views: for a Shorts channel that is the number that says whether the
    writing worked.
    """
    ids = history.video_ids(locale)[-MAX_VIDEOS:]
    if not ids:
        return []

    async with httpx.AsyncClient(timeout=60) as client:
        token = await youtube._access_token(client, locale)
        reply = await client.get(
            REPORTS_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "ids": "channel==MINE",
                "startDate": (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat(),
                "endDate": date.today().isoformat(),
                "metrics": "views,averageViewDuration,averageViewPercentage",
                "dimensions": "video",
                "filters": "video==" + ",".join(ids),
            },
        )
    if reply.status_code != 200:
        raise AnalyticsError(f"ดึงสถิติไม่ได้ ({reply.status_code}): {reply.text[:300]}")

    rows = reply.json().get("rows", [])
    result = [
        {
            "video_id": row[0],
            "title": history.title_of(row[0]),
            "views": row[1],
            "seconds": row[2],
            "percent": row[3],
        }
        for row in rows
    ]
    result.sort(key=lambda r: (r["percent"], r["views"]), reverse=True)
    return result


async def latest_data_date(locale: str = locales.DEFAULT) -> str | None:
    """The most recent day YouTube has processed.

    Analytics runs a few days behind, so a clip uploaded today has no rows yet.
    Reporting the cut-off turns an empty result into something explainable
    instead of something that looks broken.
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            token = await youtube._access_token(client, locale)
            reply = await client.get(
                REPORTS_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "ids": "channel==MINE",
                    "startDate": (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat(),
                    "endDate": date.today().isoformat(),
                    "metrics": "views",
                    "dimensions": "day",
                },
            )
        rows = reply.json().get("rows", []) if reply.status_code == 200 else []
        return rows[-1][0] if rows else None
    except Exception:
        logger.exception("หาวันล่าสุดของข้อมูลไม่ได้")
        return None


def gate_note(locale: str = locales.DEFAULT) -> str | None:
    """What is still missing before conclusions are allowed, or None if past it.

    Counted per channel (docs/adr/0008): thirty Clips split across two
    audiences is not thirty data points about either of them, so each Locale
    reaches the same threshold on its own.
    """
    published = len(history.video_ids(locale))
    if published >= GATE_CLIPS:
        return None
    return (
        f"⚠️ ข้อมูลยังไม่พอสรุปอะไร — มี {published}/{GATE_CLIPS} คลิป "
        f"(ต้องมี {GATE_VIEWS_PER_VARIANT} views ต่อ variant ด้วยตอนเริ่มทดลอง). "
        "ตัวเลขข้างล่างดูได้ แต่ยังห้ามใช้ตัดสินใจ และบอทยังไม่ป้อนกลับเข้า prompt"
    )


def format_report(rows: list[dict], as_of: str | None = None,
                  locale: str = locales.DEFAULT) -> str:
    if not rows:
        lag = f" ข้อมูลล่าสุดที่ YouTube ประมวลผลคือ {as_of}" if as_of else ""
        gate = gate_note(locale)
        head = f"{gate}\n\n" if gate else ""
        return (
            f"{head}ยังไม่มีสถิติ — นับเฉพาะคลิปที่อัปผ่านบอทตัวนี้ "
            f"และ YouTube ประมวลผลช้ากว่าปัจจุบันหลายวัน{lag}"
        )
    gate = gate_note(locale)
    lines = [f"📊 {len(rows)} คลิปล่าสุด (เรียงตาม % ที่คนดูจนจบ)", ""]
    if gate:
        lines = [gate, ""] + lines
    for row in rows:
        lines.append(
            f"{row['percent']:.0f}% · {row['views']} views · {row['seconds']:.0f}s\n"
            f"   {row['title']}"
        )
    return "\n".join(lines)


async def winning_examples(limit: int = 3,
                           locale: str = locales.DEFAULT) -> list[str]:
    """Titles worth writing more like — nothing at all before the Gate.

    Feeding the top performers back into the prompt is the whole point of the
    loop, and it is exactly what must not happen while one clip holds 88% of
    the channel's views: the model would learn from a single sample and drift
    off the locked niche. See docs/adr/0004.
    """
    if gate_note(locale) is not None:
        return []
    try:
        rows = await performance(locale)
    except Exception:
        logger.exception("ดึงสถิติเพื่อป้อน prompt ไม่สำเร็จ")
        return []
    return [r["title"] for r in rows[:limit] if r["views"] > 0]
