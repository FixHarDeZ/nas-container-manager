"""The permanent record of every Script this bot writes.

One file per Topic session under `/data/clips`, written whether or not the
human ever publishes the Clip. Keeping only the Scripts that survived review
would make a Variant that writes badly look as good as one that writes well —
the discard rate is itself a measurement (docs/adr/0004).

Nothing here may break a render: a Manifest that cannot be written is logged
and skipped, the same rule the Notifier follows.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DIR = Path(os.environ.get("DATA_DIR", "/data")) / "clips"


def _path(clip_id: str) -> Path:
    return DIR / f"{clip_id}.json"


def _save(record: dict) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        _path(record["id"]).write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        logger.exception("เขียน manifest ไม่ได้ (%s)", record.get("id"))


def start(topic: str, locale: str = "th") -> str:
    """Open a Manifest for a new Topic and return its id.

    `locale` is written on every record from here on. A Manifest older than
    Locales has no such field, and every reader treats that absence as Thai.
    """
    # Sharing a filename means the second Manifest silently erases the first,
    # which is the one failure this module exists to prevent. Milliseconds are
    # not enough on their own — the backfill opens Manifests in a tight loop
    # and lands several inside the same millisecond — so a taken id is bumped
    # until it is free.
    stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    clip_id, bump = stem, 0
    while _path(clip_id).exists():
        bump += 1
        clip_id = f"{stem}-{bump}"
    _save({
        "id": clip_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "topic": topic,
        "locale": locale,
        # Filled in once experiments start; recorded as null until then so a
        # pre-experiment Manifest is never mistaken for an unassigned one.
        "variant": None,
        "explore": False,
        # Every generated Script in order — index 0 is the first draft, the
        # last is whatever was rendered or discarded.
        "scripts": [],
        "outcome": "drafting",
        "published": False,
        "snapshots": [],
    })
    return clip_id


def load(clip_id: str) -> dict | None:
    try:
        return json.loads(_path(clip_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("อ่าน manifest %s ไม่ได้", clip_id)
        return None


def update(clip_id: str | None, **fields) -> None:
    """Merge fields into an existing Manifest. Unknown id is a no-op."""
    if not clip_id:
        return
    record = load(clip_id)
    if record is None:
        return
    record.update(fields)
    _save(record)


def add_script(clip_id: str | None, script: dict) -> None:
    """Append a draft. Revisions are kept, not overwritten."""
    if not clip_id:
        return
    record = load(clip_id)
    if record is None:
        return
    record["scripts"].append(
        {"at": datetime.now().isoformat(timespec="seconds"), "script": script}
    )
    _save(record)


def by_video(video_id: str) -> dict | None:
    """The Manifest a published video came from, or None if it predates them."""
    for record in load_all():
        if record.get("video_id") == video_id:
            return record
    return None


def add_snapshot(clip_id: str | None, snapshot: dict) -> None:
    """Append one dated measurement, replacing any taken the same day.

    Re-running the daily pull must not double-count: the date is the key.
    """
    if not clip_id:
        return
    record = load(clip_id)
    if record is None:
        return
    kept = [s for s in record.get("snapshots", []) if s.get("date") != snapshot.get("date")]
    record["snapshots"] = sorted(kept + [snapshot], key=lambda s: s["date"])
    _save(record)


def day7(record: dict) -> dict | None:
    """The official measurement: the first snapshot taken on day 7 or later.

    Retention keeps moving as views accrue, so experiments compare every Clip
    at the same age rather than at whatever "latest" happens to mean today
    (docs/adr/0004).
    """
    for snapshot in record.get("snapshots", []):
        if snapshot.get("age_days", 0) >= 7:
            return snapshot
    return None


def load_all() -> list[dict]:
    if not DIR.is_dir():
        return []
    out = []
    for path in sorted(DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            logger.warning("manifest %s พัง ข้ามไป", path.name)
    return out
