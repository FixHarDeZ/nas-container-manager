"""The smallest checks that fail if the pipeline's logic breaks.

Run inside the image, where Raqm and the Thai fonts exist:
    docker compose run --rm --entrypoint pytest shorts-factory tests/ -v
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import pathlib
import subprocess
import time

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "42")
os.environ.setdefault("MIMO_API_KEY", "test-key")
os.environ.setdefault("MIMO_BASE_URL", "https://example.invalid/v1")

from app import (analytics, backfill, experiment, history, locales, main, manifest, storyboard,  # noqa: E402
                 render, retention, script as script_gen, snapshots, trends,  # noqa: E402
                 youtube)


async def _nothing(*args, **kwargs):
    return {"message_id": 7}


def a_card(text: str = "ทดสอบการ์ด", code: str | None = None) -> dict:
    return {
        "lines": [text],
        "code": code,
        "query": "server room racks",
        "narration": "อ่านออกเสียงประโยคนี้",
        "spoken": "อ่านออกเสียงประโยคนี้",
    }


def a_script(cards: int = 5) -> dict:
    return {
        "title": "ทดสอบ",
        "description": "คำอธิบาย",
        "hashtags": ["#devops"],
        "category": "เทค",
        "cards": [a_card() for _ in range(cards)],
    }


# --- script validation -------------------------------------------------------

def test_valid_script_passes():
    assert script_gen.validate(a_script())["cards"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s.update(cards=s["cards"][:2]), id="too-few-cards"),
        pytest.param(lambda s: s.update(cards=s["cards"] * 3), id="too-many-cards"),
        pytest.param(lambda s: s.pop("hashtags"), id="missing-field"),
        pytest.param(lambda s: s["cards"][0].update(lines=["ก" * 60]), id="line-absurdly-long"),
        pytest.param(lambda s: s["cards"][0].update(lines=[]), id="no-lines"),
        pytest.param(lambda s: s["cards"][0].update(narration="  "), id="empty-narration"),
        pytest.param(lambda s: s["cards"][0].update(query=""), id="empty-footage-query"),
        pytest.param(lambda s: s["cards"][0].pop("spoken"), id="missing-spoken"),
        pytest.param(
            lambda s: s["cards"][0].update(spoken="ปัญหาคือ Docker เขียน log ไม่หยุด"),
            id="latin-in-spoken",
        ),
    ],
)
def test_bad_script_is_rejected(mutate):
    script = a_script()
    mutate(script)
    with pytest.raises(script_gen.ScriptError):
        script_gen.validate(script)


def test_slightly_over_target_line_is_accepted():
    """A line a few characters over target must not fail the whole clip —
    the renderer shrinks it. Only absurd lines are rejected."""
    script = a_script()
    script["cards"][0]["lines"] = ["ก" * (script_gen.TARGET_CHARS_PER_LINE + 1)]
    assert script_gen.validate(script)


def hanging_client(delays):
    """Each call sleeps for the next delay, then answers with its index."""
    calls = []

    class Fake:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    import types
                    mine = len(calls)
                    calls.append(kwargs.get("model"))
                    await asyncio.sleep(delays[mine])
                    msg = types.SimpleNamespace(content=f"answer-{mine}")
                    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    return Fake, calls


def test_a_hung_request_is_overtaken_by_its_twin(monkeypatch):
    """Observed 2026-08-27: headers at 19:25:45, no body for ten minutes.

    Waiting it out costs the human ten minutes; cutting every slow call off
    costs the long thinks that do finish (measured up to 347s). So a second
    request goes out alongside the first and the winner is whoever answers.
    """
    monkeypatch.setattr(script_gen, "HEDGE_AFTER", 0.05)
    monkeypatch.setattr(script_gen, "HEDGE_MIN_ROOM", 0.01)
    client, calls = hanging_client([30, 0.05])   # first hangs, twin answers

    text = asyncio.run(script_gen._say(client, [], 0.8, budget=5))
    assert text == "answer-1", "the twin's answer is the one used"
    # the twin goes to the other model: both requests stuck in one episode is
    # exactly the failure that was observed
    assert calls[1] == script_gen.FALLBACK_MODEL != calls[0]


def test_a_slow_but_healthy_answer_is_never_thrown_away(monkeypatch):
    """A 347s think has to land — that is the failure that started all this."""
    monkeypatch.setattr(script_gen, "HEDGE_AFTER", 0.05)
    monkeypatch.setattr(script_gen, "HEDGE_MIN_ROOM", 0.01)
    client, calls = hanging_client([0.2, 30])    # first is slow but arrives

    text = asyncio.run(script_gen._say(client, [], 0.8, budget=5))
    assert text == "answer-0"
    assert len(calls) == 2, "the hedge goes out, and is simply not needed"


def test_both_requests_hanging_still_gives_up_on_the_budget(monkeypatch):
    monkeypatch.setattr(script_gen, "HEDGE_AFTER", 0.05)
    client, _ = hanging_client([30, 30])

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(script_gen._say(client, [], 0.8, budget=0.3))


def fake_client(replies):
    """A client that hands out the prepared answers in order.

    A string is an answer; a number is "hang for this many seconds".
    """
    queue = list(replies)

    class Fake:
        asked = []

        class chat:
            class completions:
                @staticmethod
                async def create(model=None, **_):
                    import types
                    Fake.asked.append(model)
                    nxt = queue.pop(0)
                    if not isinstance(nxt, str):
                        await asyncio.sleep(nxt)
                    msg = types.SimpleNamespace(content=nxt)
                    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    return Fake


def test_a_broken_script_is_retried_before_giving_up(monkeypatch):
    """Telling the model what it got wrong usually fixes it in one go.

    A *timeout* is not retried, and cannot be: the deadline is shared, so an
    attempt that runs out of time has spent the whole budget by definition.
    Only a schema slip leaves time on the clock."""
    good = json.dumps({
        "title": "t", "description": "d", "hashtags": ["#x"], "category": "เทค",
        "cards": [a_card() for _ in range(5)],
    })
    monkeypatch.setattr(script_gen, "_client", lambda: fake_client(["ไม่ใช่ JSON", good]))

    result = asyncio.run(script_gen.generate("หัวข้อ"))
    assert result["title"] == "t"


def test_real_thai_line_over_the_char_count_is_accepted():
    """Character count is not the rule — pixels are. This 35-character line of
    ordinary Thai draws at 654px against 864px of usable frame, so losing a
    whole script to it (as happened 2026-08-29) is a false reject."""
    line = "น้ำท่วมปีนี้มาเร็วกว่าที่คิดไว้มากๆ"
    assert len(line) > script_gen.HARD_MAX_CHARS_PER_LINE
    assert script_gen._too_wide(line) == 0
    script = a_script()
    script["cards"][0]["lines"] = [line]
    assert script_gen.validate(script)


def test_line_that_would_overflow_is_rejected():
    script = a_script()
    script["cards"][0]["lines"] = ["ก" * 60]
    with pytest.raises(script_gen.ScriptError, match="กว้างเกินการ์ด"):
        script_gen.validate(script)


def test_hard_max_line_still_fits_the_card():
    longest = "ก" * script_gen.HARD_MAX_CHARS_PER_LINE
    usable = render.CW - render.MARGIN * 2
    font = render._fit([longest], render.THAI_BOLD, render.TEXT_SIZE, usable)
    assert font.getlength(longest) <= usable


def test_prosody_settings_reach_edge_tts(monkeypatch, tmp_path):
    """Rate and pitch must be passed through, not silently dropped."""
    seen = {}

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
            seen.update(text=text, voice=voice, rate=rate, pitch=pitch)

        async def save(self, path):
            pathlib.Path(path).write_bytes(b"")

    monkeypatch.setattr(render.edge_tts, "Communicate", FakeCommunicate)
    monkeypatch.setenv("TTS_RATE", "+12%")
    monkeypatch.setenv("TTS_PITCH", "-20Hz")
    asyncio.run(render.speak("ทดสอบ", tmp_path / "a.mp3"))

    assert seen["rate"] == "+12%"
    assert seen["pitch"] == "-20Hz"


def test_the_budget_is_shared_across_attempts(monkeypatch):
    """Two full-length attempts is twenty minutes of a human staring at
    'กำลังเขียนสคริปต์...'. httpx cannot enforce this: its timeout is per read,
    so a server that trickles bytes resets that clock forever."""
    monkeypatch.setattr(script_gen, "BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(script_gen, "MIN_ATTEMPT", 0.2)
    started = time.monotonic()

    class Hanging:
        class chat:
            class completions:
                @staticmethod
                async def create(**_):
                    await asyncio.sleep(30)

    monkeypatch.setattr(script_gen, "_client", lambda: Hanging)
    with pytest.raises(script_gen.ScriptError, match="ไม่ตอบภายใน"):
        asyncio.run(script_gen.generate("หัวข้อ"))
    # one attempt burned the budget; the second is never started
    assert time.monotonic() - started < 1.0


def test_llm_client_has_a_bounded_timeout(monkeypatch):
    """A stalled response must not freeze the bot's only loop."""
    monkeypatch.setenv("MIMO_TIMEOUT_SECONDS", "42")
    client = script_gen._client()
    assert client.timeout == 42
    assert client.max_retries == 1


def test_parse_unwraps_fenced_json():
    assert script_gen._parse('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_rejects_prose():
    with pytest.raises(script_gen.ScriptError):
        script_gen._parse("ไม่มี JSON เลย")


# --- delivery naming ---------------------------------------------------------

def test_slugify_keeps_thai_and_drops_separators():
    assert main.slugify("Docker บน NAS: ทำไม/ช้า?") == "Docker-บน-NAS-ทำไมช้า"


def test_slugify_never_returns_empty():
    assert main.slugify("???") == "clip"


# --- trust boundary ----------------------------------------------------------

def test_help_lists_every_command_the_bot_answers(monkeypatch):
    """A command the help forgets is a command nobody remembers using."""
    import re

    answered = set(re.findall(r'text\.startswith\("(/\w+)"\)', pathlib.Path(
        main.__file__).read_text(encoding="utf-8")))
    answered -= {"/start"}   # an alias for /help, not worth a line of its own
    missing = [command for command in answered if command not in main.HELP]
    assert not missing, f"ไม่ได้อธิบายไว้ใน /help: {missing}"


def test_a_message_over_telegrams_limit_is_sent_in_pieces():
    """Over 4096 characters sendMessage answers 400 and delivers nothing.

    That is how /help went silent: it grew to 4690 characters and every
    invocation logged a 400 nobody was reading.
    """
    pieces = main.chunks(main.HELP)
    assert len(pieces) > 1
    assert all(len(piece) <= main.TELEGRAM_TEXT_LIMIT for piece in pieces)
    # Split on paragraph breaks, so nothing is cut mid-sentence and the whole
    # page still arrives.
    assert "\n\n".join(pieces) == main.HELP


def test_a_keyboard_rides_the_last_piece_only(monkeypatch):
    """Buttons under anything but the final piece get text posted below them."""
    sent = []

    async def fake_api(client, method, **payload):
        sent.append(payload)
        return {"message_id": len(sent)}

    monkeypatch.setattr(main, "api", fake_api)
    result = asyncio.run(main.say(None, main.HELP, parse_mode="HTML",
                                  reply_markup={"inline_keyboard": []}))
    assert len(sent) > 1
    assert [bool(payload.get("reply_markup")) for payload in sent[:-1]] == [False] * (len(sent) - 1)
    assert sent[-1].get("reply_markup") is not None
    # The caller edits this message later, so it must be the one with buttons.
    assert result["message_id"] == len(sent)
    # parse_mode is not tail-only: a piece sent without it renders its markup
    # as literal text, and the piece after it carries an unbalanced tag.
    assert all(payload.get("parse_mode") == "HTML" for payload in sent)


def test_commands_work_while_a_script_is_waiting_for_review(monkeypatch):
    """Plain text revises the pending Script — a command must not become feedback."""
    called = []

    async def boom(*args, **kwargs):
        called.append("make_script")

    monkeypatch.setattr(main, "make_script", boom)
    monkeypatch.setattr(main, "say", _nothing)
    seen = []

    async def fake_retention(client, video_id=""):
        seen.append(video_id)

    monkeypatch.setattr(main, "on_retention", fake_retention)
    monkeypatch.setattr(main, "on_stats", lambda client: _nothing())

    state = {"mode": "review", "topic": "หัวข้อเดิม", "script": {}}
    asyncio.run(main.on_text(None, state, "/retention abc123"))
    assert seen == ["abc123"] and not called

    # ...and plain text in the same state still revises, as before
    asyncio.run(main.on_text(None, state, "แก้ hook หน่อย"))
    assert called == ["make_script"]


def test_updates_from_other_chats_are_dropped():
    # Read the id off the module: this suite also runs against the real .env.
    mine, theirs = main.CHAT_ID, main.CHAT_ID + 1
    assert main.is_ours({"message": {"chat": {"id": mine}, "text": "hi"}})
    assert not main.is_ours({"message": {"chat": {"id": theirs}, "text": "hi"}})
    assert main.is_ours({"callback_query": {"message": {"chat": {"id": mine}}}})
    assert not main.is_ours({"callback_query": {"message": {"chat": {"id": theirs}}}})


# --- drawing (needs Raqm + the Thai font, i.e. the real image) ---------------

def test_card_over_footage_is_transparent_at_frame_size(tmp_path):
    """Over B-roll the card must be an overlay, not an opaque background."""
    from PIL import Image

    path = render.draw_card(a_card(), tmp_path / "o.png", over_footage=True)
    with Image.open(path) as img:
        assert img.size == (render.W, render.H)
        assert img.mode == "RGBA"
        assert img.getpixel((5, 5))[3] == 0  # corner is see-through


def test_footage_is_skipped_without_a_key(monkeypatch):
    from app import footage

    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    assert not footage.enabled()
    assert asyncio.run(footage.fetch("server room", pathlib.Path("/tmp/none.mp4"))) is None


def test_card_is_drawn_oversized_for_the_zoom():
    from PIL import Image

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = render.draw_card(
            a_card("เก็บไว้ที่ไหน", code="docker ps -a"), pathlib.Path(tmp) / "c.png"
        )
        with Image.open(path) as img:
            # Bigger than the frame: the Ken Burns crop lives in the extra pixels.
            assert img.size == (render.CW, render.CH)
            assert img.size > (render.W, render.H)


def test_long_line_shrinks_to_fit():
    wide = "ตั้งค่าคอนเทนเนอร์ให้ครบทุกอย่าง"
    font = render._fit([wide], render.THAI_BOLD, render.TEXT_SIZE, render.CW - render.MARGIN * 2)
    assert font.getlength(wide) <= render.CW - render.MARGIN * 2


def test_zoom_lands_exactly_on_the_last_frame():
    """The move must finish with the narration, not before or after it."""
    frames = 150
    rate = float(render.ken_burns(frames, True).split("min(1+")[1].split("*on")[0])
    assert 1 + rate * (frames - 1) == pytest.approx(render.OVERSCAN)


def test_zoom_out_starts_wide_and_ends_at_frame_size():
    assert f"max({render.OVERSCAN}-" in render.ken_burns(90, False)


def test_segment_is_frame_sized_silent_and_the_right_length(tmp_path):
    """Segments carry no audio — the narration is muxed over the whole clip.

    A segment that kept its own audio track would make `concat -c copy`
    produce garbage.
    """
    png = render.draw_card(a_card("ทดสอบการซูม"), tmp_path / "c.png")
    out = render._segment(png, 2.0, tmp_path / "s.mp4")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    assert probe == [f"video,{render.W},{render.H}"]  # one stream, no audio

    seconds = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(out)],
        capture_output=True, text=True).stdout.strip())
    assert seconds == pytest.approx(2.0, abs=0.1)


def test_narration_is_one_take_with_a_start_per_card():
    """One synthesis call for the whole Script, and boundaries that line up."""
    import tempfile

    cards = [
        {"narration": "ปัญหาคือ ด็อกเกอร์ ไม่ได้จำกัดขนาดล็อกให้เรา, มันจะเขียนไปเรื่อยๆ"},
        {"narration": "วิธีแก้ง่ายสุด คือใส่ออปชันตอนรันคอนเทนเนอร์"},
        {"narration": "สรุปคือ ตั้งค่าไว้เสมอ, กันดิสก์เต็มได้ชัวร์"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        result = asyncio.run(render.narrate(cards, pathlib.Path(tmp) / "n.mp3"))
        assert result is not None, "sentence boundaries did not line up"
        audio, starts = result

        assert len(starts) == len(cards)
        assert starts[0] == 0.0
        assert starts == sorted(starts), "card starts must run forward"
        # every cut lands inside the file
        assert starts[-1] < render.audio_seconds(audio)


def test_the_voice_reads_the_transliteration_and_the_screen_keeps_the_english():
    card = {"narration": "ปัญหาคือ Docker เขียน log ไม่หยุด",
            "spoken": "ปัญหาคือ ด็อกเกอร์ เขียน ล็อก ไม่หยุด"}
    assert render._tts_text(card) == "ปัญหาคือ ด็อกเกอร์ เขียน ล็อก ไม่หยุด"
    # a Script written before `spoken` existed still renders
    assert render._tts_text({"narration": "มีแต่ narration"}) == "มีแต่ narration"


def test_a_pronunciation_override_does_not_break_card_alignment(monkeypatch, tmp_path):
    """The override must be applied before `narrate()` compares boundaries.

    Substituting anywhere later leaves the check holding the pre-substitution
    text while the voice reports the post-substitution one: every Card looks
    misaligned, `narrate()` returns None, and the Clip silently drops to
    per-Card speech with restarted prosody.
    """
    monkeypatch.setattr(render, "SAY_PATH", tmp_path / "say.json")
    render.say_set("ทีเอไอพาสปอร์ต", "ไทยพาสปอร์ต")
    assert render.say_as() == {"ทีเอไอพาสปอร์ต": "ไทยพาสปอร์ต"}

    cards = [{"narration": "TH-AI Passport คืออะไร", "spoken": "ทีเอไอพาสปอร์ตคืออะไร"},
             {"narration": "ต่างจากเล่มเดิมยังไง", "spoken": "ต่างจากเล่มเดิมยังไง"}]

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
            self.parts = text.split(render.CARD_SEPARATOR)

        async def stream(self):
            yield {"type": "audio", "data": b""}
            for i, part in enumerate(self.parts):
                # what the voice actually saw, offsets in 100ns ticks
                yield {"type": "SentenceBoundary",
                       "offset": (i + 1) * 5 * 10**7, "text": part}

    monkeypatch.setattr(render.edge_tts, "Communicate", FakeCommunicate)
    result = asyncio.run(render.narrate(cards, tmp_path / "n.mp3"))

    assert result is not None, "override desynced the boundary check"
    _, starts = result
    assert starts == [0.0, 10.0]

    render.say_set("ทีเอไอพาสปอร์ต", "")
    assert render.say_as() == {}


def test_say_sets_lists_and_deletes(monkeypatch, tmp_path):
    """The command the human types, all three branches of it."""
    monkeypatch.setattr(render, "SAY_PATH", tmp_path / "say.json")
    sent = []

    async def fake_say(client, text):
        sent.append(text)

    monkeypatch.setattr(main, "say", fake_say)

    asyncio.run(main.on_say(None, "ทีเอไอ = ไทย"))
    assert render.say_as() == {"ทีเอไอ": "ไทย"}
    asyncio.run(main.on_say(None, ""))
    assert "ทีเอไอ" in sent[-1] and "ไทย" in sent[-1]
    asyncio.run(main.on_say(None, "ทีเอไอ ="))
    assert render.say_as() == {}
    # a missing left-hand side would write an entry that matches everything
    asyncio.run(main.on_say(None, "= ไทย"))
    assert render.say_as() == {}


@pytest.mark.parametrize("topic", [
    "วอลเล่หญิงไทยชนะจีนได้ไปโอลิมปิก",
    "วอลเล่ย์บอลหญิง U19 จีนแพ้ไทยเพราะอะไร",
    "วิเคราะห์ทีมวอลเลย์บอลไทยกับจีน เจอกันไทยชนะ 3-2",
])
def test_a_topic_about_a_result_is_turned_away(monkeypatch, topic):
    """The model has no source for a score, so it invents one — see the six
    volleyball clips of 2026-08-29..31, all the same essay."""
    sent = []

    async def fake_say(client, text, **kw):
        sent.append(text)

    async def never(*a, **kw):
        raise AssertionError("a result topic reached the model")

    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main.script_gen, "generate", never)
    state = {"mode": "idle", "auto_pick": "แตะไม่ได้"}
    asyncio.run(main.make_script(None, state, topic))

    assert sent and "ไม่รู้ผลแข่ง" in sent[0]
    # nothing was claimed: no Manifest, no Variant, and the pending pick lives
    assert state == {"mode": "idle", "auto_pick": "แตะไม่ได้"}


def test_a_bare_url_is_turned_away_before_reaching_the_model(monkeypatch):
    """Telegram's link preview is not part of message.text, so the model would
    receive only an opaque URL and answer prose instead of the Script JSON."""
    sent = []

    async def fake_say(client, text, **kw):
        sent.append(text)

    async def never(*a, **kw):
        raise AssertionError("a bare URL reached the model")

    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main.script_gen, "generate", never)
    monkeypatch.setattr(main.manifest, "start", lambda topic, locale="th": "test-id")
    monkeypatch.setattr(main.manifest, "update", lambda *a, **kw: None)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    state = {"mode": "idle", "auto_pick": "แตะไม่ได้"}
    asyncio.run(main.make_script(
        None, state, "https://marketeeronline.co/archives/484466",
    ))

    assert sent and "พิมพ์หัวข้อ" in sent[0] and "ลิงก์" in sent[0]
    assert state == {"mode": "idle", "auto_pick": "แตะไม่ได้"}


@pytest.mark.parametrize("topic", [
    "TH-AI Passport คืออะไร ต่างจาก Digital ID ยังไง",
    "วอลเลย์บอลไทยเล่นสไตล์ไหน ต่างจากทีมตัวสูงยังไง",
    "iPhone 18 Pro Max ราคาคาดการณ์เท่าไหร่",
])
def test_ordinary_topics_still_get_through(topic):
    assert not main.result_shaped(topic)


def test_the_force_prefix_gets_past_the_guard_and_is_not_part_of_the_topic(monkeypatch):
    seen = {}

    async def fake_say(client, text, **kw):
        return None

    async def fake_generate(topic, **kw):
        seen["topic"] = topic
        raise RuntimeError("stop here — the guard is what is under test")

    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main.script_gen, "generate", fake_generate)
    monkeypatch.setattr(main.manifest, "start", lambda topic, locale="th": "test-id")
    monkeypatch.setattr(main.manifest, "update", lambda *a, **kw: None)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    asyncio.run(main.make_script(None, {"mode": "idle"}, "!ไทยชนะจีน 3-2"))

    assert seen["topic"] == "ไทยชนะจีน 3-2"


def test_redo_renders_the_last_script_without_asking_the_model(monkeypatch):
    """/redo exists so a /say fix does not cost a whole rewrite."""
    rendered = {}
    sent = []

    async def fake_say(client, text, **kw):
        sent.append(text)

    async def never(*a, **kw):
        raise AssertionError("/redo went back to the model")

    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main.script_gen, "generate", never)
    monkeypatch.setattr(main, "spawn", lambda coro, name: rendered.update(job=name) or coro.close())
    state = {"mode": "idle", "last_script": {"title": "เดิม"}, "last_topic": "หัวข้อเดิม",
             "last_clip_id": "clip-1"}
    asyncio.run(main.on_redo(None, state))

    assert rendered["job"] == "do_render"
    # the re-render belongs to the Clip it came from, not a new one
    assert state["clip_id"] == "clip-1" and state["topic"] == "หัวข้อเดิม"
    assert state["script"] == {"title": "เดิม"}


def test_redo_without_a_previous_clip_says_so(monkeypatch):
    sent = []

    async def fake_say(client, text, **kw):
        sent.append(text)

    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main, "spawn", lambda *a, **kw: pytest.fail("nothing to render"))
    asyncio.run(main.on_redo(None, {"mode": "idle"}))
    assert "ยังไม่มีคลิปล่าสุด" in sent[0]


def test_a_mistyped_command_is_not_treated_as_a_topic(monkeypatch):
    """/stat opened a Clip on 2026-08-30 and /redo on 2026-08-31, both spending
    minutes of model time on a typo."""
    sent = []

    async def fake_say(client, text, **kw):
        sent.append(text)

    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main, "spawn", lambda *a, **kw: pytest.fail("a typo reached the model"))
    asyncio.run(main.on_text(None, {"mode": "idle"}, "/stat"))
    assert "ไม่รู้จักคำสั่ง" in sent[0]


def _speech_runs(path: pathlib.Path) -> list[tuple[float, float]]:
    """(end, duration) of every silence longer than a clause break."""
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-af", f"silencedetect=noise={render.SILENCE_FLOOR}:d=0.2", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return [line for line in done.stderr.splitlines() if "silence_duration" in line]


def test_card_joins_lose_their_dead_air(tmp_path):
    """A paragraph break leaves ~1s of silence at every Card join; trim it.

    Stands in for narration with tone bursts: two seconds of speech, a second
    of nothing, two more seconds.
    """
    source = tmp_path / "src.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
         "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[a]",
         "-map", "[a]", str(source)],
        capture_output=True, check=True,
    )
    assert len(_speech_runs(source)) == 1, "the fixture should have one long gap"

    tight, starts = render.tighten(source, [0.0, 3.0], tmp_path)

    assert len(starts) == 2 and starts[0] == 0.0
    # the gap is cut back to a clause-length pause, not removed outright
    assert starts[1] == pytest.approx(2.0 + render.JOIN_SILENCE, abs=0.1)
    assert render.audio_seconds(tight) == pytest.approx(4.0 + render.JOIN_SILENCE, abs=0.15)
    # both Cards are still there: one gap between them, none inside them
    assert len(_speech_runs(tight)) == 1


def test_no_music_folder_means_no_music(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "BGM_DIR", tmp_path / "missing")
    assert render.pick_music() is None


def test_empty_music_folder_means_no_music(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "BGM_DIR", tmp_path)
    (tmp_path / "notes.txt").write_text("not a track")
    assert render.pick_music() is None


def test_music_is_picked_from_the_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "BGM_DIR", tmp_path)
    (tmp_path / "a.mp3").write_bytes(b"")
    (tmp_path / "b.wav").write_bytes(b"")
    assert render.pick_music().name in {"a.mp3", "b.wav"}


# --- youtube -----------------------------------------------------------------

# --- captions ----------------------------------------------------------------

def test_srt_uses_raw_narration_not_the_spoken_form(tmp_path):
    """Transliteration belongs to the voice; the screen wants real English."""
    cards = [
        {"narration": "ปัญหาคือ Docker ไม่ได้จำกัดขนาด log"},
        {"narration": "สรุปคือ ตั้งค่าไว้เสมอ"},
    ]
    srt = render.write_srt(cards, [0.0, 5.0], 9.5, tmp_path / "c.srt")
    body = srt.read_text(encoding="utf-8")

    assert "Docker" in body and "ด็อกเกอร์" not in body
    assert "00:00:00,000 --> 00:00:05,000" in body
    # the last cue ends at the audio length, not at some reported duration
    assert "00:00:05,000 --> 00:00:09,500" in body


def test_srt_timestamps_cross_the_minute_boundary(tmp_path):
    srt = render.write_srt([{"narration": "ท้ายคลิป"}], [65.25], 71.5, tmp_path / "c.srt")
    assert "00:01:05,250 --> 00:01:11,500" in srt.read_text(encoding="utf-8")


# --- manifest ----------------------------------------------------------------

def test_every_draft_is_kept_including_the_discarded_ones(tmp_path, monkeypatch):
    """A Variant that writes badly must not be flattered by the human's taste."""
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")

    kept = manifest.start("ทำไม log บวม")
    manifest.add_script(kept, a_script())
    manifest.add_script(kept, a_script())          # a revision
    manifest.update(kept, outcome="rendered", render={"seconds": 41.2})
    manifest.update(kept, published=True, video_id="abc123")

    thrown = manifest.start("หัวข้อที่ไม่ชอบ")
    manifest.add_script(thrown, a_script())
    manifest.update(thrown, outcome="discarded")

    records = {r["id"]: r for r in manifest.load_all()}
    assert len(records) == 2, "the discarded draft must survive too"
    assert len(records[kept]["scripts"]) == 2, "revisions are kept, not overwritten"
    assert records[kept]["published"] is True and records[kept]["video_id"] == "abc123"
    assert records[thrown]["published"] is False
    assert records[thrown]["outcome"] == "discarded"


def test_manifests_opened_in_the_same_millisecond_do_not_overwrite(tmp_path, monkeypatch):
    """The backfill opens them in a tight loop; a shared id would erase data."""
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    ids = [manifest.start(f"หัวข้อ {i}") for i in range(20)]
    assert len(set(ids)) == 20
    assert len(manifest.load_all()) == 20


def test_a_broken_manifest_never_breaks_the_caller(tmp_path, monkeypatch):
    """Recording is not allowed to take a render down with it."""
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    manifest.update(None, outcome="rendered")           # no id yet
    manifest.add_script("does-not-exist", a_script())   # id that was never opened
    assert manifest.load("does-not-exist") is None
    assert manifest.load_all() == []


def test_upload_credits_the_clip_it_was_offered_for(tmp_path, monkeypatch):
    """The upload button outlives the Topic that produced the clip.

    Render A, send a new Topic B, then press upload on A's message: the video
    id must land on A's Manifest, not on whichever Topic happens to be open.
    """
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(history, "PATH", tmp_path / "history.json")
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(youtube, "configured", lambda locale="th": True)

    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "send_video", _nothing)
    monkeypatch.setattr(main, "api", _nothing)

    async def fake_upload(clip, script, locale="th"):
        return "vidA", "public"

    monkeypatch.setattr(youtube, "upload", fake_upload)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really an mp4")
    script = a_script()

    first = manifest.start("หัวข้อ A")
    state = {"clip_id": first, "topic": "หัวข้อ A"}
    asyncio.run(main.deliver(None, state, script, clip))

    # the human sends a new Topic before pressing upload on the old clip
    second = manifest.start("หัวข้อ B")
    state.update(clip_id=second, topic="หัวข้อ B")

    asyncio.run(main.do_upload(None, state))

    records = {r["id"]: r for r in manifest.load_all()}
    assert records[first]["published"] is True
    assert records[first]["video_id"] == "vidA"
    assert records[second]["published"] is False, "the open Topic must not be credited"
    assert history.load()[0]["topic"] == "หัวข้อ A"


# --- trends ------------------------------------------------------------------

TRENDS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>ช่อง29</title><ht:approx_traffic>20000+</ht:approx_traffic>
  <ht:news_item><ht:news_item_title>ถ่ายทอดสด ไทย VS เกาหลีใต้</ht:news_item_title></ht:news_item>
</item>
<item><title>mac mini ชิป m6</title><ht:approx_traffic>200+</ht:approx_traffic>
  <ht:news_item><ht:news_item_title>Apple ลุ้น iPhone 18 จอพับ</ht:news_item_title></ht:news_item>
</item>
<item><title>ไม่มีตัวเลข</title></item>
</channel></rss>"""


def test_search_spikes_are_read_biggest_first():
    rows = trends.parse_rss(TRENDS_RSS)
    assert [r["term"] for r in rows] == ["ช่อง29", "mac mini ชิป m6", "ไม่มีตัวเลข"]
    assert rows[0]["traffic"] == 20000
    assert rows[0]["headline"].startswith("ถ่ายทอดสด")
    assert rows[2]["traffic"] == 0, "a spike with no volume still counts, just last"


def test_news_and_sport_never_become_topics():
    """The rule the ADR calls risk, not taste — a regression here is silent."""
    assert trends.keep("28") and trends.keep(None)
    assert not trends.keep("25"), "News & Politics"
    assert not trends.keep("17"), "Sports — live results, and they age in hours"


def test_an_old_suggestion_list_stops_crediting_topics():
    """state.json outlives restarts; a stale list would invent a trend origin."""
    import datetime as dt

    suggestion = {"topic": "ชิป M6 ต่างจาก M4 ยังไง", "from": "mac mini ชิป m6",
                  "kind": "evergreen", "category": "เทค"}
    fresh = {"suggested": [suggestion],
             "suggested_at": dt.datetime.now().isoformat(timespec="seconds")}
    stale = {"suggested": [suggestion],
             "suggested_at": (dt.datetime.now() - dt.timedelta(days=30)).isoformat()}

    assert main.trend_origin(fresh, "ชิป M6 ต่างจาก M4 ยังไง")
    assert main.trend_origin(stale, "ชิป M6 ต่างจาก M4 ยังไง") is None
    assert main.trend_origin({"suggested": [suggestion]}, "ชิป M6 ต่างจาก M4 ยังไง") is None


def test_a_topic_typed_back_from_the_suggestions_is_credited():
    """The whole point is answering 'did trend topics do better?' later."""
    import datetime as dt

    state = {"suggested": [
        {"topic": "ชิป M6 ต่างจาก M4 ยังไง", "from": "mac mini ชิป m6",
         "kind": "evergreen", "category": "เทค"},
    ], "suggested_at": dt.datetime.now().isoformat(timespec="seconds")}
    # retyped, not copied byte for byte
    origin = main.trend_origin(state, "ชิป M6 ต่างจาก M4 ยังไง แบบเข้าใจง่าย")
    assert origin["from"] == "mac mini ชิป m6" and origin["kind"] == "evergreen"
    assert main.trend_origin(state, "ทำไมแมวชอบนอนกลางวัน") is None
    assert main.trend_origin({}, "อะไรก็ได้") is None


def test_categories_are_reported_as_observation_not_experiment():
    """Topics are chosen by the human, so this can never be a randomised arm."""
    records = [
        {"outcome": "rendered", "variant": "question",
         "scripts": [{"script": {"category": "การเงิน"}}],
         "snapshots": [{"date": "d", "age_days": 7, "views": 180, "percent": 70.0}]},
        {"outcome": "rendered", "variant": "question",
         "scripts": [{"script": {"category": "เทค"}}], "trend": {"category": "เทค"},
         "snapshots": [{"date": "d", "age_days": 7, "views": 3, "percent": 40.0}]},
        {"outcome": "drafting", "scripts": [{"script": {"category": "เทค"}}], "snapshots": []},
    ]
    counts = experiment.by_category(records)
    assert counts["การเงิน"]["clips"] == 1 and counts["การเงิน"]["views"] == 180
    assert counts["เทค"]["clips"] == 1, "a topic that never became a clip is not one"
    assert counts["เทค"]["trend"] == 1

    body = experiment.report(records)
    assert "สังเกตการณ์ ไม่ใช่การทดลอง" in body
    assert body.index("การเงิน") < body.index("• เทค"), "best median first"


# --- retention ---------------------------------------------------------------

CARDS = [
    {"start": 0.0, "seconds": 4.0, "narration": "hook"},
    {"start": 4.0, "seconds": 6.0, "narration": "กลางเรื่อง"},
    {"start": 10.0, "seconds": 5.0, "narration": "สรุป"},
]


def test_a_drop_is_reported_as_a_card_not_a_fraction():
    """`elapsedVideoTimeRatio` is a fraction of the clip; the script is not."""
    rows = [[round(0.05 * i, 2), 1.0] for i in range(1, 21)]
    rows[9][1] = 0.2          # a cliff at 50% of a 15s clip = 7.5s = card 2
    curve = [[r[0], r[1], 1.0] for r in rows]

    found = retention.drops(curve, duration=15.0, cards=CARDS)
    assert [d["card"] for d in found] == [1]
    assert found[0]["at"] == pytest.approx(7.5, abs=0.1)
    assert "card 2" in retention.summary(curve, 15.0, CARDS)
    assert "กลางเรื่อง" in retention.summary(curve, 15.0, CARDS)


def test_one_cliff_spread_over_two_buckets_is_one_drop():
    """Otherwise the same moment is named three times and the labels collide."""
    curve = [[round(0.01 * i, 2), 1.0, 1.0] for i in range(1, 101)]
    for i in range(40, 100):
        curve[i][1] = 0.3          # the fall happens across buckets 0.40-0.42
    curve[40][1], curve[41][1] = 0.7, 0.5

    found = retention.drops(curve, duration=15.0, cards=CARDS)
    assert len(found) == 1
    assert found[0]["at"] == pytest.approx(0.41 * 15.0, abs=0.2), "dated from where it began"
    assert found[0]["size"] == pytest.approx(0.7, abs=0.01), "the whole fall, not one bucket"


def test_a_curve_that_only_slopes_has_no_cliffs():
    """Every clip loses viewers everywhere; only the cliffs are worth naming."""
    curve = [[round(0.05 * i, 2), 1.0 - 0.02 * i, 1.0] for i in range(1, 21)]
    assert retention.drops(curve, 15.0, CARDS) == []
    assert "ชันผิดปกติ" in retention.summary(curve, 15.0, CARDS)


def test_card_lookup_covers_the_whole_clip():
    assert retention.card_at(CARDS, 0.0) == 0
    assert retention.card_at(CARDS, 4.0) == 1
    assert retention.card_at(CARDS, 14.9) == 2
    # past the last boundary (rounding, or a tail the srt did not cover)
    assert retention.card_at(CARDS, 99.0) == 2
    assert retention.card_at([], 1.0) is None


def test_the_chart_is_drawn_with_the_card_boundaries(tmp_path):
    curve = [[round(0.01 * i, 2), max(1.0 - 0.01 * i, 0.1), 1.0] for i in range(1, 101)]
    dest = retention.chart(curve, 15.0, CARDS, tmp_path / "curve.png", "ชื่อคลิป")
    from PIL import Image

    with Image.open(dest) as img:
        assert img.size == (retention.W, retention.H)
        colours = {colour for _, colour in img.getcolors(maxcolors=1 << 20)}
    assert retention.LINE in colours, "the curve itself must be drawn"


# --- experiment --------------------------------------------------------------

def test_one_clip_in_three_breaks_the_pattern():
    """Learning only from its own past is how a channel stops improving."""
    explore = experiment.assign(roll=0.0)
    assert explore["explore"] is True and explore["variant"] is None
    assert explore["style"] == experiment.EXPLORE_CLAUSE

    assigned = experiment.assign(roll=0.99, pick="question")
    assert assigned["explore"] is False and assigned["variant"] == "question"
    # the clause is stored verbatim: the base prompt drifts, the record must not
    assert assigned["style"] == experiment.VARIANTS["question"]


def test_explore_clips_never_enter_the_arithmetic():
    records = [
        {"variant": "question", "outcome": "rendered",
         "snapshots": [{"date": "d", "age_days": 7, "views": 10, "percent": 50.0}]},
        {"explore": True, "variant": None, "outcome": "rendered",
         "snapshots": [{"date": "d", "age_days": 7, "views": 900, "percent": 99.0}]},
        {"variant": "question", "outcome": "discarded", "snapshots": []},
        {"outcome": "rendered", "snapshots": []},   # predates the experiment
    ]
    counts = experiment.tally(records)
    assert counts["question"] == {"clips": 2, "discarded": 1, "failed": 0,
                                  "views": 10, "percents": [50.0]}
    assert counts["explore"]["views"] == 900
    assert counts["shock_number"]["clips"] == 0


def test_a_topic_that_never_produced_a_script_is_not_a_clip():
    """Otherwise an arm reaches the Gate on records that made nothing."""
    records = [
        {"variant": "question", "outcome": "drafting", "snapshots": []},
        {"variant": "question", "outcome": "generate_failed", "snapshots": []},
        {"variant": "question", "outcome": "rendered", "snapshots": []},
    ]
    counts = experiment.tally(records)
    assert counts["question"]["clips"] == 1
    assert counts["question"]["failed"] == 2
    # and the discard rate is not diluted by failures
    assert counts["question"]["discarded"] == 0


def test_a_revision_keeps_the_variant_it_was_born_with(tmp_path, monkeypatch):
    """Rewriting a script you dislike must not quietly pick the winner."""
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(history, "recent_titles", lambda locale=None: [])
    monkeypatch.setattr(main, "close_prompt", _nothing)
    monkeypatch.setattr(main, "say", _nothing)

    async def no_winners(limit=3, locale="th"):
        return []

    monkeypatch.setattr(analytics, "winning_examples", no_winners)
    monkeypatch.setattr(experiment, "assign",
                        lambda locale="th": {"variant": "question", "explore": False,
                                             "style": experiment.VARIANTS["question"]})

    styles = []

    async def fake_generate(topic, previous=None, feedback="", avoid=None,
                            winners=None, style="", locale="th", sibling=None):
        styles.append(style)
        return a_script()

    monkeypatch.setattr(script_gen, "generate", fake_generate)

    state = {}
    asyncio.run(main.make_script(None, state, "หัวข้อ"))
    first = state["clip_id"]
    asyncio.run(main.make_script(None, state, "หัวข้อ", feedback="แก้ hook ให้แรงกว่านี้"))

    records = manifest.load_all()
    assert len(records) == 1, "a revision must not open a second Manifest"
    assert records[0]["id"] == first and records[0]["variant"] == "question"
    assert len(records[0]["scripts"]) == 2, "both drafts are kept"
    assert styles == [experiment.VARIANTS["question"]] * 2, "the clause must not be re-rolled"


def test_a_stalled_mimo_is_asked_again_after_a_cooldown(monkeypatch, tmp_path):
    """Every request silent at the deadline is a sick window, not a bad prompt.

    Measured twice (2026-09-07 17:04, 2026-09-08 08:02): primary and both
    hedges dead at 600s, the same Topic answered in about a minute shortly
    after. A malformed reply gets no such second chance — that one comes back
    malformed again.
    """
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(history, "recent_titles", lambda locale=None: [])
    monkeypatch.setattr(main, "close_prompt", _nothing)
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "STALL_COOLDOWN", 0)

    async def no_winners(limit=3, locale="th"):
        return []

    monkeypatch.setattr(analytics, "winning_examples", no_winners)

    tries = []

    async def stall_once(topic, **kwargs):
        tries.append(topic)
        if len(tries) == 1:
            raise script_gen.ScriptStalled("mimo ไม่ตอบภายใน 600 วินาที")
        return a_script()

    monkeypatch.setattr(script_gen, "generate", stall_once)
    state = {}
    asyncio.run(main.make_script(None, state, "หัวข้อ"))
    assert len(tries) == 2 and state["mode"] == "review"

    async def always_bad(topic, **kwargs):
        tries.append(topic)
        raise script_gen.ScriptError("สคริปต์ผิดกติกา")

    monkeypatch.setattr(script_gen, "generate", always_bad)
    tries.clear()
    asyncio.run(main.make_script(None, {"mode": "idle"}, "หัวข้อ"))
    assert len(tries) == 1, "a bad reply is not retried — only a stall is"


def _arm(percents, views, variant):
    return [
        {"variant": variant, "outcome": "rendered",
         "snapshots": [{"date": str(i), "age_days": 7, "views": views // len(percents),
                        "percent": p}]}
        for i, p in enumerate(percents)
    ]


def test_no_winner_is_named_before_the_gate():
    records = _arm([80.0], 5, "question") + _arm([40.0], 5, "shock_number")
    assert "ยังสรุปไม่ได้" in experiment.verdict(experiment.tally(records))


def test_a_small_gap_past_the_gate_is_a_draw_not_a_winner():
    percents = [50.0] * experiment.MIN_CLIPS
    records = (_arm(percents, experiment.MIN_VIEWS, "question")
               + _arm([53.0] * experiment.MIN_CLIPS, experiment.MIN_VIEWS, "shock_number"))
    verdict = experiment.verdict(experiment.tally(records))
    assert "เสมอ" in verdict and "🏆" not in verdict


def test_a_real_gap_past_the_gate_names_the_winner():
    records = (_arm([50.0] * experiment.MIN_CLIPS, experiment.MIN_VIEWS, "question")
               + _arm([70.0] * experiment.MIN_CLIPS, experiment.MIN_VIEWS, "shock_number"))
    verdict = experiment.verdict(experiment.tally(records))
    assert verdict.startswith("🏆 shock_number")


def test_day7_is_what_counts_not_the_latest_reading():
    """A clip measured yesterday must not be compared with one measured today."""
    record = {"variant": "question", "outcome": "rendered", "snapshots": [
        {"date": "2026-09-01", "age_days": 3, "views": 5, "percent": 90.0},
        {"date": "2026-09-05", "age_days": 7, "views": 40, "percent": 55.0},
        {"date": "2026-09-20", "age_days": 22, "views": 60, "percent": 51.0},
    ]}
    counts = experiment.tally([record])
    assert counts["question"]["percents"] == [55.0]
    assert counts["question"]["views"] == 40


def test_the_variant_clause_reaches_the_model(monkeypatch):
    seen = {}

    body = json.dumps({"title": "t", "description": "d", "hashtags": ["#x"],
                       "category": "เทค",
                       "cards": [a_card() for _ in range(5)]})

    class Spy:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    import types
                    seen["messages"] = kwargs["messages"]
                    msg = types.SimpleNamespace(content=body)
                    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(script_gen, "_client", lambda: Spy)
    asyncio.run(script_gen.generate("หัวข้อ", style=experiment.VARIANTS["shock_number"]))
    assert any(experiment.VARIANTS["shock_number"] in m["content"] for m in seen["messages"])


# --- snapshots ---------------------------------------------------------------

def test_snapshots_run_once_a_day_after_the_hour():
    import datetime as dt

    state = {}
    assert not snapshots.due(state, dt.datetime(2026, 8, 27, snapshots.HOUR - 1, 59))
    assert snapshots.due(state, dt.datetime(2026, 8, 27, snapshots.HOUR, 0))

    state["last_snapshot"] = "2026-08-27"
    assert not snapshots.due(state, dt.datetime(2026, 8, 27, 23, 59)), "twice in one day"
    # a day skipped (bot down) is picked up at the next tick, not lost
    assert snapshots.due(state, dt.datetime(2026, 8, 29, snapshots.HOUR, 5))


def test_only_young_published_clips_are_measured():
    import datetime as dt

    today = dt.date(2026, 8, 27)
    records = [
        {"id": "young", "video_id": "a", "published_at": "2026-08-26T10:00:00"},
        {"id": "old", "video_id": "b", "published_at": "2026-06-01T10:00:00"},
        {"id": "never-published", "created_at": "2026-08-26T10:00:00"},
    ]
    assert set(snapshots._wanted(records, today)) == {"a"}


def test_a_snapshot_taken_twice_in_a_day_does_not_double_up(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    clip_id = manifest.start("หัวข้อ")
    manifest.add_snapshot(clip_id, {"date": "2026-08-28", "age_days": 1, "views": 3})
    manifest.add_snapshot(clip_id, {"date": "2026-08-28", "age_days": 1, "views": 9})
    manifest.add_snapshot(clip_id, {"date": "2026-09-03", "age_days": 7, "percent": 61.0})

    record = manifest.load(clip_id)
    assert [s["date"] for s in record["snapshots"]] == ["2026-08-28", "2026-09-03"]
    assert record["snapshots"][0]["views"] == 9, "the later reading wins"
    # experiments compare every clip at the same age, never at "latest"
    assert manifest.day7(record)["percent"] == 61.0


def test_day7_is_none_until_the_clip_is_old_enough(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    clip_id = manifest.start("หัวข้อ")
    manifest.add_snapshot(clip_id, {"date": "2026-08-28", "age_days": 2, "percent": 90.0})
    assert manifest.day7(manifest.load(clip_id)) is None


def test_the_newest_clips_are_the_ones_that_get_measured(tmp_path, monkeypatch):
    """The id filter has a cap; at 3 clips a day the window outgrows it."""
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(analytics, "MAX_VIDEOS", 2)
    for day, video_id in enumerate(["old", "middle", "newest"], start=1):
        clip_id = manifest.start("หัวข้อ")
        manifest.update(clip_id, video_id=video_id, published=True,
                        published_at=f"2026-08-0{day}T10:00:00")

    asked = []

    async def fake_rows(client, ids, locale="th"):
        asked.extend(ids)
        return []

    monkeypatch.setattr(snapshots, "_rows", fake_rows)
    monkeypatch.setattr(snapshots.youtube, "configured", lambda locale="th": True)
    import datetime as dt
    monkeypatch.setattr(snapshots, "MAX_AGE_DAYS", 3650)
    asyncio.run(snapshots.run())
    assert asked == ["newest", "middle"], "the oldest clip is the one to drop"


def test_a_failed_snapshot_does_not_retry_every_tick(monkeypatch):
    """`due()` runs on every poll tick; a dead credential must not be hammered."""
    async def explode():
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(snapshots, "run", explode)
    monkeypatch.setattr(main, "save_state", lambda state: None)

    state = {}
    asyncio.run(main.take_snapshots(None, state))
    assert state["last_snapshot"], "the day is stamped even when the pull failed"
    assert not snapshots.due(state)


# --- backfill ----------------------------------------------------------------

SRT_SAMPLE = """1
00:00:00,000 --> 00:00:04,807
เคยมั้ย เปิดดูซีรีย์จีนแนวตั้งแค่ตอนเดียว

2
00:00:04,807 --> 00:01:05,250
ซีรีย์จีนแนวตั้ง หรือ Short Vertical Drama
"""


def test_cards_are_read_back_out_of_the_subtitles():
    cards = backfill.cards_from_srt(SRT_SAMPLE)
    assert [c["start"] for c in cards] == [0.0, 4.807]
    assert cards[1]["seconds"] == pytest.approx(60.443, abs=0.001)
    assert cards[0]["narration"].startswith("เคยมั้ย")


def test_old_clips_get_a_manifest_marked_reconstructed(tmp_path, monkeypatch):
    """Their scripts died with the workdir; the boundaries survive in /output."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "clip.txt").write_text("ทำไมซีรีย์จีนแนวตั้ง\n\nคำอธิบาย\n", encoding="utf-8")
    (out / "clip.srt").write_text(SRT_SAMPLE, encoding="utf-8")

    monkeypatch.setattr(backfill, "OUTPUT_DIR", out)
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(history, "PATH", tmp_path / "history.json")
    history.PATH.write_text(json.dumps([
        {"video_id": "vid1", "title": "ทำไมซีรีย์จีนแนวตั้ง", "topic": "",
         "uploaded_at": "2026-08-26T20:29:14"},
        {"video_id": "vid2", "title": "คลิปที่ไฟล์หายไปแล้ว", "topic": "",
         "uploaded_at": "2026-08-26T21:39:09"},
    ], ensure_ascii=False), encoding="utf-8")

    assert backfill.run() == 2
    assert backfill.run() == 0, "backfill must be idempotent"

    with_srt = manifest.by_video("vid1")
    assert with_srt["reconstructed"] is True and with_srt["published"] is True
    assert len(with_srt["render"]["cards"]) == 2
    assert with_srt["published_at"] == "2026-08-26T20:29:14"
    # a clip whose files are gone still gets a record, just an emptier one
    assert manifest.by_video("vid2")["render"]["cards"] == []


# --- the gate ----------------------------------------------------------------

def test_nothing_is_fed_back_into_the_prompt_before_the_gate(monkeypatch):
    """One clip holds 88% of this channel's views — see docs/adr/0004."""
    monkeypatch.setattr(history, "video_ids", lambda locale=None: ["a"] * 9)

    def explode(locale="th"):
        raise AssertionError("performance() must not even be called before the gate")

    monkeypatch.setattr(analytics, "performance", explode)
    assert asyncio.run(analytics.winning_examples()) == []
    assert "9/30" in analytics.gate_note()


def test_past_the_gate_the_winners_come_back(monkeypatch):
    monkeypatch.setattr(history, "video_ids", lambda locale=None: ["a"] * 30)
    monkeypatch.setattr(history, "title_of", lambda v: "ชนะ")

    async def rows(locale="th"):
        return [{"title": "ชนะ", "views": 12}, {"title": "แพ้", "views": 0}]

    monkeypatch.setattr(analytics, "performance", rows)
    assert analytics.gate_note() is None
    assert asyncio.run(analytics.winning_examples()) == ["ชนะ"]


def test_the_report_says_when_it_cannot_be_trusted(monkeypatch):
    monkeypatch.setattr(history, "video_ids", lambda locale=None: ["a"] * 9)
    body = analytics.format_report([{"video_id": "a", "title": "t", "views": 3,
                                     "seconds": 20, "percent": 55}])
    assert "ยังไม่พอสรุป" in body
    assert "55%" in body


# --- history -----------------------------------------------------------------

def test_history_records_and_reads_back(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "PATH", tmp_path / "history.json")
    assert history.recent_titles() == []

    history.record("abc123", {"title": "เรื่องแรก"}, "หัวข้อแรก")
    history.record("def456", {"title": "เรื่องสอง"}, "หัวข้อสอง")

    assert history.recent_titles() == ["เรื่องแรก", "เรื่องสอง"]
    assert history.video_ids() == ["abc123", "def456"]
    assert history.title_of("def456") == "เรื่องสอง"
    assert history.title_of("nope") == "nope"


def test_history_survives_a_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(history, "PATH", path)
    assert history.load() == []


# --- prompt priming ----------------------------------------------------------

def test_past_titles_and_winners_reach_the_prompt():
    note = script_gen._context_note(["เรื่องเก่า"], ["เรื่องที่ปัง"])
    assert "เรื่องเก่า" in note and "ห้ามเขียนซ้ำ" in note
    assert "เรื่องที่ปัง" in note


def test_no_history_means_no_extra_prompt():
    assert script_gen._context_note([], []) == ""


def test_empty_history_reports_plainly():
    assert "ยังไม่มีสถิติ" in analytics.format_report([])


def test_empty_report_explains_the_lag_when_known():
    """An empty result should read as 'not yet', not as 'broken'."""
    text = analytics.format_report([], as_of="2026-08-22")
    assert "2026-08-22" in text


def test_report_sorts_by_retention():
    rows = [
        {"video_id": "a", "title": "A", "views": 10, "seconds": 20, "percent": 80},
        {"video_id": "b", "title": "B", "views": 99, "seconds": 5, "percent": 20},
    ]
    text = analytics.format_report(rows)
    assert text.index("A") < text.index("B")


def test_first_frame_is_a_thumbnail_sized_jpeg(tmp_path):
    """The cover is the opening frame, and small enough for YouTube's 2MB cap."""
    from PIL import Image

    png = render.draw_card(a_card("ปกคลิป"), tmp_path / "c.png")
    clip = render._segment(png, 1.0, tmp_path / "s.mp4")
    cover = render.first_frame(clip, tmp_path / "cover.jpg")

    with Image.open(cover) as img:
        assert img.format == "JPEG"
        assert img.size == (render.W, render.H)
    assert 0 < cover.stat().st_size < 2 * 1024 * 1024


def test_upload_needs_all_three_credentials(monkeypatch):
    for name in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "x")
    assert youtube.configured()
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "")
    assert not youtube.configured()


def test_upload_refuses_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "")
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    with pytest.raises(youtube.UploadError):
        asyncio.run(youtube.upload(clip, a_script()))


def test_metadata_strips_hashes_into_tags(monkeypatch):
    monkeypatch.setenv("YOUTUBE_PRIVACY", "public")
    script = a_script()
    script["hashtags"] = ["#DevOps", "#AIOps"]
    body = youtube.metadata(script)
    assert body["snippet"]["tags"] == ["DevOps", "AIOps"]
    assert "#DevOps #AIOps" in body["snippet"]["description"]
    assert body["status"]["privacyStatus"] == "public"
    assert body["status"]["selfDeclaredMadeForKids"] is False


def test_metadata_respects_youtube_limits():
    script = a_script()
    script["title"] = "ก" * 300
    script["description"] = "ข" * 9000
    body = youtube.metadata(script)
    assert len(body["snippet"]["title"]) == youtube.MAX_TITLE
    assert len(body["snippet"]["description"]) <= youtube.MAX_DESCRIPTION


def test_internal_full_stop_would_split_a_card():
    assert render._speakable("แบบนี้. แล้วก็แบบนั้น") == "แบบนี้, แล้วก็แบบนั้น"


def test_a_hyphen_is_read_as_a_pause_so_it_is_dropped():
    # "เอฟ-สามสิบห้า" came out as "เอฟ", a second of silence, then "สามสิบห้า".
    assert render._speakable("เอฟ-สามสิบห้า บินเร็ว") == "เอฟสามสิบห้า บินเร็ว"
    # A spaced dash separates clauses; it keeps a breath, as a comma.
    assert render._speakable("อันนี้ — สำคัญมาก") == "อันนี้, สำคัญมาก"


def test_a_tapped_suggestion_only_counts_for_the_list_it_came_from():
    """Two /trends runs = two live keyboards; an index alone points anywhere."""
    import datetime as dt

    topics = [{"topic": "ทองผันผวนเพราะอะไร"}, {"topic": "เกม Roblox ตัวนี้คืออะไร"}]
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    state = {"suggested": topics, "suggested_at": stamp}
    keyboard = main.topics_keyboard(topics, stamp)
    buttons = keyboard["inline_keyboard"][0]

    assert [b["text"] for b in buttons] == ["1", "2"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)
    assert main.picked(state, buttons[1]["callback_data"]) == "เกม Roblox ตัวนี้คืออะไร"
    # the same taps against a newer list are refused, not silently re-indexed
    newer = {"suggested": [{"topic": "อย่างอื่น"}], "suggested_at": "2026-08-28T09:00:00"}
    assert main.picked(newer, buttons[0]["callback_data"]) is None
    assert main.picked(state, "pick::0") is None
    assert main.picked(state, f"pick:{stamp}:9") is None
    # past SUGGESTION_LIFETIME the origin can no longer be credited, so the tap
    # must be refused rather than writing a Clip with no trend field
    old_stamp = (dt.datetime.now() - dt.timedelta(days=3)).isoformat(timespec="seconds")
    assert main.picked({"suggested": topics, "suggested_at": old_stamp},
                       f"pick:{old_stamp}:0") is None


def test_a_tapped_topic_keeps_its_trend_origin():
    """The pick shortcut must not lose the attribution /trends exists to collect."""
    import datetime as dt

    stamp = dt.datetime.now().isoformat(timespec="seconds")
    state = {"suggested": [{"topic": "ทองผันผวนเพราะอะไร", "from": "ทองเปิดบวก 50 บาท",
                            "kind": "evergreen", "category": "การเงิน"}],
             "suggested_at": stamp}
    topic = main.picked(state, f"pick:{stamp}:0")
    assert main.trend_origin(state, topic)["from"] == "ทองเปิดบวก 50 บาท"


def test_only_the_newest_passed_slot_is_owed(monkeypatch):
    """A bot that was down all day comes back and runs /trends once, not three times."""
    import datetime as dt
    from app import schedule

    monkeypatch.setattr(schedule, "settings", lambda: {
        "th": {"enabled": True, "hours": [8, 12, 17], "auto_pick_minutes": 15},
        "en": {"enabled": False, "hours": [20], "auto_pick_minutes": 15},
    })
    early = dt.datetime(2026, 8, 28, 7, 0)
    assert main.auto_slots({}, early) == []

    late = dt.datetime(2026, 8, 28, 23, 0)
    assert main.auto_slots({}, late) == [("th", "2026-08-28T17")]
    # stamped: the same tick 30 seconds later must not start a second run
    assert main.auto_slots({"last_auto_trends": {"th": "2026-08-28T17"}}, late) == []
    # yesterday's stamp does not satisfy today's slot
    assert main.auto_slots({"last_auto_trends": {"th": "2026-08-27T17"}}, late)


def test_a_locale_is_owed_a_round_only_when_it_is_switched_on(monkeypatch):
    """English publishes to a channel unattended, so it is off until asked for."""
    import datetime as dt
    from app import schedule

    monkeypatch.setattr(schedule, "settings", lambda: {
        "th": {"enabled": True, "hours": [8], "auto_pick_minutes": 15},
        "en": {"enabled": True, "hours": [8], "auto_pick_minutes": 40},
    })
    late = dt.datetime(2026, 8, 28, 9, 0)
    # Both due at once is allowed to happen; the loop takes one at a time and
    # the other stays owed. Each carries its own stamp, so one running does not
    # mark the other as done.
    assert main.auto_slots({}, late) == [("en", "2026-08-28T08"), ("th", "2026-08-28T08")]
    owed = main.auto_slots({"last_auto_trends": {"en": "2026-08-28T08"}}, late)
    assert owed == [("th", "2026-08-28T08")]


def test_an_old_bare_stamp_belongs_to_thai_only(monkeypatch):
    """`last_auto_trends` was one string from when only Thai ran unattended.

    Read as a map it would satisfy nothing and Thai's round would fire twice on
    the first tick after the upgrade.
    """
    import datetime as dt
    from app import schedule

    monkeypatch.setattr(schedule, "settings", lambda: {
        "th": {"enabled": True, "hours": [8], "auto_pick_minutes": 15},
        "en": {"enabled": True, "hours": [8], "auto_pick_minutes": 15},
    })
    late = dt.datetime(2026, 8, 28, 9, 0)
    assert main.auto_slots({"last_auto_trends": "2026-08-28T08"}, late) == [
        ("en", "2026-08-28T08")
    ]


def test_the_automatic_pick_waits_for_its_deadline():
    import datetime as dt

    now = dt.datetime(2026, 8, 28, 8, 10)
    assert main.auto_pick_due({}, now) is False
    assert main.auto_pick_due({"auto_pick": {"deadline": "2026-08-28T08:15:00"}}, now) is False
    assert main.auto_pick_due({"auto_pick": {"deadline": "2026-08-28T08:05:00"}}, now) is True
    # a state written by an older build, or half-written, is not a deadline
    assert main.auto_pick_due({"auto_pick": {}}, now) is False


def test_a_timeout_does_not_bury_the_first_failure(monkeypatch):
    """Reported 2026-08-29: 'mimo ไม่ตอบภายใน 600 วินาที' when mimo had in fact
    answered — the first attempt came back in 343s and failed validation, and
    the retry inherited too little of the shared budget to finish. Overwriting
    last_error with the timeout hid the schema slip that started it."""
    monkeypatch.setattr(script_gen, "BUDGET_SECONDS", 0.4)
    monkeypatch.setattr(script_gen, "MIN_ATTEMPT", 0.1)
    monkeypatch.setattr(script_gen, "HEDGE_AFTER", 0.05)
    monkeypatch.setattr(script_gen, "_client", lambda: fake_client(["ไม่ใช่ JSON", 30, 30]))

    with pytest.raises(script_gen.ScriptError) as caught:
        asyncio.run(script_gen.generate("หัวข้อ"))
    assert "ไม่ตอบภายใน" in str(caught.value)
    assert "รอบก่อนหน้า" in str(caught.value)


def test_the_schema_retry_leads_with_the_smaller_model(monkeypatch):
    """The retry inherits only what the first attempt left of the shared
    budget, which can be less than a pro-model think takes. Correcting JSON
    against a schema it has already been shown does not need the pro model."""
    good = json.dumps({
        "title": "t", "description": "d", "hashtags": ["#x"], "category": "เทค",
        "cards": [a_card() for _ in range(5)],
    })
    client = fake_client(["ไม่ใช่ JSON", good])
    monkeypatch.setattr(script_gen, "_client", lambda: client)

    assert asyncio.run(script_gen.generate("หัวข้อ"))["title"] == "t"
    assert client.asked == [script_gen.PRIMARY_MODEL, script_gen.FALLBACK_MODEL]


def test_an_unparseable_reply_does_not_pollute_the_retry(monkeypatch):
    """Observed 2026-09-04: attempt 0 (pro model) returned 60 chars of
    non-JSON after 167s. generate() appended that garbage to `messages` as an
    assistant turn plus a "ส่ง JSON ใหม่" correction, then sent it to the
    weaker fallback model, which replied with a 496-char fragment missing
    `title`. The two-attempt cap then raised ScriptError with ~420s of the
    600s budget unused — but the same prompt succeeds on a plain retry, so an
    unparseable reply must be retried with `messages` unchanged instead."""
    good = json.dumps({
        "title": "t", "description": "d", "hashtags": ["#x"], "category": "เทค",
        "cards": [a_card() for _ in range(5)],
    })
    replies = iter(["ก" * 60, good])
    seen_messages = []

    async def fake_say(client, messages, temperature, budget, models=None):
        seen_messages.append(list(messages))
        return next(replies)

    monkeypatch.setattr(script_gen, "_say", fake_say)

    result = asyncio.run(script_gen.generate("หัวข้อ"))
    assert result["title"] == "t"
    assert seen_messages[1] == seen_messages[0], (
        "the garbage reply must not be appended before the retry"
    )


def test_attempts_are_bounded_by_the_deadline_not_a_fixed_count(monkeypatch):
    """Before this fix, `for attempt in range(2):` gave up after exactly two
    tries even when most of BUDGET_SECONDS was still unspent. Every attempt
    below returns unparseable garbage, so the only thing that can stop the
    loop is running out of budget — proving it is deadline-driven rather than
    capped at two."""
    monkeypatch.setattr(script_gen, "BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(script_gen, "MIN_ATTEMPT", 0.05)
    calls = []

    async def fake_say(client, messages, temperature, budget, models=None):
        calls.append(models)
        await asyncio.sleep(0.06)
        return "ก" * 60  # never parses as JSON

    monkeypatch.setattr(script_gen, "_say", fake_say)

    with pytest.raises(script_gen.ScriptError):
        asyncio.run(script_gen.generate("หัวข้อ"))
    assert len(calls) > 2, "budget allowed more than the old hard count of 2"


# --- footage the human generates in Flow (docs/adr/0005) ---------------------

def _parked_state(tmp_path, clip_id="clip-1"):
    return {
        "mode": "idle",
        "parked": {
            "clip_id": clip_id, "topic": "หัวข้อ", "script": a_script(),
            "style": "", "card": 0, "prompt": "a slow push in on rain",
            "prompt_message_id": 7,
            "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        },
    }


def test_flow_parks_the_clip_and_frees_the_bot(monkeypatch, tmp_path):
    """🎨 hands over a prompt and lets go: the bot must be idle afterwards, or
    the human cannot use it while they are off generating in Flow."""
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main, "close_prompt", _nothing)
    sent = []

    async def fake_say(client, text, **extra):
        sent.append(text)
        return {"message_id": 42}

    monkeypatch.setattr(main, "say", fake_say)

    async def fake_prompt(topic, card):
        return "slow push in on a flooded street at dusk"

    monkeypatch.setattr(script_gen, "flow_prompt", fake_prompt)

    clip_id = manifest.start("หัวข้อ")
    state = {"mode": "review", "script": a_script(), "topic": "หัวข้อ",
             "clip_id": clip_id, "message_id": 5}
    asyncio.run(main.on_flow(None, state))

    assert state["mode"] == "idle" and state["script"] is None
    parked = state["parked"]
    assert parked["clip_id"] == clip_id and parked["prompt_message_id"] == 42
    assert "slow push in" in sent[-1], "the prompt must be in the message it is replied to"
    assert manifest.load_all()[0]["flow_prompt"].startswith("slow push")


def test_a_failed_flow_prompt_keeps_the_script(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main, "close_prompt", _nothing)
    monkeypatch.setattr(main, "say", _nothing)

    async def boom(topic, card):
        raise script_gen.ScriptError("mimo ไม่ตอบ")

    monkeypatch.setattr(script_gen, "flow_prompt", boom)

    state = {"mode": "review", "script": a_script(), "topic": "หัวข้อ", "clip_id": None}
    asyncio.run(main.on_flow(None, state))
    assert state["mode"] == "review" and state["script"] is not None
    assert "parked" not in state


def test_footage_must_reply_to_the_prompt_message(monkeypatch, tmp_path):
    """A file matched to the wrong Card renders a clip that is about something
    else and looks fine, so an unmatched file is refused rather than guessed."""
    monkeypatch.setattr(main, "save_state", lambda state: None)
    said = []
    monkeypatch.setattr(main, "say", lambda client, text, **extra: said.append(text) or _nothing())

    async def never(*args, **kwargs):
        raise AssertionError("must not download an unmatched file")

    monkeypatch.setattr(main, "download_footage", never)

    state = _parked_state(tmp_path)
    asyncio.run(main.on_footage(None, state, {"video": {"file_id": "f1"}}))
    asyncio.run(main.on_footage(
        None, state,
        {"video": {"file_id": "f1"}, "reply_to_message": {"message_id": 999}},
    ))
    assert len(said) == 2 and "parked" in state and "footage" not in state["parked"]


def test_oversize_footage_is_refused_with_a_way_out(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "save_state", lambda state: None)
    said = []
    monkeypatch.setattr(main, "say", lambda client, text, **extra: said.append(text) or _nothing())
    state = _parked_state(tmp_path)
    asyncio.run(main.on_footage(None, state, {
        "document": {"file_id": "f1", "file_size": main.TELEGRAM_FILE_LIMIT + 1},
        "reply_to_message": {"message_id": 7},
    }))
    assert "20MB" in said[0] and "footage" not in state["parked"]


def test_replied_footage_is_filed_under_the_clip(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main, "FOOTAGE_DIR", tmp_path / "footage")
    monkeypatch.setattr(main, "say", _nothing)

    async def fake_download(client, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4")
        return True

    monkeypatch.setattr(main, "download_footage", fake_download)

    state = _parked_state(tmp_path)
    asyncio.run(main.on_footage(None, state, {
        "video": {"file_id": "f1", "file_size": 5_000_000},
        "reply_to_message": {"message_id": 7},
    }))
    stored = state["parked"]["footage"]["0"]
    assert stored.endswith("footage/clip-1/c00.mp4") and pathlib.Path(stored).is_file()


def test_render_parked_hands_the_file_to_the_renderer(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main, "say", _nothing)
    seen = {}

    async def fake_render(client, state, supplied=None):
        seen["supplied"] = supplied
        seen["script"] = state["script"]

    monkeypatch.setattr(main, "do_render", fake_render)

    state = _parked_state(tmp_path)
    state["parked"]["footage"] = {"0": str(tmp_path / "c00.mp4")}
    asyncio.run(main.render_parked(None, state))

    assert seen["supplied"] == {0: pathlib.Path(tmp_path / "c00.mp4")}
    assert "parked" not in state and seen["script"] is not None


def test_a_script_under_review_is_not_clobbered_by_the_parked_one(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "save_state", lambda state: None)
    said = []
    monkeypatch.setattr(main, "say", lambda client, text, **extra: said.append(text) or _nothing())

    async def never(*args, **kwargs):
        raise AssertionError("must not render while a Script waits for review")

    monkeypatch.setattr(main, "do_render", never)

    state = _parked_state(tmp_path)
    state.update(mode="review", script=a_script())
    asyncio.run(main.render_parked(None, state))
    assert "parked" in state and said


def test_parked_clip_expires_and_is_written_off(monkeypatch, tmp_path):
    import datetime as dt

    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(main, "say", _nothing)

    clip_id = manifest.start("หัวข้อ")
    state = _parked_state(tmp_path, clip_id)
    assert not main.parked_expired(state)

    state["parked"]["created_at"] = (
        dt.datetime.now() - main.PARK_LIFETIME - dt.timedelta(minutes=1)
    ).isoformat(timespec="seconds")
    assert main.parked_expired(state)

    asyncio.run(main.drop_parked(None, state.pop("parked")))
    assert manifest.load_all()[0]["outcome"] == "abandoned"


def test_auto_pick_stands_down_while_a_clip_is_parked(monkeypatch, tmp_path):
    """The human is busy generating Footage; an unattended clip would land on
    top of the one they are working on — and the round must be *dropped*, not
    held until the parked clip clears and fired off a stale /trends list."""
    import datetime as dt

    monkeypatch.setattr(main, "save_state", lambda state: None)
    overdue = {"deadline": (dt.datetime.now() - dt.timedelta(minutes=1)).isoformat()}

    free = {"mode": "idle", "auto_pick": dict(overdue)}
    assert main.take_auto_pick(free) and "auto_pick" not in free

    parked = {**_parked_state(tmp_path), "auto_pick": dict(overdue)}
    assert not main.take_auto_pick(parked)
    assert "auto_pick" not in parked, "a skipped round must not fire later"

    busy = {"mode": "rendering", "auto_pick": dict(overdue)}
    assert not main.take_auto_pick(busy) and "auto_pick" not in busy


# --- storyboards for Google Flow (docs/adr/0006) -----------------------------

TAG = "the same 25-year-old Thai woman with long dark hair and a white shirt"


def a_board(scenes: int = 5, ratio: str = "9:16", character: bool = True) -> dict:
    return {
        "overview": {
            "title": "บ้านที่ใช่",
            "mood_tone_progression": "อบอุ่นขึ้นเรื่อยๆ",
            "target_audience": "คนวัยทำงาน",
            "master_character": {
                "name": "มิ้นท์", "age": 25, "ethnicity": "Thai",
                "appearance": "long dark hair", "outfit": "white shirt",
                "locked_prompt_tag": TAG,
            } if character else None,
        },
        "scenes": [
            {"scene_number": i, "camera": "Medium Shot",
             "scene_description": f"เหตุการณ์ฉาก {i}", "visual_details": "แสงเช้า",
             "sound_verbatim": "เสียงพากย์", "on_screen_text": "ข้อความ",
             "scene_mood_note": "อบอุ่น",
             "image_gen_prompt": (
                 f"A cinematic {ratio} shot of {TAG}, standing in a garden, "
                 f"golden hour, clean center composition for text overlay, "
                 f"photorealistic, {storyboard.NEGATIVES}"
             ),
             "motion": "slow push in as she turns to the camera"}
            for i in range(1, scenes + 1)
        ],
    }


def test_every_scene_must_repeat_the_locked_character(monkeypatch):
    """A paraphrased character tag in one scene is a different face in that
    scene — the whole reason the tag exists."""
    board = a_board()
    board["scenes"][2]["image_gen_prompt"] = board["scenes"][2]["image_gen_prompt"].replace(
        TAG, "a young Thai woman")
    with pytest.raises(script_gen.ScriptError, match="locked_prompt_tag"):
        storyboard.validate(board, "9:16")

    # ...and a storyboard with no character at all is fine
    assert storyboard.validate(a_board(character=False), "9:16")


def test_image_prompts_must_be_english_and_carry_the_rules():
    for mutate, complaint in (
        (lambda b: b["scenes"][0].update(image_gen_prompt="ภาพผู้หญิงยืนในสวน"), "อังกฤษ"),
        (lambda b: b["scenes"][0].update(motion="ค่อยๆ ซูมเข้า"), "อังกฤษ"),
        (lambda b: b["scenes"][0].update(
            image_gen_prompt=f"A cinematic shot of {TAG}, {storyboard.NEGATIVES}"), "9:16"),
        (lambda b: b["scenes"][0].update(
            image_gen_prompt=f"A cinematic 9:16 shot of {TAG}, garden"), "no text"),
        (lambda b: b["scenes"][0].pop("motion"), "motion"),
        (lambda b: b["overview"].pop("target_audience"), "target_audience"),
    ):
        board = a_board()
        mutate(board)
        with pytest.raises(script_gen.ScriptError, match=complaint):
            storyboard.validate(board, "9:16")


def test_scene_count_must_match_the_script():
    with pytest.raises(script_gen.ScriptError, match="ตรงกับสคริปต์"):
        storyboard.validate(a_board(scenes=4), "9:16", scenes_wanted=5)


def test_the_script_owns_the_words_not_the_model():
    """What is spoken and what is drawn are already decided; a storyboard that
    paraphrases them is a set of images for a video that does not exist."""
    script = a_script(cards=3)
    script["cards"][1]["narration"] = "ประโยคจริงของการ์ดที่สอง"
    script["cards"][1]["lines"] = ["บรรทัดจริง"]
    board = a_board(scenes=3)
    board["scenes"][1]["sound_verbatim"] = "โมเดลเขียนเองมั่วๆ"

    locked = storyboard.lock_to_script(board, script["cards"])
    assert locked["scenes"][1]["sound_verbatim"] == "ประโยคจริงของการ์ดที่สอง"
    assert locked["scenes"][1]["on_screen_text"] == "บรรทัดจริง"


def test_messages_lead_with_the_character_then_one_per_scene():
    board = storyboard.validate(a_board(scenes=4), "9:16")
    messages = storyboard.messages(board)

    assert len(messages) == 1 + 1 + 4, "overview, character, then a message per scene"
    assert "ingredient" in messages[1]["heading"]
    assert TAG in messages[1]["blocks"][0][1]
    labels = [label for label, _ in messages[2]["blocks"]]
    assert len(labels) == 2, "one block to make the image, one to make the video"
    assert messages[2]["blocks"][1][1].endswith("slow push in as she turns to the camera")

    # long-form has neither: no character to lock, and no on-screen text unless
    # the story calls for one — the message must survive the key being absent
    long_board = a_board(scenes=4, character=False, ratio="16:9")
    for scene in long_board["scenes"]:
        scene.pop("on_screen_text")
    long_messages = storyboard.messages(storyboard.validate(long_board, "16:9"))
    assert len(long_messages) == 1 + 4
    assert "ข้อความบนจอ" not in long_messages[1]["heading"]


def test_storyboard_button_leaves_the_script_in_review(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(main, "say", _nothing)
    sent = []

    async def fake_send(client, board):
        sent.append(board)

    monkeypatch.setattr(main, "send_storyboard", fake_send)

    async def fake_for_script(script):
        return storyboard.validate(a_board(scenes=len(script["cards"])), "9:16")

    monkeypatch.setattr(storyboard, "for_script", fake_for_script)

    clip_id = manifest.start("หัวข้อ")
    state = {"mode": "review", "script": a_script(), "clip_id": clip_id, "message_id": 5}
    asyncio.run(main.on_storyboard(None, state))

    assert state["mode"] == "review" and state["message_id"] == 5
    assert state["script"] is not None, "the button must not consume the Script"
    assert sent and manifest.load_all()[0]["storyboard"]["overview"]["title"] == "บ้านที่ใช่"


def test_a_storyboard_is_credited_to_the_clip_it_was_asked_for(monkeypatch, tmp_path):
    """The model call takes minutes and the human may start another Topic
    meanwhile; the storyboard must not land on the new clip's manifest."""
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "send_storyboard", _nothing)

    first = manifest.start("หัวข้อ A")
    second = manifest.start("หัวข้อ B")
    state = {"mode": "review", "script": a_script(), "clip_id": first}

    async def slow_for_script(script):
        state["clip_id"] = second      # the human moved on while this ran
        return storyboard.validate(a_board(scenes=len(script["cards"])), "9:16")

    monkeypatch.setattr(storyboard, "for_script", slow_for_script)
    asyncio.run(main.on_storyboard(None, state))

    records = {r["id"]: r for r in manifest.load_all()}
    assert "storyboard" in records[first] and "storyboard" not in records[second]


def test_a_scene_message_carries_the_thai_and_the_english(monkeypatch):
    posted = {}

    async def fake_api(client, method, **payload):
        posted.update(payload)
        return {}

    monkeypatch.setattr(main, "api", fake_api)
    asyncio.run(main.send_prompt(None, "หัวเรื่อง", [("ภาพ", "A cinematic 9:16 shot")]))

    assert posted["parse_mode"] == "HTML"
    assert "หัวเรื่อง" in posted["text"] and "<pre>A cinematic 9:16 shot</pre>" in posted["text"]


# --- Locales (English clips) -------------------------------------------------

def an_english_card(text: str = "Docker logs eat disk") -> dict:
    return {
        "lines": [text],
        "code": None,
        "query": "server room racks",
        "narration": "Your Docker logs are eating the whole disk.",
        "spoken": "Your Docker logs are eating the whole disk.",
    }


def an_english_script(cards: int = 5) -> dict:
    return {
        "title": "Docker logs",
        "description": "why they grow",
        "hashtags": ["#devops"],
        "category": "tech",
        "cards": [an_english_card() for _ in range(cards)],
    }


def test_an_english_script_passes_the_english_rules():
    assert script_gen.validate(an_english_script(), "en")["cards"]


def test_the_latin_rule_is_mirrored_not_shared():
    """Thai `spoken` may not contain Latin; English `spoken` may not contain
    Thai. Applying either rule to the other Locale rejects every script."""
    with pytest.raises(script_gen.ScriptError, match="ละติน"):
        english = an_english_script()
        script_gen.validate(english, "th")

    thai = a_script()
    with pytest.raises(script_gen.ScriptError, match="ไทย"):
        script_gen.validate(thai, "en")


def test_a_latin_line_that_clears_the_pixel_floor_is_still_rejected():
    """Latin runs ~50px a character at full size against Thai's ~21, so a line
    the renderer *can* draw at its 40px minimum is one nobody can read on a
    phone. For English the character count is the binding rule."""
    line = "This line is far too long to read"   # 33 characters
    assert len(line) > script_gen.HARD_MAX_CHARS_PER_LINE - 2
    assert script_gen._too_wide(line, "th") == 0, "the pixel floor lets it through"
    assert script_gen._too_wide(line, "en") > 0, "the count must not"


def test_the_english_voice_reads_an_english_clip(monkeypatch, tmp_path):
    seen = {}

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
            seen.update(text=text, voice=voice)

        async def save(self, path):
            pathlib.Path(path).write_bytes(b"")

    monkeypatch.setattr(render.edge_tts, "Communicate", FakeCommunicate)
    asyncio.run(render.speak("hello", tmp_path / "a.mp3", "en"))
    assert seen["voice"] == "en-US-AndrewNeural"
    asyncio.run(render.speak("ทดสอบ", tmp_path / "b.mp3"))
    assert seen["voice"] == "th-TH-NiwatNeural", "Thai must keep its own voice"


def test_a_hyphen_joins_english_words_and_disappears_in_thai():
    """The Thai voice pauses on a hyphen and the transliteration does not need
    it; English "state-of-the-art" without one is a word nobody can say."""
    assert render._speakable("state-of-the-art", "en") == "state of the art"
    assert render._speakable("เอฟ-สามสิบห้า", "th") == "เอฟสามสิบห้า"


def test_an_english_clip_lands_in_its_own_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "send_video", _nothing)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main.youtube, "configured", lambda locale="th": False)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    state = {"locale": "en"}
    asyncio.run(main.deliver(None, state, an_english_script(), clip))
    assert list((tmp_path / "en").glob("*.mp4")), "English clips go to /output/en"
    assert state["last_locale"] == "en"

    state = {}
    asyncio.run(main.deliver(None, state, a_script(), clip))
    assert list(tmp_path.glob("*.mp4")), "Thai clips stay where they always were"
    assert state["last_locale"] == "th"


def test_a_parked_clip_keeps_its_own_locale(monkeypatch):
    """A Parked Clip waits while the bot goes idle, so another Topic — in the
    other Locale — can be started and finished before its Footage arrives."""
    rendered = {}

    async def fake_render(client, state, supplied=None):
        rendered.update(locale=state.get("locale"))

    monkeypatch.setattr(main, "do_render", fake_render)
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "save_state", lambda state: None)

    state = {
        "mode": "idle",
        # the live slots belong to a Thai clip started while the English one waits
        "locale": "th",
        "parked": {"clip_id": "x", "topic": "t", "script": an_english_script(),
                   "locale": "en", "card": 0},
    }
    asyncio.run(main.render_parked(None, state))
    assert rendered["locale"] == "en"


def test_an_english_result_topic_is_turned_away(monkeypatch):
    sent = []

    async def fake_say(client, text, **kw):
        sent.append(text)

    async def never(*a, **kw):
        raise AssertionError("a result topic reached the model")

    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main.script_gen, "generate", never)
    state = {"mode": "idle"}
    asyncio.run(main.make_script(None, state, "who won the game last night", locale="en"))
    assert sent and "ไม่รู้ผลแข่ง" in sent[0]
    assert state == {"mode": "idle"}


def test_an_english_clip_is_written_in_english_and_learns_from_nothing_thai(monkeypatch):
    """The prompt must be the English one, and the Thai channel's titles must
    not be fed to a model writing for a US audience."""
    seen = {}

    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main.manifest, "start", lambda topic, locale="th": "test-id")
    monkeypatch.setattr(main.manifest, "update", lambda *a, **kw: None)
    monkeypatch.setattr(main.manifest, "add_script", lambda *a, **kw: None)
    monkeypatch.setattr(main.history, "recent_titles",
                        lambda locale=None: ["คลิปไทยเก่า"] if locale == "th" else [])

    async def fake_generate(topic, previous=None, feedback="", avoid=None,
                            winners=None, style="", locale="th", sibling=None):
        seen.update(locale=locale, avoid=avoid, style=style)
        return an_english_script()

    monkeypatch.setattr(main.script_gen, "generate", fake_generate)
    state = {"mode": "idle"}
    asyncio.run(main.make_script(None, state, "docker logs", locale="en"))

    assert seen["locale"] == "en"
    assert seen["avoid"] == [], "the Thai channel's titles must not reach the English prompt"
    assert seen["style"] in list(experiment.VARIANTS_EN.values()) + [experiment.EXPLORE_CLAUSE_EN]
    assert state["locale"] == "en"


def test_the_english_prompt_is_written_in_english():
    """Asking for an English script in Thai gets a translation, not writing."""
    assert not script_gen.THAI.search(script_gen.system_prompt("en"))
    assert not script_gen.THAI.search(script_gen.trends_prompt("en"))
    assert script_gen.THAI.search(script_gen.system_prompt("th"))


def test_an_unknown_locale_falls_back_to_thai():
    """Manifests written before Locales existed carry no locale at all."""
    assert locales.get(None)["code"] == "th"
    assert locales.get("de")["code"] == "th"
    assert main.output_dir(None) == main.OUTPUT_DIR


def test_trends_asks_the_right_country(monkeypatch):
    asked = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            raise AssertionError("no network in tests")

    async def fake_searches(client, geo="TH"):
        asked.append(("rss", geo))
        return []

    async def fake_watching(client, region="TH"):
        asked.append(("yt", region))
        return []

    monkeypatch.setattr(trends.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setattr(trends, "searches", fake_searches)
    monkeypatch.setattr(trends, "watching", fake_watching)

    asyncio.run(trends.collect("en"))
    assert asked == [("rss", "US"), ("yt", "US")]
    asked.clear()
    asyncio.run(trends.collect())
    assert asked == [("rss", "TH"), ("yt", "TH")]


def test_an_english_clip_never_uploads_through_the_thai_channel(monkeypatch):
    """Two channels (docs/adr/0008), and no shared fallback: without English
    credentials the button must not appear, and the upload must refuse rather
    than reach for the Thai channel's refresh token."""
    for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
        monkeypatch.setenv(f"YOUTUBE_{name}", "thai-channel")
        monkeypatch.delenv(f"YOUTUBE_EN_{name}", raising=False)

    assert youtube.configured("th")
    assert not youtube.configured("en")
    with pytest.raises(youtube.UploadError, match="อังกฤษ"):
        asyncio.run(youtube.upload(pathlib.Path("/nonexistent.mp4"), an_english_script(), "en"))


def test_the_upload_uses_the_locale_the_clip_was_delivered_with(monkeypatch, tmp_path):
    seen = {}

    async def fake_upload(clip, script, locale=locales.DEFAULT):
        seen["upload"] = locale
        return "vid123", "public"

    async def fake_captions(video_id, srt, locale=locales.DEFAULT):
        seen["captions"] = locale

    monkeypatch.setattr(main.youtube, "upload", fake_upload)
    monkeypatch.setattr(main.youtube, "add_captions", fake_captions)
    monkeypatch.setattr(main.history, "record", lambda *a, **kw: seen.update(history=kw or a))
    monkeypatch.setattr(main.manifest, "update", lambda *a, **kw: None)
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "retire_buttons", _nothing)
    monkeypatch.setattr(main, "save_state", lambda state: None)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n")
    state = {"last_clip": str(clip), "last_srt": str(srt),
             "last_script": an_english_script(), "last_locale": "en"}
    asyncio.run(main.do_upload(None, state))

    assert seen["upload"] == "en"
    assert seen["captions"] == "en", "an English clip must not be tagged with Thai subtitles"


# --- Locales, batch 2: two channels, two sets of numbers ---------------------

def _published(locale: str, video_id: str, variant: str = "question",
               views: int = 100, percent: float = 50.0) -> dict:
    return {
        "id": video_id, "locale": locale, "video_id": video_id,
        "variant": variant, "outcome": "rendered", "published": True,
        "published_at": "2026-09-01T10:00:00",
        "snapshots": [{"date": "2026-09-08", "age_days": 7,
                       "views": views, "percent": percent}],
    }


def test_history_is_read_per_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "PATH", tmp_path / "history.json")
    history.record("th1", {"title": "คลิปไทย"}, "หัวข้อ")
    history.record("en1", {"title": "An English clip"}, "topic", "en")

    assert history.video_ids("th") == ["th1"]
    assert history.video_ids("en") == ["en1"]
    assert history.video_ids() == ["th1", "en1"], "no locale means every channel"
    assert history.recent_titles(locale="en") == ["An English clip"]


def test_an_entry_written_before_locales_belongs_to_thai(monkeypatch, tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps([{"video_id": "old", "title": "เก่า"}]), encoding="utf-8")
    monkeypatch.setattr(history, "PATH", path)
    assert history.video_ids("th") == ["old"]
    assert history.video_ids("en") == []


def test_each_channel_reaches_the_gate_on_its_own_count(monkeypatch, tmp_path):
    """Thirty clips split across two audiences is not thirty data points about
    either of them — see docs/adr/0008."""
    monkeypatch.setattr(history, "PATH", tmp_path / "history.json")
    for i in range(30):
        history.record(f"th{i}", {"title": f"ไทย {i}"}, "หัวข้อ")
    for i in range(3):
        history.record(f"en{i}", {"title": f"English {i}"}, "topic", "en")

    assert analytics.gate_note("th") is None, "the Thai channel is past the gate"
    assert "3/30" in analytics.gate_note("en"), "the English one is not"


def test_the_english_prompt_never_learns_from_the_thai_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "PATH", tmp_path / "history.json")
    for i in range(30):
        history.record(f"th{i}", {"title": f"ไทย {i}"}, "หัวข้อ")

    async def never(locale="th"):
        raise AssertionError("performance() must not be called before that channel's gate")

    monkeypatch.setattr(analytics, "performance", never)
    assert asyncio.run(analytics.winning_examples(locale="en")) == []


def test_the_experiment_is_counted_per_channel():
    records = [_published("th", "t1"), _published("th", "t2", views=7),
               _published("en", "e1", variant="shock_number", views=900)]

    thai = experiment.tally(experiment.for_locale(records, "th"))
    english = experiment.tally(experiment.for_locale(records, "en"))
    assert thai["question"]["clips"] == 2
    assert thai["shock_number"]["clips"] == 0, "the English clip is not Thai data"
    assert english["shock_number"]["views"] == 900
    assert "อังกฤษ" in experiment.report(records, "en")


def test_a_manifest_without_a_locale_is_counted_as_thai():
    records = [{"variant": "question", "outcome": "rendered", "snapshots": []}]
    assert experiment.for_locale(records, "th") == records
    assert experiment.for_locale(records, "en") == []


def test_each_channel_gets_its_own_snapshot_pull(tmp_path, monkeypatch):
    """One pull per channel: a video id the channel does not own comes back as
    no row at all, which would read as 'not processed yet'."""
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(snapshots, "MAX_AGE_DAYS", 3650)
    for locale, video_id in (("th", "thai-vid"), ("en", "english-vid")):
        clip_id = manifest.start("หัวข้อ", locale)
        manifest.update(clip_id, video_id=video_id, published=True,
                        published_at="2026-09-01T10:00:00")

    asked = []

    async def fake_rows(client, ids, locale="th"):
        asked.append((locale, ids))
        return []

    monkeypatch.setattr(snapshots, "_rows", fake_rows)
    monkeypatch.setattr(snapshots.youtube, "configured", lambda locale="th": True)
    asyncio.run(snapshots.run())
    assert sorted(asked) == [("en", ["english-vid"]), ("th", ["thai-vid"])]


def test_a_channel_with_no_credentials_is_skipped_not_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(snapshots, "MAX_AGE_DAYS", 3650)
    for locale, video_id in (("th", "thai-vid"), ("en", "english-vid")):
        clip_id = manifest.start("หัวข้อ", locale)
        manifest.update(clip_id, video_id=video_id, published=True,
                        published_at="2026-09-01T10:00:00")

    asked = []

    async def fake_rows(client, ids, locale="th"):
        asked.append(locale)
        return []

    monkeypatch.setattr(snapshots, "_rows", fake_rows)
    monkeypatch.setattr(snapshots.youtube, "configured", lambda locale="th": locale == "th")
    asyncio.run(snapshots.run())
    assert asked == ["th"]


def test_one_channel_failing_does_not_cost_the_other_its_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(snapshots, "MAX_AGE_DAYS", 3650)
    for locale, video_id in (("th", "thai-vid"), ("en", "english-vid")):
        clip_id = manifest.start("หัวข้อ", locale)
        manifest.update(clip_id, video_id=video_id, published=True,
                        published_at="2026-09-01T10:00:00")

    async def fake_rows(client, ids, locale="th"):
        if locale == "en":
            raise analytics.AnalyticsError("ช่องอังกฤษล่ม")
        return [["thai-vid", 5, 0, 0, 0, 0, 44.0, 9.0, 1.0]]

    monkeypatch.setattr(snapshots, "_rows", fake_rows)
    monkeypatch.setattr(snapshots.youtube, "configured", lambda locale="th": True)
    assert asyncio.run(snapshots.run()) == 1


def test_the_retention_curve_is_read_from_the_clips_own_channel(monkeypatch):
    asked = {}

    async def fake_fetch(video_id, client=None, locale="th"):
        asked[video_id] = locale
        raise retention.NoCurve("ยังไม่มีเส้น")

    monkeypatch.setattr(main.retention, "fetch", fake_fetch)
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main.manifest, "load_all", lambda: [
        dict(_published("en", "english-vid"),
             render={"seconds": 20.0, "cards": [{"start": 0.0, "narration": "x"}]}),
    ])
    asyncio.run(main.on_retention(None, "english-vid"))
    assert asked == {"english-vid": "en"}


# --- Locale pairs: one Topic, both channels ---------------------------------

def test_a_pair_starts_in_thai_and_remembers_the_topic(monkeypatch):
    calls = []

    async def fake_make_script(client, state, topic, **kw):
        calls.append((topic, kw.get("locale")))

    monkeypatch.setattr(main, "make_script", fake_make_script)
    monkeypatch.setattr(main, "say", _nothing)
    state = {"mode": "idle"}
    asyncio.run(main.start_pair(None, state, "ทำไม log บวม"))

    assert calls == [("ทำไม log บวม", "th")], "the human reviews Thai first"
    assert state["pair"]["topic"] == "ทำไม log บวม"


def test_the_english_half_is_written_from_the_thai_one_not_translated(monkeypatch):
    seen = {}

    async def fake_make_script(client, state, topic, **kw):
        seen.update(topic=topic, **kw)

    monkeypatch.setattr(main, "make_script", fake_make_script)
    monkeypatch.setattr(main, "say", _nothing)
    approved = a_script()
    state = {"pair": {"topic": "ทำไม log บวม", "pair_id": "clip-th"},
             "last_script": approved}
    asyncio.run(main.continue_pair(None, state))

    assert seen["locale"] == "en" and seen["topic"] == "ทำไม log บวม"
    assert seen["sibling"] == approved, "the approved Thai script is context"
    assert seen["pair_id"] == "clip-th"
    assert "pair" not in state, "the queue is consumed, not left to fire twice"


def test_the_sibling_note_asks_for_a_rewrite_not_a_translation():
    note = script_gen._sibling_note(a_script(), "en")
    assert "do not translate" in note.lower()
    assert "อ่านออกเสียงประโยคนี้" in note, "the other half's narration is the context"


def test_a_delivered_clip_queues_the_other_half(monkeypatch, tmp_path):
    spawned = []

    async def fake_build(script, workdir, supplied=None, locale="th"):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        return clip, {}

    async def fake_deliver(client, state, script, clip):
        state["last_script"] = script

    monkeypatch.setattr(main.render, "build", fake_build)
    monkeypatch.setattr(main, "deliver", fake_deliver)
    monkeypatch.setattr(main, "close_prompt", _nothing)
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main.manifest, "update", lambda *a, **kw: None)
    monkeypatch.setattr(main, "spawn", lambda coro, label: spawned.append(label) or coro.close())

    state = {"script": a_script(), "locale": "th",
             "pair": {"topic": "หัวข้อ", "pair_id": "clip-th"}}
    asyncio.run(main.do_render(None, state))
    assert spawned == ["continue_pair"]


def test_a_failed_render_cancels_the_other_half(monkeypatch):
    said = []

    async def boom(script, workdir, supplied=None, locale="th"):
        raise RuntimeError("ffmpeg ล้ม")

    async def fake_say(client, text, **kw):
        said.append(text)

    monkeypatch.setattr(main.render, "build", boom)
    monkeypatch.setattr(main, "close_prompt", _nothing)
    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main.manifest, "update", lambda *a, **kw: None)
    monkeypatch.setattr(main, "spawn", lambda coro, label: coro.close())

    state = {"script": a_script(), "locale": "th", "pair": {"topic": "หัวข้อ"}}
    asyncio.run(main.do_render(None, state))
    assert "pair" not in state
    assert any("ยกเลิกภาษาอังกฤษ" in text for text in said)


def test_both_halves_of_a_pair_carry_the_same_id(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "DIR", tmp_path / "clips")
    monkeypatch.setattr(main, "close_prompt", _nothing)
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main.history, "recent_titles", lambda locale=None: [])

    async def no_winners(limit=3, locale="th"):
        return []

    monkeypatch.setattr(main.analytics, "winning_examples", no_winners)

    async def fake_generate(topic, previous=None, feedback="", avoid=None,
                            winners=None, style="", locale="th", sibling=None):
        return a_script() if locale == "th" else an_english_script()

    monkeypatch.setattr(main.script_gen, "generate", fake_generate)

    state = {"mode": "idle", "pair": {"topic": "หัวข้อ"}}
    asyncio.run(main.make_script(None, state, "หัวข้อ", locale="th"))
    first = state["clip_id"]
    asyncio.run(main.make_script(None, state, "หัวข้อ", locale="en",
                                 pair_id=state["pair"]["pair_id"]))

    records = {r["id"]: r for r in manifest.load_all()}
    assert len(records) == 2
    assert {r["pair_id"] for r in records.values()} == {first}
    assert records[first]["locale"] == "th"


def test_the_trends_list_offers_both_a_thai_and_a_pair_row():
    keyboard = main.topics_keyboard([{"topic": "a"}, {"topic": "b"}], "stamp")
    rows = keyboard["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == ["pick:stamp:0", "pick:stamp:1"]
    assert [b["callback_data"] for b in rows[1]] == ["pair:stamp:0", "pair:stamp:1"]


def test_a_pair_button_credits_the_same_list_a_number_button_does():
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    state = {"suggested": [{"topic": "หนึ่ง"}, {"topic": "สอง"}], "suggested_at": stamp}
    assert main.picked(state, f"pick:{stamp}:1") == "สอง"
    assert main.picked(state, f"pair:{stamp}:1") == "สอง"
    assert main.picked(state, "pair:2020-01-01T00:00:00:1") is None


def test_each_finished_clip_keeps_its_own_upload_button(monkeypatch, tmp_path):
    """Two clips minutes apart — a pair does exactly that — and the older
    button must still upload the clip it was sent under."""
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "say", _nothing)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main.youtube, "configured", lambda locale="th": True)

    keyboards = []

    async def fake_send_video(client, path, caption, **kw):
        keyboards.append(kw.get("reply_markup"))
        return {"message_id": len(keyboards)}

    monkeypatch.setattr(main, "send_video", fake_send_video)
    clip = tmp_path / "rendered.mp4"
    clip.write_bytes(b"x")

    state = {"locale": "th", "clip_id": "clip-th", "topic": "หัวข้อ"}
    asyncio.run(main.deliver(None, state, a_script(), clip))
    state.update(locale="en", clip_id="clip-en", topic="topic")
    asyncio.run(main.deliver(None, state, an_english_script(), clip))

    assert [k["inline_keyboard"][0][0]["callback_data"] for k in keyboards] == [
        "upload:clip-th", "upload:clip-en"]
    assert set(state["uploads"]) == {"clip-th", "clip-en"}

    first = main._to_upload(state, "clip-th")
    assert first["locale"] == "th" and first["script"]["title"] == "ทดสอบ"


def test_an_upload_button_without_an_id_still_means_the_last_clip():
    """Buttons sent before ids travelled in the callback data are still live."""
    state = {"last_clip": "/tmp/a.mp4", "last_script": a_script(),
             "last_topic": "หัวข้อ", "last_locale": "th", "uploads": {}}
    assert main._to_upload(state, None)["clip"] == "/tmp/a.mp4"
    assert main._to_upload({"uploads": {}}, None) is None


def test_two_stuck_requests_get_a_third_rather_than_more_waiting(monkeypatch):
    """Measured 2026-09-07 17:04: a request hung, its hedge hung too, and both
    were silent at the 600s deadline — while the same topic answered in 62s
    when it was simply asked again. One hedge is not enough."""
    monkeypatch.setattr(script_gen, "HEDGE_AFTER", 0.05)
    monkeypatch.setattr(script_gen, "HEDGE_AGAIN", 0.05)
    monkeypatch.setattr(script_gen, "HEDGE_MIN_ROOM", 0.05)
    client, calls = hanging_client([30, 30, 0.05])   # only the third answers

    text = asyncio.run(script_gen._say(client, [], 0.8, budget=5))
    assert text == "answer-2"
    # the first hedge goes to the other model, the second one back to the
    # better writer
    assert calls == [script_gen.PRIMARY_MODEL, script_gen.FALLBACK_MODEL,
                     script_gen.PRIMARY_MODEL]


def test_a_hedge_is_not_fired_with_no_time_left_to_answer(monkeypatch):
    """A request sent into the last seconds of the budget cannot come back;
    firing it only spends tokens on an answer nobody will read."""
    monkeypatch.setattr(script_gen, "HEDGE_AFTER", 0.05)
    monkeypatch.setattr(script_gen, "HEDGE_MIN_ROOM", 30.0)
    client, calls = hanging_client([30, 30])

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(script_gen._say(client, [], 0.8, budget=0.3))
    assert calls == [script_gen.PRIMARY_MODEL], "no room, so no hedge at all"


# --- the trends schedule (docs/adr/0009) -------------------------------------

def _schedule(tmp_path, monkeypatch):
    import importlib
    from app import schedule
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    return importlib.reload(schedule)


def test_no_stored_schedule_behaves_exactly_as_the_environment_did(tmp_path, monkeypatch):
    """A container that has never been given a schedule must not change."""
    monkeypatch.setenv("TRENDS_HOURS", "8,12,17")
    monkeypatch.setenv("AUTO_PICK_MINUTES", "15")
    schedule = _schedule(tmp_path, monkeypatch)
    stored = schedule.settings()
    assert stored["th"] == {"enabled": True, "hours": [8, 12, 17], "auto_pick_minutes": 15}
    # English starts off: switching it on publishes to a channel unattended,
    # which is a decision, not a side effect of deploying.
    assert stored["en"]["enabled"] is False


def test_a_schedule_that_will_not_parse_falls_back_rather_than_killing_the_bot(
        tmp_path, monkeypatch):
    schedule = _schedule(tmp_path, monkeypatch)
    schedule.PATH.write_text("{ not json", encoding="utf-8")
    assert schedule.settings()["th"]["enabled"] is True
    # Same for a file that parses but stores nonsense — the file is writable
    # from a LAN page and the bot must survive whatever ends up in it.
    schedule.PATH.write_text('{"th": {"hours": [99]}}', encoding="utf-8")
    assert schedule.settings()["th"]["hours"] != [99]


@pytest.mark.parametrize("payload, wrong", [
    ({"th": {"hours": [24]}}, "0-23"),
    ({"th": {"hours": ["ห้าโมง"]}}, "ตัวเลข"),
    ({"th": {"hours": list(range(13))}}, "รอบต่อวัน"),
    ({"th": {"hours": [8], "auto_pick_minutes": 0}}, "auto_pick_minutes"),
    ({"th": {"hours": [8], "auto_pick_minutes": 9999}}, "auto_pick_minutes"),
    ({"th": {"enabled": True, "hours": []}}, "ไม่ได้ตั้งเวลา"),
    ({"fr": {"hours": [8]}}, "ไม่รู้จักภาษา"),
    ({"th": "8,12"}, "object"),
])
def test_the_schedule_form_is_not_trusted(tmp_path, monkeypatch, payload, wrong):
    """Untrusted text off a LAN page behind one basic auth. Nothing is coerced."""
    schedule = _schedule(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as caught:
        schedule.validate(payload)
    assert wrong in str(caught.value)


def test_saving_a_schedule_normalises_it(tmp_path, monkeypatch):
    schedule = _schedule(tmp_path, monkeypatch)
    stored = schedule.save({"th": {"enabled": "yes", "hours": ["12", 8, 8],
                                   "auto_pick_minutes": "20"}})
    assert stored["th"] == {"enabled": True, "hours": [8, 12], "auto_pick_minutes": 20}
    assert schedule.settings()["th"]["hours"] == [8, 12]


def test_two_trends_rounds_never_overlap(monkeypatch):
    """Two Locales can be scheduled for the same hour.

    Overlapping rounds would leave one `suggested` list in state with the other
    round's buttons still on screen — a 💡 button that writes about something
    the human never saw.
    """
    said = []

    async def fake_say(client, text, **extra):
        said.append(text)
        return {"message_id": 1}

    async def boom(*args, **kwargs):
        raise AssertionError("a second round must not start")

    monkeypatch.setattr(main, "say", fake_say)
    monkeypatch.setattr(main, "save_state", lambda state: None)
    monkeypatch.setattr(main, "_trends_round", boom)
    asyncio.run(main.on_trends(None, {"trends_running": True}))
    assert "รอบก่อนอยู่" in said[0]

    # And the flag is cleared even when the round blows up, or every later
    # round is blocked by a marker nobody can see.
    async def explode(*args, **kwargs):
        raise RuntimeError("trend source down")

    monkeypatch.setattr(main, "_trends_round", explode)
    state = {}
    with pytest.raises(RuntimeError):
        asyncio.run(main.on_trends(None, state))
    assert "trends_running" not in state
