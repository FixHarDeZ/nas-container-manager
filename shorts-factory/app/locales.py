"""Everything that changes when a Clip is written for a different audience.

A Locale is one audience, not one language: the words are only the start of
it. The voice that reads them, how many characters fit on a line of the card,
which country's trends feed the Topic list, where the finished file lands and
which channel it is eventually published to all move together, and moving one
without the others produces a Clip that is wrong in a way nobody notices until
it is on YouTube.

Read through `get()` so an unknown code degrades to Thai rather than raising:
a Manifest written before Locales existed carries no `locale` field at all.

Environment is read at call time, never at import, so the tests can swap a
voice without reloading the module.
"""
from __future__ import annotations

import os

DEFAULT = "th"

LOCALES = {
    "th": {
        "code": "th",
        # Shown in the Telegram messages, which stay Thai whatever the Clip is.
        "label": "ไทย",
        "voice_env": "TTS_VOICE",
        "voice_default": "th-TH-NiwatNeural",
        # Told to the model, which cannot measure pixels. Thai glyphs are
        # narrow: 34 characters came to 719px of the 864px available at full
        # size, measured over every line this bot has written.
        "target_chars": 22,
        "hard_max_chars": 34,
        # Latin gives the voice an English accent mid-sentence; Thai gives an
        # English voice a word it cannot say at all. Same rule, mirrored.
        "spoken_script": "thai",
        "captions": "th",
        # Files land straight in /output, where they always have.
        "subdir": "",
        "trends_geo": "TH",
        "trends_region": "TH",
        "youtube_prefix": "YOUTUBE_",
    },
    "en": {
        "code": "en",
        "label": "อังกฤษ",
        "voice_env": "TTS_VOICE_EN",
        "voice_default": "en-US-AndrewNeural",
        # Latin is about 50px a character in Waree-Bold at full size against
        # Thai's 21 (measured on the container, 2026-09-07), so the same 34
        # would shrink every card to the 40px floor. 24 keeps the font near
        # 66px, which is still readable on a phone.
        "target_chars": 18,
        "hard_max_chars": 24,
        "spoken_script": "latin",
        # Unlike Thai, the character count is binding: the renderer's pixel
        # floor would pass a 38-character Latin line and then draw it at the
        # 40px minimum. See script._too_wide().
        "enforce_char_count": True,
        "captions": "en",
        "subdir": "en",
        "trends_geo": "US",
        "trends_region": "US",
        "youtube_prefix": "YOUTUBE_EN_",
    },
}


def get(code: str | None) -> dict:
    return LOCALES.get(code or DEFAULT, LOCALES[DEFAULT])


def codes() -> list[str]:
    return sorted(LOCALES)


def voice(code: str | None) -> str:
    spec = get(code)
    return os.environ.get(spec["voice_env"], spec["voice_default"])
