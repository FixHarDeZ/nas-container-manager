"""What a Locale's country is paying attention to right now.

Two sources, both free and both about demand rather than supply:

- Google Trends' RSS feed (`/trending/rss?geo=TH`) — what people are searching
  for, with an approximate volume and the news headline behind each spike. The
  older `dailytrends` JSON endpoint is retired and answers 404; this is what
  replaced it.
- YouTube's own `mostPopular` chart for TH — what people are actually watching
  through to the point of charting, with view counts and a category.

Trends are an **outside** signal, so nothing here is held back by the Gate in
docs/adr/0004: that gate exists to stop the bot learning from its own thin
numbers, not to stop it reading the world.
"""
from __future__ import annotations

import logging
from xml.etree import ElementTree

import httpx

from app import locales, youtube

logger = logging.getLogger(__name__)

RSS_URL = "https://trends.google.com/trending/rss"
POPULAR_URL = "https://www.googleapis.com/youtube/v3/videos"
REGION = "TH"
NS = {"ht": "https://trends.google.com/trending/rss"}

# Categories dropped before anything is suggested. A bot writing a script about
# a live news story, an election or a match result is a bot inventing facts
# about real people, and it publishes to a real channel under a real name.
# 25 = News & Politics, 17 = Sports (live results age in hours anyway).
BLOCKED_CATEGORIES = {"25", "17"}

MAX_SEARCHES = 10
MAX_VIDEOS = 12


def keep(category_id: str | None) -> bool:
    """Whether a charting video may be turned into a Topic at all."""
    return category_id not in BLOCKED_CATEGORIES


def _traffic(text: str) -> int:
    """`20000+` → 20000. Only used for ordering."""
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def parse_rss(xml: str) -> list[dict]:
    """Search spikes, biggest first, each with the headline that caused it."""
    root = ElementTree.fromstring(xml)
    out = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        headlines = [
            (news.findtext("ht:news_item_title", "", NS) or "").strip()
            for news in item.findall("ht:news_item", NS)
        ]
        out.append({
            "source": "google-trends",
            "term": title,
            "traffic": _traffic(item.findtext("ht:approx_traffic", "", NS) or ""),
            "headline": next((h for h in headlines if h), ""),
        })
    out.sort(key=lambda row: row["traffic"], reverse=True)
    return out[:MAX_SEARCHES]


async def searches(client: httpx.AsyncClient, geo: str = REGION) -> list[dict]:
    reply = await client.get(RSS_URL, params={"geo": geo})
    reply.raise_for_status()
    return parse_rss(reply.text)


async def watching(client: httpx.AsyncClient, region: str = REGION) -> list[dict]:
    """The TH chart, minus the categories this bot has no business writing."""
    token = await youtube._access_token(client)
    reply = await client.get(
        POPULAR_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"part": "snippet,statistics", "chart": "mostPopular",
                "regionCode": region, "maxResults": 30},
    )
    reply.raise_for_status()
    out = []
    for item in reply.json().get("items", []):
        snippet = item["snippet"]
        if not keep(snippet.get("categoryId")):
            continue
        out.append({
            "source": f"youtube-{region.lower()}",
            "term": snippet["title"],
            "traffic": int(item["statistics"].get("viewCount", 0)),
            "headline": "",
            "category_id": snippet.get("categoryId"),
        })
    return out[:MAX_VIDEOS]


async def collect(locale: str = locales.DEFAULT) -> list[dict]:
    """Both sources for one Locale's country. A failing source is skipped."""
    spec = locales.get(locale)
    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for name, source, where in (
            ("google-trends", searches, spec["trends_geo"]),
            (f"youtube-{spec['trends_region'].lower()}", watching, spec["trends_region"]),
        ):
            try:
                rows += await source(client, where)
            except Exception:
                logger.exception("ดึง trend จาก %s ไม่สำเร็จ", name)
    return rows


def format_raw(rows: list[dict]) -> str:
    """The unprocessed list, so a bad suggestion can be caught against it."""
    if not rows:
        return "ดึง trend ไม่ได้เลยสักแหล่ง"
    lines = ["📈 ของดิบที่ดึงมา", ""]
    for row in rows:
        where = "ค้นหา" if row["source"] == "google-trends" else "ดูบน YT"
        lines.append(f"• [{where}] {row['term'][:48]} · {row['traffic']:,}")
        if row.get("headline"):
            lines.append(f"    ↳ {row['headline'][:60]}")
    return "\n".join(lines)
