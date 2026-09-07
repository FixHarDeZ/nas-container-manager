"""What the bot has already published.

The only record of which videos are ours: YouTube is queried by id, and the
prompt is told what not to repeat. Kept as a plain JSON list because it is
appended once per upload and read whole.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PATH = Path(os.environ.get("DATA_DIR", "/data")) / "history.json"
RECENT_TITLES = 30


def load() -> list[dict]:
    if not PATH.exists():
        return []
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("history.json อ่านไม่ได้ เริ่มนับใหม่")
        return []


def record(video_id: str, script: dict, topic: str, locale: str = "th") -> None:
    entries = load()
    entries.append({
        "video_id": video_id,
        "title": script.get("title", ""),
        "topic": topic,
        # Which channel this went to (docs/adr/0008). Entries written before
        # Locales existed have no field; every reader treats that as Thai.
        "locale": locale,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
    })
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")


def recent_titles(limit: int = RECENT_TITLES) -> list[str]:
    return [e["title"] for e in load()[-limit:] if e.get("title")]


def video_ids() -> list[str]:
    return [e["video_id"] for e in load() if e.get("video_id")]


def title_of(video_id: str) -> str:
    for entry in load():
        if entry.get("video_id") == video_id:
            return entry.get("title", video_id)
    return video_id
