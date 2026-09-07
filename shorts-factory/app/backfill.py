"""Manifests for the Clips that were published before Manifests existed.

Everything the render knew was deleted with its workdir. What survives in
`/output` is the metadata `.txt` and the `.srt`, which together give the title
and the Card boundaries — enough to read a retention curve against, and not
enough to reproduce the Clip. Those Manifests are flagged `reconstructed` so
nothing later mistakes them for a full record: their `lines`, footage queries
and audio settings are gone for good.

Idempotent and run at startup: a Clip that already has a Manifest is skipped.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from app import history, manifest

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output"))


def _seconds(stamp: str) -> float:
    """`00:01:05,250` → 65.25"""
    clock, ms = stamp.strip().split(",")
    hours, minutes, secs = (int(part) for part in clock.split(":"))
    return hours * 3600 + minutes * 60 + secs + int(ms) / 1000


def cards_from_srt(text: str) -> list[dict]:
    """Card starts and narration, read back out of the subtitles."""
    cards = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, end = (_seconds(part) for part in lines[1].split("-->"))
        cards.append({
            "start": round(start, 3),
            "seconds": round(end - start, 3),
            "narration": " ".join(lines[2:]).strip(),
        })
    return cards


def _files_by_title() -> dict[str, Path]:
    """Metadata files keyed by the title on their first line."""
    found = {}
    if not OUTPUT_DIR.is_dir():
        return found
    # rglob, not glob: an English Clip lands in /output/en, and a Manifest
    # that is not rebuilt for it is a Clip with no numbers at all.
    for path in OUTPUT_DIR.rglob("*.txt"):
        try:
            first = path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        if first:
            found[first] = path
    return found


def run() -> int:
    """Write a reconstructed Manifest per published Clip that lacks one."""
    # ponytail: by_video() re-reads every Manifest per history entry — O(n²)
    # file reads at startup. Fine at 9; index by video id if this grows.
    by_title = _files_by_title()
    written = 0
    for entry in history.load():
        video_id = entry.get("video_id")
        if not video_id or manifest.by_video(video_id):
            continue

        meta = by_title.get(entry.get("title", ""))
        srt = meta.with_suffix(".srt") if meta else None
        cards = []
        if srt and srt.is_file():
            try:
                cards = cards_from_srt(srt.read_text(encoding="utf-8"))
            except OSError:
                logger.warning("อ่าน %s ไม่ได้", srt)

        # The only surviving trace of which Locale an old Clip belongs to is
        # the folder its metadata file sits in.
        locale = "en" if meta is not None and meta.parent.name == "en" else "th"
        clip_id = manifest.start(entry.get("topic") or "", locale)
        manifest.update(
            clip_id,
            created_at=entry.get("uploaded_at") or datetime.now().isoformat(timespec="seconds"),
            published_at=entry.get("uploaded_at"),
            published=True,
            video_id=video_id,
            outcome="rendered",
            reconstructed=True,
            title=entry.get("title", ""),
            render={
                "reconstructed": True,
                "seconds": round(cards[-1]["start"] + cards[-1]["seconds"], 3) if cards else None,
                "cards": cards,
            },
        )
        written += 1
        logger.info("backfill %s (%d card)", video_id, len(cards))
    return written
