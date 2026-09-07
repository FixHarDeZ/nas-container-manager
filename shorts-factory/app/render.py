"""Draws Cards, speaks them, and assembles the Clip."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import subprocess
from pathlib import Path

import edge_tts
from PIL import Image, ImageDraw, ImageFont, features

from app import footage, locales

logger = logging.getLogger(__name__)

W, H = 1080, 1920
# Cards are drawn oversized and the Ken Burns move crops into them, so zooming
# never scales text past its native pixels.
OVERSCAN = 1.12
CW, CH = round(W * OVERSCAN), round(H * OVERSCAN)
ZOOM_FPS = 30
MARGIN = 108
BG_TOP, BG_BOTTOM = (16, 20, 24), (27, 36, 48)
FG = (242, 244, 248)
ACCENT = (255, 210, 74)
CODE_BG, CODE_BORDER, CODE_FG = (11, 14, 18), (46, 56, 68), (168, 226, 181)
# Text sits on live footage in the B-roll path, so it carries its own shadow.
SHADOW = (0, 0, 0, 190)
SHADOW_OFFSET = 5
SCRIM = "black@0.5"  # holds the footage back far enough for text to read

# Waree covers Thai and Latin in one face; Noto Sans Thai has no Latin glyphs.
THAI_BOLD = "/usr/share/fonts/truetype/tlwg/Waree-Bold.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

# The floor is the size script._too_wide() validates against: a line that does
# not fit 864px (the frame width less margins, which is what the footage path
# draws into) at 40px Waree is rejected before it ever reaches the renderer.
TEXT_SIZE, MIN_TEXT_SIZE, CODE_SIZE = 92, 40, 38
LINE_SPACING = 1.35


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a font with Raqm layout, which Thai needs to place its marks."""
    if not features.check("raqm"):
        # Basic layout drops Thai tone marks with only a UserWarning, so refuse
        # to render rather than emit a clip with silently wrong text.
        raise RuntimeError("Raqm ไม่พร้อมใช้ — ต้องติดตั้ง libraqm0 (ดู docs/adr/0003)")
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)


def _background(w: int, h: int) -> Image.Image:
    """Vertical gradient, drawn one row at a time."""
    img = Image.new("RGBA", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        fill = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        draw.line([(0, y), (w, y)], fill=fill + (255,))
    return img


def _fit(lines: list[str], font_path: str, start_size: int, max_width: int) -> ImageFont.FreeTypeFont:
    """Largest size at which every line fits the usable width."""
    size = start_size
    while size > MIN_TEXT_SIZE:
        font = _font(font_path, size)
        if all(font.getlength(line) <= max_width for line in lines):
            return font
        size -= 4
    return _font(font_path, MIN_TEXT_SIZE)


def _draw_code(draw: ImageDraw.ImageDraw, code: str, top: int, w: int) -> None:
    font = _font(MONO, CODE_SIZE)
    rows = code.strip().splitlines()
    pad, gap = 32, round(CODE_SIZE * 1.4)
    box_h = pad * 2 + gap * len(rows)
    box_w = min(w - MARGIN * 2, max(int(font.getlength(r)) for r in rows) + pad * 2)
    x0 = (w - box_w) // 2
    draw.rounded_rectangle(
        [x0, top, x0 + box_w, top + box_h], radius=20, fill=CODE_BG, outline=CODE_BORDER, width=2
    )
    for i, row in enumerate(rows):
        draw.text((x0 + pad, top + pad + i * gap), row, font=font, fill=CODE_FG)


def draw_card(card: dict, path: Path, is_hook: bool = False, over_footage: bool = False) -> Path:
    """Render one Card to a PNG.

    Over footage the card is a transparent overlay at frame size, because the
    footage supplies both the background and the movement. On its own it is
    drawn oversized on a gradient, and the Ken Burns crop provides the movement.
    """
    w, h = (W, H) if over_footage else (CW, CH)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if over_footage else _background(w, h)
    draw = ImageDraw.Draw(img)

    lines = card["lines"]
    font = _fit(lines, THAI_BOLD, TEXT_SIZE, w - MARGIN * 2)
    step = round(font.size * LINE_SPACING)
    text_h = step * len(lines)

    code = (card.get("code") or "").strip()
    code_h = 0
    if code:
        rows = code.splitlines()
        code_h = 64 + 32 * 2 + round(CODE_SIZE * 1.4) * len(rows)

    y = (h - text_h - code_h) // 2
    for line in lines:
        x = (w - font.getlength(line)) / 2
        if over_footage:
            draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), line, font=font, fill=SHADOW)
        draw.text((x, y), line, font=font, fill=ACCENT if is_hook else FG)
        y += step

    if code:
        _draw_code(draw, code, y + 64, w)

    img.save(path)
    return path


BGM_DIR = Path(os.environ.get("BGM_DIR", "/output/bgm"))
BGM_VOLUME = 0.35   # before ducking; the sidechain takes it down further under speech
BGM_FADE = 2.0      # seconds of fade-out at the end

CARD_SEPARATOR = ".\n\n"   # what makes the endpoint treat each Card as one sentence
TAIL_PAD = 0.2               # video runs slightly past the audio; -shortest trims it
# The newline in CARD_SEPARATOR is a paragraph break: it is what makes the
# endpoint emit one SentenceBoundary per Card, and it also buys a paragraph
# sized pause. Measured on th-TH-NiwatNeural: 0.96-1.01s at every Card join
# against 0.12-0.53s at the voice's own clause breaks. Cutting the joins back
# to a value inside that range is what stops the delivery sounding like a
# series of announcements.
JOIN_SILENCE = 0.30
SILENCE_FLOOR = "-32dB"


CLAUSE_DASH = re.compile(r"\s+[-‐-―]\s+")
WORD_DASH = re.compile(r"[-‐-―]")


def _speakable(narration: str, locale: str = locales.DEFAULT) -> str:
    """A full stop inside a Card would split it into two sentences.

    A hyphen is read as a break, not as part of the word: "เอฟ-สามสิบห้า" comes
    out as "เอฟ", a second of silence, then "สามสิบห้า". A dash with spaces
    around it stands between clauses, so it becomes the comma the voice pauses
    on for 0.12-0.53s; one inside a word is dropped so the name is said whole.
    """
    text = narration.replace(".", ",")
    # A hyphen inside a Thai word is noise once transliterated, but inside an
    # English one it joins two real words ("state-of-the-art"), so there it
    # becomes a space rather than nothing.
    glue = "" if locales.get(locale)["spoken_script"] == "thai" else " "
    return WORD_DASH.sub(glue, CLAUSE_DASH.sub(", ", text)).strip()


SAY_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "say.json"


def say_as() -> dict[str, str]:
    """Pronunciation overrides, keyed on the Thai the model wrote.

    Two things the Script cannot fix by itself. The voice reads some correct
    Thai spellings wrong — "พาสปอร์ต" comes out "พาด" — and the model
    letter-spells a coined word it does not recognise as a pun, writing
    "TH-AI" as "ทีเอไอ" where it is meant to be read "ไทย". A rewrite produces
    the same spelling and the same wrong audio, so the human respells the word
    once here and every later Clip says it right.
    """
    try:
        return json.loads(SAY_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def say_set(wrong: str, right: str) -> None:
    """Add an override, or drop it when `right` is empty."""
    entries = say_as()
    if right:
        entries[wrong] = right
    else:
        entries.pop(wrong, None)
    SAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SAY_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), "utf-8")


def _tts_text(card: dict, locale: str = locales.DEFAULT) -> str:
    """What the voice reads: the transliterated form, never the screen form.

    Overrides are applied here rather than at the join in `narrate()` so both
    sides of its boundary check see the same string; substituting later would
    make every Card look misaligned and drop the Clip to per-Card speech.
    Longest key first, so an entry cannot be half-eaten by a shorter one.
    """
    text = _speakable(card.get("spoken") or card["narration"], locale)
    for wrong, right in sorted(say_as().items(), key=lambda kv: -len(kv[0])):
        text = text.replace(wrong, right)
    return text


async def narrate(cards: list[dict], path: Path,
                  locale: str = locales.DEFAULT) -> tuple[Path, list[float]] | None:
    """Speak the whole Script in one breath and report where each Card starts.

    One synthesis call keeps the prosody continuous — speaking Card by Card
    restarts the intonation every time and reads as a series of announcements.
    Thai emits no `WordBoundary` events (no spaces to boundary on), but it does
    emit one `SentenceBoundary` per separated Card, which is what the cuts use.
    Verified for en-US-AndrewNeural too (2026-09-07): three Cards in, three
    SentenceBoundary events out, so both Locales take this path.

    Returns None when the boundaries cannot be trusted; the caller then falls
    back to speaking each Card separately.
    """
    narrations = [_tts_text(c, locale) for c in cards]
    communicate = edge_tts.Communicate(
        CARD_SEPARATOR.join(narrations),
        locales.voice(locale),
        rate=os.environ.get("TTS_RATE", "+0%"),
        pitch=os.environ.get("TTS_PITCH", "+0Hz"),
    )
    bounds: list[tuple[float, str]] = []
    with path.open("wb") as handle:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
            elif chunk["type"] == "SentenceBoundary":
                bounds.append((chunk["offset"] / 1e7, str(chunk.get("text", ""))))

    if len(bounds) != len(cards):
        logger.warning("ได้ขอบเขตประโยค %d อัน แต่มี %d card", len(bounds), len(cards))
        return None
    # A matching count is not alignment — one Card split in two and the next two
    # merged would also count right, and every image change after it would land
    # mid-sentence. Check each boundary really opens the Card it claims.
    for i, (spoken, (_, heard)) in enumerate(zip(narrations, bounds)):
        if not heard.startswith(spoken[:10]):
            logger.warning("ขอบเขตประโยคที่ %d ไม่ตรงกับ narration", i + 1)
            return None

    starts = [offset for offset, _ in bounds]

    # The first Card owns everything before the second boundary, including the
    # lead-in silence.
    starts[0] = 0.0
    return path, starts


async def speak(text: str, path: Path, locale: str = locales.DEFAULT) -> Path:
    """Synthesise one Card's narration.

    Rate and pitch are configurable because edge-tts defaults read slow and
    flat; they are the only prosody knobs the endpoint exposes.
    """
    await edge_tts.Communicate(
        text,
        locales.voice(locale),
        rate=os.environ.get("TTS_RATE", "+0%"),
        pitch=os.environ.get("TTS_PITCH", "+0Hz"),
    ).save(str(path))
    return path


def _run(args: list[str]) -> None:
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg ล้มเหลว: {done.stderr.strip()[-600:]}")


def audio_seconds(path: Path) -> float:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(done.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"อ่านความยาวเสียงไม่ได้: {done.stderr.strip()[-300:]}") from exc


def ken_burns(frames: int, zoom_in: bool) -> str:
    """A slow push or pull across the oversized card.

    `zoompan` is driven off the frame counter rather than accumulating into
    `zoom`, because accumulation rounds every step and the drift shows up as
    visible stutter on a slow move. The rate is solved so the move lands
    exactly on the last frame of the narration.
    """
    rate = (OVERSCAN - 1) / max(frames - 1, 1)
    z = (
        f"min(1+{rate:.8f}*on,{OVERSCAN})"
        if zoom_in
        else f"max({OVERSCAN}-{rate:.8f}*on,1)"
    )
    return (
        f"zoompan=z='{z}':d={frames}"
        ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={W}x{H}:fps={ZOOM_FPS}"
    )


def _segment(png: Path, seconds: float, out: Path, zoom_in: bool = True) -> Path:
    """One gradient card, moving slowly, held for `seconds`. Video only."""
    frames = max(round(seconds * ZOOM_FPS), 2)
    _run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(png),
        "-filter_complex", ken_burns(frames, zoom_in),
        "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-r", str(ZOOM_FPS), "-an", str(out),
    ])
    return out


def _segment_over_footage(card_png: Path, seconds: float, clip: Path, out: Path) -> Path:
    """Stock footage, held back by a scrim, with the card laid over it.

    No zoompan here — the footage already moves. `-stream_loop -1` covers a
    Card that outlasts the clip. Video only.
    """
    _run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(clip),
        "-loop", "1", "-i", str(card_png),
        "-filter_complex",
        (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={ZOOM_FPS},"
            f"drawbox=x=0:y=0:w=iw:h=ih:color={SCRIM}:t=fill[bg];"
            "[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
        ),
        "-map", "[v]", "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-r", str(ZOOM_FPS), "-an", str(out),
    ])
    return out


def concat(parts: list[Path], out: Path) -> Path:
    listing = out.parent / f"{out.stem}-parts.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)])
    return out


def pick_music() -> Path | None:
    """One track from the music folder, if the human has put any there."""
    if not BGM_DIR.is_dir():
        return None
    tracks = sorted(p for p in BGM_DIR.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav"})
    return random.choice(tracks) if tracks else None


def mux(video: Path, audio: Path, out: Path, music: Path | None = None) -> Path:
    """Lay the narration over the cut video, optionally under music.

    The video track is padded past the end of the audio and `-shortest` trims
    it back, because sentence durations reported by the endpoint overshoot the
    real file — trusting them would clip the last words of every clip.

    Music is ducked by `sidechaincompress` keyed on the narration itself, so it
    drops whenever the voice speaks and returns in the gaps. That keeps speech
    on top by construction rather than by guessing a fixed level.
    """
    args = ["ffmpeg", "-y", "-i", str(video), "-i", str(audio)]
    if music:
        speech = audio_seconds(audio)
        args += [
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex",
            (
                f"[2:a]volume={BGM_VOLUME},"
                f"afade=t=out:st={max(speech - BGM_FADE, 0):.3f}:d={BGM_FADE}[music];"
                "[1:a]asplit=2[voice][key];"
                "[music][key]sidechaincompress="
                "threshold=0.03:ratio=12:attack=5:release=400[ducked];"
                # normalize=0 is essential: amix defaults to scaling every input
                # by 1/n, which quietly drops the narration ~6dB the moment any
                # music is present — measured, not assumed.
                "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                # level=false: alimiter auto-levels by default, which would make a clip
                # with music louder than one without. Peak protection only.
                "alimiter=limit=0.95:level=false[mix]"
            ),
            "-map", "0:v", "-map", "[mix]",
        ]
    else:
        args += ["-map", "0:v", "-map", "1:a"]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-shortest", str(out)]
    _run(args)
    return out


def _timestamp(seconds: float) -> str:
    whole = int(seconds)
    ms = round((seconds - whole) * 1000)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d},{ms:03d}"


def write_srt(cards: list[dict], starts: list[float], end: float, dest: Path) -> Path:
    """Subtitles from the Card boundaries we already know.

    Uses each Card's `narration`, never its `spoken` twin: the transliteration
    ("ด็อกเกอร์") is right for the voice and wrong on screen, where the English
    words should be spelled as English.
    """
    lines = []
    for i, card in enumerate(cards):
        stop = starts[i + 1] if i + 1 < len(starts) else end
        lines += [
            str(i + 1),
            f"{_timestamp(starts[i])} --> {_timestamp(stop)}",
            card["narration"].strip(),
            "",
        ]
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def first_frame(clip: Path, dest: Path) -> Path:
    """The very first frame of the Clip, as a JPEG fit for a thumbnail.

    That frame is the hook card, which is the one screen written to make
    someone stop scrolling — so it is already the right picture.
    """
    _run([
        "ffmpeg", "-y", "-i", str(clip), "-frames:v", "1",
        # -q:v 2 keeps it well under YouTube's 2MB thumbnail limit
        "-q:v", "2", str(dest),
    ])
    return dest


def concat_audio(parts: list[Path], out: Path) -> Path:
    """Join audio slices losslessly.

    Re-encoded to PCM rather than stream-copied: copying mp3 frames carries
    each part's encoder padding into the join, which is exactly the silence
    this path exists to remove.
    """
    listing = out.parent / f"{out.stem}-parts.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
          "-c:a", "pcm_s16le", str(out)])
    return out


def tighten(audio: Path, starts: list[float], workdir: Path) -> tuple[Path, list[float]]:
    """Cut the paragraph-length dead air out of every Card join.

    Each Card is cut out at the boundaries we already have and its trailing
    silence trimmed back to JOIN_SILENCE, then the pieces are joined again. The
    prosody is untouched — this is still one synthesis, only shorter — and the
    new start times are measured off the trimmed pieces rather than derived
    from the endpoint's offsets, which overshoot the real file.
    """
    slices = []
    for i, start in enumerate(starts):
        out = workdir / f"tight{i:02d}.wav"
        # Seeking on the input, not the output: output-side -ss/-to are applied
        # after the filter chain, where `areverse` has already reordered the
        # timestamps, and the trim silently does nothing.
        args = ["ffmpeg", "-y", "-ss", f"{start:.3f}"]
        if i + 1 < len(starts):
            args += ["-to", f"{starts[i + 1]:.3f}"]
        args += ["-i", str(audio)]
        args += [
            "-af",
            # Trimming the tail means reversing, trimming the head, reversing
            # back: silenceremove only ever works on the front of a stream.
            "areverse,"
            "silenceremove=start_periods=1:start_duration=0"
            f":start_threshold={SILENCE_FLOOR}:start_silence={JOIN_SILENCE},"
            "areverse",
            str(out),
        ]
        _run(args)
        slices.append(out)

    joined = concat_audio(slices, workdir / "narration-tight.wav")
    starts, clock = [], 0.0
    for part in slices:
        starts.append(clock)
        clock += audio_seconds(part)
    return joined, starts


async def _narration_track(cards: list[dict], workdir: Path,
                           locale: str = locales.DEFAULT) -> tuple[Path, list[float]]:
    """One audio track for the whole Script, plus the start time of each Card.

    Preferred: a single synthesis call, so the delivery never resets mid-clip.
    Fallback: speak each Card on its own and join them, which is choppier but
    always works.
    """
    single = await narrate(cards, workdir / "narration.mp3", locale)
    if single:
        return tighten(*single, workdir)

    logger.info("ใช้วิธีพูดทีละ card แทน (ขอบเขตประโยคไม่น่าเชื่อถือ)")
    parts = await asyncio.gather(
        *(speak(_tts_text(c, locale), workdir / f"card{i:02d}.mp3", locale)
          for i, c in enumerate(cards))
    )
    starts, clock = [], 0.0
    for part in parts:
        starts.append(clock)
        clock += audio_seconds(part)
    return concat(parts, workdir / "narration.mp3"), starts


async def _ready(path: Path) -> Path:
    """A Footage file that already exists, shaped like footage.fetch()."""
    return path


async def build(script: dict, workdir: Path,
                supplied: dict[int, Path] | None = None,
                locale: str = locales.DEFAULT) -> tuple[Path, dict]:
    """Script → mp4, plus the parameters it was actually built with.

    The second return value is what the Manifest records: the workdir is wiped
    after every render, so anything not handed back here is gone for good.

    `supplied` maps a Card index to Footage a human generated in Google Flow
    (docs/adr/0005). Those Cards skip the Pexels search entirely; the rest are
    unaffected.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    cards = script["cards"]
    supplied = {i: Path(p) for i, p in (supplied or {}).items() if Path(p).is_file()}

    narration, clips = await asyncio.gather(
        _narration_track(cards, workdir, locale),
        asyncio.gather(
            *(_ready(supplied[i]) if i in supplied
              else footage.fetch(c.get("query", ""), workdir / f"broll{i:02d}.mp4")
              for i, c in enumerate(cards))
        ),
    )
    audio, starts = narration
    total = audio_seconds(audio)
    # Each Card runs until the next one starts; the last runs past the end of
    # the narration so the mux trims video rather than speech.
    spans = [
        (starts[i + 1] if i + 1 < len(starts) else total + TAIL_PAD) - starts[i]
        for i in range(len(starts))
    ]

    segments = []
    for i, (card, seconds, clip) in enumerate(zip(cards, spans, clips)):
        png = draw_card(
            card, workdir / f"card{i:02d}.png", is_hook=(i == 0), over_footage=bool(clip)
        )
        out = workdir / f"seg{i:02d}.mp4"
        if clip:
            segments.append(_segment_over_footage(png, seconds, clip, out))
        else:
            # Alternate push and pull so a run of gradient cards does not feel
            # like one long drift.
            segments.append(_segment(png, seconds, out, zoom_in=(i % 2 == 0)))

    write_srt(cards, starts, total, workdir / "captions.srt")

    silent = concat(segments, workdir / "silent.mp4")
    music = pick_music()
    if music:
        logger.info("ใส่เพลงประกอบ: %s", music.name)
    clip = mux(silent, audio, workdir / "clip.mp4", music)

    details = {
        "seconds": round(audio_seconds(clip), 3),
        "frame": [W, H],
        "locale": locale,
        "voice": locales.voice(locale),
        "rate": os.environ.get("TTS_RATE", "+0%"),
        "pitch": os.environ.get("TTS_PITCH", "+0Hz"),
        "join_silence": JOIN_SILENCE,
        "bgm": music.name if music else None,
        # The narration rides along: `/retention` names the Card a drop-off
        # landed on, and a Card with only timings to show would name it blank.
        "cards": [
            {"start": round(start, 3), "seconds": round(span, 3),
             "footage": bool(clip_path),
             # Which Footage a Card got, so "does a Flow hook hold viewers
             # longer than a stock one" is answerable later without guessing.
             "footage_source": ("flow" if i in supplied else "pexels") if clip_path else None,
             "narration": card["narration"]}
            for i, (card, start, span, clip_path) in enumerate(zip(cards, starts, spans, clips))
        ],
    }
    return clip, details
