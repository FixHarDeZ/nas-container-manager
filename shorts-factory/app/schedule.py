"""When the bot goes looking for a Topic on its own, per Locale.

Lives in its own file on its own volume — `/config/schedule.json` — because it
is the one thing in this stack the dashboard is allowed to write. Everything
under `/data` stays read-only to that process (docs/adr/0009 amends 0007), so
the blast radius of the dashboard's single writing route is exactly this file:
what hours a trends round fires at, and how long it waits for a human.

The environment still supplies the defaults, so a container that has never had
the file behaves exactly as it did before this existed.

Read through `settings()` rather than cached at import: the dashboard is a
different process and the bot must see an edit without a restart.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app import locales

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
PATH = CONFIG_DIR / "schedule.json"

# The bounds a stored value has to sit inside. Not taste: an hour outside 0-23
# never fires, and a wait of zero renders an unattended Clip before the human
# has read the notification the list arrived in.
MIN_PICK_MINUTES = 1
MAX_PICK_MINUTES = 240
MAX_HOURS = 12


def _env_hours() -> list[int]:
    raw = os.environ.get("TRENDS_HOURS", "8,12,17")
    return sorted({int(h) for h in raw.split(",") if h.strip()})


def defaults() -> dict:
    """What every Locale does before anyone has touched the dashboard.

    Thai keeps the schedule it has been running on. English starts switched
    off: turning it on publishes to a channel unattended, which is a decision
    for a human and not a side effect of deploying this file.
    """
    out = {}
    for code in locales.codes():
        thai = code == locales.DEFAULT
        out[code] = {
            "enabled": thai,
            "hours": _env_hours() if thai else [20],
            "auto_pick_minutes": int(os.environ.get("AUTO_PICK_MINUTES", "15")),
        }
    return out


def validate(payload: dict) -> dict:
    """The stored shape, or ValueError naming the first thing wrong with it.

    Deliberately strict and deliberately whole-request: a half-applied schedule
    is worse than a rejected one, because the half that applied is the half
    nobody checked.
    """
    if not isinstance(payload, dict):
        raise ValueError("ต้องเป็น object")
    clean = {}
    for code, spec in payload.items():
        if code not in locales.codes():
            raise ValueError(f"ไม่รู้จักภาษา {code}")
        if not isinstance(spec, dict):
            raise ValueError(f"{code}: ต้องเป็น object")
        hours = spec.get("hours") or []
        if not isinstance(hours, list):
            raise ValueError(f"{code}: hours ต้องเป็น list")
        if len(hours) > MAX_HOURS:
            raise ValueError(f"{code}: ตั้งได้ไม่เกิน {MAX_HOURS} รอบต่อวัน")
        parsed = set()
        for hour in hours:
            try:
                hour = int(hour)
            except (TypeError, ValueError):
                raise ValueError(f"{code}: ชั่วโมงต้องเป็นตัวเลข ไม่ใช่ {hour!r}")
            if not 0 <= hour <= 23:
                raise ValueError(f"{code}: ชั่วโมงต้องอยู่ระหว่าง 0-23 ไม่ใช่ {hour}")
            parsed.add(hour)
        try:
            minutes = int(spec.get("auto_pick_minutes", 15))
        except (TypeError, ValueError):
            raise ValueError(f"{code}: auto_pick_minutes ต้องเป็นตัวเลข")
        if not MIN_PICK_MINUTES <= minutes <= MAX_PICK_MINUTES:
            raise ValueError(
                f"{code}: auto_pick_minutes ต้องอยู่ระหว่าง "
                f"{MIN_PICK_MINUTES}-{MAX_PICK_MINUTES} ไม่ใช่ {minutes}"
            )
        # An enabled Locale with no hours would silently never fire, which
        # reads in the dashboard as "on" and behaves as "off".
        if spec.get("enabled") and not parsed:
            raise ValueError(f"{code}: เปิดไว้แต่ไม่ได้ตั้งเวลาเลย")
        clean[code] = {
            "enabled": bool(spec.get("enabled")),
            "hours": sorted(parsed),
            "auto_pick_minutes": minutes,
        }
    return clean


def settings() -> dict:
    """The stored schedule merged over the defaults, per Locale.

    A Locale added to the code after the file was written gets its default
    rather than disappearing, and a file that will not parse is ignored with a
    warning: the bot losing its trends rounds is a smaller failure than the bot
    refusing to start.
    """
    merged = defaults()
    try:
        stored = json.loads(PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return merged
    except (json.JSONDecodeError, OSError):
        logger.warning("อ่าน %s ไม่ได้ ใช้ค่าเริ่มต้น", PATH)
        return merged
    try:
        for code, spec in validate(stored).items():
            merged[code] = spec
    except ValueError:
        logger.warning("%s เก็บค่าที่ใช้ไม่ได้ ใช้ค่าเริ่มต้น", PATH, exc_info=True)
    return merged


def save(payload: dict) -> dict:
    """Validate and store. The dashboard is the only caller."""
    clean = validate(payload)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Written whole and renamed into place: the bot reads this file on every
    # poll tick and must never see half of it.
    temporary = PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(PATH)
    return clean


def stamps(state: dict) -> dict:
    """`last_auto_trends` as a Locale→slot map, whatever shape it is on disk.

    It used to be one bare string, from when only Thai ran unattended. That
    value is Thai's and nothing else's; reading it as a map would fire every
    Locale's first round twice.
    """
    stored = state.get("last_auto_trends")
    if isinstance(stored, dict):
        return dict(stored)
    return {locales.DEFAULT: stored} if stored else {}


def due(state: dict, now) -> list[tuple[str, str]]:
    """The (locale, slot) rounds that are owed, newest passed hour only.

    Same rule per Locale as the single-Locale version had: a bot that was down
    all day comes back and runs each Locale once, not once per missed hour.
    """
    done = stamps(state)
    owed = []
    for code, spec in sorted(settings().items()):
        if not spec["enabled"]:
            continue
        passed = [hour for hour in spec["hours"] if now.hour >= hour]
        if not passed:
            continue
        slot = f"{now.date().isoformat()}T{max(passed):02d}"
        if done.get(code) != slot:
            owed.append((code, slot))
    return owed
