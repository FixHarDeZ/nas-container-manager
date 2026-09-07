"""Where viewers leave, and which Card was on screen when they did.

The Analytics API does serve per-second retention for Shorts — verified
2026-08-27 against `v7ljwc_6_jM` (PT21S, 361 views): 100 rows of
`audienceWatchRatio` over `elapsedVideoTimeRatio`, one per 1% of the clip. Two
other Shorts on the same channel with 27 and 12 views returned nothing, so the
gate is views, not format: a Clip nobody watched has no curve to read.

`elapsedVideoTimeRatio` is a fraction of the Clip, so a Card's boundaries turn
into buckets by multiplying by the Clip's own duration — which is why the
Manifest records the Card start times.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta

import httpx
from PIL import Image, ImageDraw

from app import analytics, locales, render, youtube

logger = logging.getLogger(__name__)

# How much steeper than the Clip's own typical step a fall must be before it is
# worth pointing at. Every clip loses viewers everywhere; only the cliffs matter.
STEEPNESS = 2.0
# ...and a floor, as a fraction of the curve's own height. Without it a clip
# whose typical step is near zero would have every wobble called a cliff, and a
# clip with exactly one fall could never have one: its own fall would be the
# median it is compared against.
MIN_DROP = 0.05
# A cliff usually spans two or three buckets. Reporting them separately names
# the same moment three times and stacks three labels on top of each other.
CLUSTER_GAP = 0.03
TOP_DROPS = 3

W, H = 1100, 620
PAD_L, PAD_R, PAD_T, PAD_B = 90, 40, 60, 90
BG = (18, 22, 28)
GRID = (48, 56, 66)
LINE = (255, 210, 74)
DROP = (255, 96, 96)
FG = (232, 236, 242)
DIM = (150, 160, 172)


class NoCurve(RuntimeError):
    """YouTube has no retention data for this Clip yet."""


@asynccontextmanager
async def _borrow(client: httpx.AsyncClient | None):
    """Use the caller's client, or open one for this call alone."""
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=60) as own:
        yield own


async def fetch(video_id: str, client: httpx.AsyncClient | None = None,
                locale: str = locales.DEFAULT) -> list[list]:
    """Rows of `[ratio, audienceWatchRatio, relativeRetentionPerformance]`.

    Takes a client so a caller walking several Clips does not refresh the
    access token once per Clip.
    """
    async with _borrow(client) as session:
        token = await youtube._access_token(session, locale)
        reply = await session.get(
            analytics.REPORTS_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "ids": "channel==MINE",
                "startDate": (date.today() - timedelta(days=analytics.LOOKBACK_DAYS)).isoformat(),
                "endDate": date.today().isoformat(),
                "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                "dimensions": "elapsedVideoTimeRatio",
                "filters": f"video=={video_id}",
            },
        )
    if reply.status_code >= 500:
        # The Reports API answers 500 for individual videos now and then; one
        # sick video must not abort a walk over all of them.
        raise NoCurve(f"YouTube ตอบ {reply.status_code} สำหรับคลิปนี้ ลองใหม่ทีหลัง")
    if reply.status_code != 200:
        raise analytics.AnalyticsError(
            f"ดึงเส้น retention ไม่ได้ ({reply.status_code}): {reply.text[:200]}"
        )
    rows = reply.json().get("rows", [])
    if not rows:
        raise NoCurve(
            "YouTube ยังไม่มีเส้น retention ของคลิปนี้ — ต้องมีคนดูมากพอก่อน "
            "(วัดแล้ว: 361 views มี, 27 views ไม่มี)"
        )
    return sorted(rows, key=lambda row: row[0])


def card_at(cards: list[dict], seconds: float) -> int | None:
    """Which Card was on screen at `seconds`."""
    for i, card in enumerate(cards):
        if card["start"] <= seconds < card["start"] + card.get("seconds", 0):
            return i
    return len(cards) - 1 if cards and seconds >= cards[-1]["start"] else None


def drops(rows: list[list], duration: float, cards: list[dict]) -> list[dict]:
    """The steepest falls, as times and Cards rather than as fractions."""
    steps = [
        {"ratio": rows[i][0], "size": rows[i - 1][1] - rows[i][1]}
        for i in range(1, len(rows))
    ]
    if not steps:
        return []
    # A rise is not a fall, but it still counts as a step when working out what
    # "typical" means — otherwise a curve with one cliff and no other movement
    # measures that cliff against itself.
    magnitudes = sorted(max(step["size"], 0.0) for step in steps)
    typical = magnitudes[len(magnitudes) // 2]
    height = max((row[1] for row in rows), default=0.0)
    threshold = max(typical * STEEPNESS, height * MIN_DROP)
    steep = [step for step in steps if step["size"] >= threshold > 0]

    # Neighbouring buckets are one event: merge them, and date the event from
    # where the fall began rather than where it finished.
    clusters: list[dict] = []
    for step in sorted(steep, key=lambda step: step["ratio"]):
        if clusters and step["ratio"] - clusters[-1]["end"] <= CLUSTER_GAP:
            clusters[-1]["size"] += step["size"]
            clusters[-1]["end"] = step["ratio"]
        else:
            clusters.append({"ratio": step["ratio"], "end": step["ratio"], "size": step["size"]})

    clusters.sort(key=lambda cluster: cluster["size"], reverse=True)
    out = []
    for cluster in clusters[:TOP_DROPS]:
        at = cluster["ratio"] * duration
        out.append({
            "at": round(at, 2),
            "size": round(cluster["size"], 4),
            "card": card_at(cards, at),
        })
    return sorted(out, key=lambda drop: drop["at"])


def chart(rows: list[list], duration: float, cards: list[dict], dest, title: str = ""):
    """The curve, with the Card boundaries drawn on it and the cliffs marked."""
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    top = max((row[1] for row in rows), default=1.0) or 1.0

    def x_of(ratio: float) -> float:
        return PAD_L + ratio * plot_w

    def y_of(value: float) -> float:
        return PAD_T + plot_h - (value / top) * plot_h

    label = render._font(render.THAI_BOLD, 22)
    small = render._font(render.THAI_BOLD, 18)

    for step in range(5):
        y = PAD_T + plot_h * step / 4
        draw.line([(PAD_L, y), (W - PAD_R, y)], fill=GRID)
        draw.text((12, y - 10), f"{top * (1 - step / 4):.1f}x", font=small, fill=DIM)

    # Card boundaries: the whole point is reading the curve against the script
    for i, card in enumerate(cards):
        x = x_of(min(card["start"] / duration, 1.0)) if duration else PAD_L
        draw.line([(x, PAD_T), (x, PAD_T + plot_h)], fill=GRID)
        draw.text((x + 5, PAD_T + plot_h + 8), f"card {i + 1}", font=small, fill=DIM)

    draw.line(
        [(x_of(row[0]), y_of(row[1])) for row in rows], fill=LINE, width=3, joint="curve"
    )

    for drop in drops(rows, duration, cards):
        x = x_of(drop["at"] / duration) if duration else PAD_L
        draw.line([(x, PAD_T), (x, PAD_T + plot_h)], fill=DROP, width=2)
        draw.text((x + 6, PAD_T + 6), f"-{drop['size']:.2f}", font=small, fill=DROP)

    draw.text((PAD_L, 18), title[:60] or "retention", font=label, fill=FG)
    draw.text((W - PAD_R - 90, H - 34), f"{duration:.0f}s", font=small, fill=DIM)
    img.save(dest)
    return dest


def summary(rows: list[list], duration: float, cards: list[dict]) -> str:
    found = drops(rows, duration, cards)
    if not found:
        return "ไม่มีจุดที่คนหนีชันผิดปกติ — เส้นลงเรียบๆ ทั้งคลิป"
    lines = ["จุดที่คนหนีชันที่สุด:"]
    for drop in found:
        card = cards[drop["card"]] if drop["card"] is not None else None
        text = (card or {}).get("narration", "")
        where = f"card {drop['card'] + 1}" if drop["card"] is not None else "?"
        lines.append(f"• {drop['at']:.1f}s ({where}) — {text[:60]}")
    return "\n".join(lines)
