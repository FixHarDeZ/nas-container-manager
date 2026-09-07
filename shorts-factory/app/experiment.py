"""The running Experiment: what is being varied, and what the numbers say.

One factor at a time, two levels, assigned per Clip at random and recorded in
the Manifest before the Script is written. YouTube offers no within-clip A/B
for Shorts, so a Variant belongs to a whole Clip and is compared against other
Clips (docs/adr/0004).

Nothing here decides anything before the Gate. `report()` is happy to say that
it does not know, which is the honest answer for a channel with 9 clips and 206
views.
"""
from __future__ import annotations

import random
from statistics import median

from app import analytics, locales, manifest

FACTOR = "hook"

# The clause appended to the prompt, stored verbatim in every Manifest: the
# prompt will drift, and a Variant name alone would not say what it meant on
# the day the Clip was written.
VARIANTS = {
    "shock_number": (
        "รอบนี้ card แรกต้องเปิดด้วย**ตัวเลขที่ทำให้คนอึ้ง** (เช่น เวลาที่เสียไป "
        "ขนาดที่โตขึ้น เงินที่หายไป) ห้ามเปิดด้วยคำถาม"
    ),
    "question": (
        "รอบนี้ card แรกต้องเปิดด้วย**คำถามตรงๆ ที่คนทำงานสายนี้ถามตัวเองอยู่แล้ว** "
        "ห้ามเปิดด้วยตัวเลขสถิติ"
    ),
}

# Same factor and the same two levels for every Locale — the arms have to be
# comparable within a Locale, not across them — but the clause itself has to
# reach the model in the language it is writing.
VARIANTS_EN = {
    "shock_number": (
        "For this clip the first card must open with **a number that stops "
        "people** (time wasted, size grown, money lost). Never open with a "
        "question."
    ),
    "question": (
        "For this clip the first card must open with **a blunt question the "
        "viewer already asks themselves**. Never open with a statistic."
    ),
}
VARIANTS_BY_LOCALE = {"th": VARIANTS, "en": VARIANTS_EN}

EXPLORE_RATE = 1 / 3
EXPLORE_CLAUSE_EN = (
    "This one is an experiment: go right outside the usual pattern "
    "(a different structure, an angle never taken before, a rhythm unlike the "
    "earlier clips). Stay on the topic given."
)
EXPLORE_CLAUSE = (
    "รอบนี้เป็นคลิปทดลอง: เขียนออกนอกแพตเทิร์นเดิมได้เต็มที่ "
    "(โครงสร้างแปลกไป มุมที่ยังไม่เคยเล่า จังหวะที่ไม่เหมือนคลิปก่อนๆ) "
    "ขอแค่ยังอยู่ในหัวข้อสาย DevOps/AI"
)

# The Gate, per Variant (docs/adr/0004). The channel-wide half lives in
# `analytics.gate_note()`.
MIN_CLIPS = 10
MIN_VIEWS = analytics.GATE_VIEWS_PER_VARIANT
MIN_GAP = 5.0   # percentage points of averageViewPercentage


def assign(roll: float | None = None, pick: str | None = None,
           locale: str = "th") -> dict:
    """Choose what this Clip is: an Explore clip, or one Variant of the factor.

    Returned as the fields a Manifest carries, so the caller writes them
    straight in and never has to know the shape.
    """
    variants = VARIANTS_BY_LOCALE.get(locale, VARIANTS)
    roll = random.random() if roll is None else roll
    if roll < EXPLORE_RATE:
        explore = EXPLORE_CLAUSE if locale == "th" else EXPLORE_CLAUSE_EN
        return {"variant": None, "explore": True, "style": explore}
    name = pick or random.choice(sorted(variants))
    return {"variant": name, "explore": False, "style": variants[name]}


def _percent(record: dict) -> float | None:
    """The day-7 reading, or nothing. Never the latest one — see ADR 0004."""
    snapshot = manifest.day7(record)
    return snapshot.get("percent") if snapshot else None


def for_locale(records: list[dict], locale: str = locales.DEFAULT) -> list[dict]:
    """Only the Clips written for one audience.

    Two audiences in one set of counters is two smaller, noisier experiments
    wearing one number (docs/adr/0008). Manifests older than Locales carry no
    field and belong to Thai, the only Locale that existed then.
    """
    return [r for r in records if r.get("locale", locales.DEFAULT) == locale]


def tally(records: list[dict]) -> dict[str, dict]:
    """Per Variant: how many Clips, how they did, and how many were thrown out.

    Explore clips are counted separately and never mixed in: they exist to
    break the pattern, so including them would measure the pattern breaking.
    """
    def empty() -> dict:
        return {"clips": 0, "discarded": 0, "failed": 0, "views": 0, "percents": []}

    out = {name: empty() for name in sorted(VARIANTS)}
    out["explore"] = empty()

    # A Manifest only becomes a Clip once a Script was actually written and
    # judged. `drafting` (the model never answered) and `render_failed` are
    # technical noise: counting them would let an arm pass the Gate on records
    # that produced nothing, and would flatter the discard rate of whichever
    # Variant fails most often, since the failures inflate its denominator.
    REAL = {"rendered", "discarded"}

    for record in records:
        key = "explore" if record.get("explore") else record.get("variant")
        if key not in out:
            continue   # written before the Experiment started
        bucket = out[key]
        outcome = record.get("outcome")
        if outcome not in REAL:
            bucket["failed"] += 1
            continue
        bucket["clips"] += 1
        if outcome == "discarded":
            bucket["discarded"] += 1
        snapshot = manifest.day7(record)
        if snapshot:
            bucket["views"] += snapshot.get("views", 0)
        percent = _percent(record)
        if percent is not None:
            bucket["percents"].append(percent)
    return out


def verdict(counts: dict[str, dict]) -> str:
    """Who won, or — far more likely for now — that nobody can tell yet."""
    arms = {name: counts[name] for name in VARIANTS}
    short = [
        f"{name} ({data['clips']}/{MIN_CLIPS} คลิป, {data['views']}/{MIN_VIEWS} views)"
        for name, data in arms.items()
        if data["clips"] < MIN_CLIPS or data["views"] < MIN_VIEWS
    ]
    if short:
        return "⚠️ ยังสรุปไม่ได้ — ยังไม่ถึงเกณฑ์: " + ", ".join(short)

    medians = {name: median(data["percents"]) for name, data in arms.items() if data["percents"]}
    if len(medians) < len(arms):
        return "⚠️ ยังสรุปไม่ได้ — บาง variant ยังไม่มีตัวเลข day-7"

    best, worst = max(medians, key=medians.get), min(medians, key=medians.get)
    gap = medians[best] - medians[worst]
    if gap < MIN_GAP:
        return (
            f"= เสมอ — ต่างกัน {gap:.1f} จุด ยังไม่ถึง {MIN_GAP:.0f} จุดที่ตั้งไว้ "
            "ถือว่าสรุปไม่ได้ ใช้ค่าเดิมต่อ"
        )
    return f"🏆 {best} ชนะ — median {medians[best]:.1f}% เทียบ {medians[worst]:.1f}% (ต่าง {gap:.1f} จุด)"


def _category_of(record: dict) -> str:
    """The Clip's own category: what the Script said, or what /trends tagged."""
    scripts = record.get("scripts") or []
    if scripts:
        category = str(scripts[-1].get("script", {}).get("category", "")).strip()
        if category:
            return category
    return str((record.get("trend") or {}).get("category", "")).strip() or "ไม่ระบุ"


def by_category(records: list[dict]) -> dict[str, dict]:
    """How each subject area did.

    **Not an experiment.** The human picks the Topic, so categories are not
    randomised and anything read here is a correlation with whatever made that
    Topic get chosen. Kept because with topics free to roam, the subject is the
    biggest thing moving the numbers and refusing to look at it would be worse
    than looking at it with the label attached.
    """
    out: dict[str, dict] = {}
    for record in records:
        if record.get("outcome") not in {"rendered", "discarded"}:
            continue
        bucket = out.setdefault(
            _category_of(record), {"clips": 0, "views": 0, "percents": [], "trend": 0}
        )
        bucket["clips"] += 1
        if record.get("trend"):
            bucket["trend"] += 1
        snapshot = manifest.day7(record)
        if snapshot:
            bucket["views"] += snapshot.get("views", 0)
            if snapshot.get("percent") is not None:
                bucket["percents"].append(snapshot["percent"])
    return out


def report(records: list[dict], locale: str = locales.DEFAULT) -> str:
    records = for_locale(records, locale)
    counts = tally(records)
    # The channel-wide warning goes first: it says the figures below cannot be
    # used to decide anything, which is no use underneath them.
    gate = analytics.gate_note(locale)
    locale_label = locales.get(locale)["label"]
    lines = ([gate, ""] if gate else []) + [f"🧪 การทดลอง ({locale_label}): {FACTOR}", ""]
    for name, data in counts.items():
        label = "explore (ไม่นับผล)" if name == "explore" else name
        percents = data["percents"]
        shown = f"{median(percents):.1f}%" if percents else "—"
        rate = f"{data['discarded']}/{data['clips']}" if data["clips"] else "0/0"
        failed = f" · พัง {data['failed']}" if data["failed"] else ""
        lines.append(
            f"• {label}: {data['clips']} คลิป · ทิ้ง {rate}{failed} · "
            f"{data['views']} views (day-7) · median day-7 {shown}"
        )
    lines += ["", verdict(counts)]

    categories = by_category(records)
    if categories:
        lines += ["", "📂 แยกตามหมวด — **สังเกตการณ์ ไม่ใช่การทดลอง**",
                  "   (คนเลือกหัวข้อเอง ไม่ได้สุ่ม อ่านเป็นความสัมพันธ์ ไม่ใช่สาเหตุ)", ""]
        ranked = sorted(
            categories.items(),
            key=lambda kv: (median(kv[1]["percents"]) if kv[1]["percents"] else -1),
            reverse=True,
        )
        for name, data in ranked:
            shown = f"{median(data['percents']):.1f}%" if data["percents"] else "—"
            trend = f" · จาก trend {data['trend']}" if data["trend"] else ""
            lines.append(
                f"• {name}: {data['clips']} คลิป · {data['views']} views (day-7) · "
                f"median {shown}{trend}"
            )
    return "\n".join(lines)
