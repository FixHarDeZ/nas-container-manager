"""Turns a Topic into a Script by asking mimo."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from openai import AsyncOpenAI

from app import locales, render

logger = logging.getLogger(__name__)

MIN_CARDS, MAX_CARDS = 5, 7
MAX_LINES_PER_CARD = 4
# What the model is asked to aim for. Not enforced: the renderer measures real
# pixel width and shrinks the font to fit, and character count is a poor proxy
# anyway — Thai glyphs are narrower than Latin ones.
TARGET_CHARS_PER_LINE = locales.get("th")["target_chars"]
# Told to the model, which cannot measure pixels, and used as the fallback rule
# wherever the font is unavailable. The real gate is _too_wide(): a character
# count is a poor proxy for Thai, where vowels and tone marks carry no advance
# width. Measured over every line this bot has written (209 lines, 2026-08-29):
# the widest came to 719px at 33 characters, well inside the 864px available.
HARD_MAX_CHARS_PER_LINE = locales.get("th")["hard_max_chars"]

LATIN = re.compile(r"[A-Za-z]+")
THAI = re.compile(r"[\u0e00-\u0e7f]+")
# What `spoken` may not contain, per Locale. A Thai voice handed a Latin word
# switches accent mid-sentence and rushes it; an English voice handed Thai
# script cannot say it at all.
FORBIDDEN_IN_SPOKEN = {"thai": LATIN, "latin": THAI}

SYSTEM_PROMPT_TH = f"""คุณเป็นคนเขียนสคริปต์ YouTube Shorts ภาษาไทย

หัวข้ออะไรก็ได้ตามที่สั่ง (เทค การเงิน สุขภาพ ไลฟ์สไตล์ ความรู้รอบตัว ฯลฯ)
เขียนแบบคนที่รู้เรื่องนั้นจริงและเล่าให้เพื่อนฟัง ไม่ใช่ท่องสารานุกรม

**ห้ามกล่าวอ้างเรื่องบุคคลจริง (ดารา นักการเมือง นักกีฬา) ข่าวสด คดีความ หรือผลการแข่งขัน**
ถ้าหัวข้อพาไปทางนั้น ให้เล่าเฉพาะแง่มุมที่เป็นความรู้ทั่วไปซึ่งตรวจสอบได้

เขียนสคริปต์คลิปแนวตั้ง ยาว 40-50 วินาที แบ่งเป็น card ละ 6-9 วินาที

กฎ:
- มี {MIN_CARDS}-{MAX_CARDS} card
- card แรกคือ hook ต้องหยุดนิ้วคนดูใน 3 วินาที ตั้งคำถามหรือชี้ความเจ็บปวดที่คนดูเจอจริง ห้ามเกริ่นแบบ "วันนี้เราจะมาพูดถึง"
- card สุดท้ายสรุปสั้นๆ ให้คนดูเอาไปใช้ต่อได้
- แต่ละ card มี lines = ข้อความบนจอ 1-{MAX_LINES_PER_CARD} บรรทัด บรรทัดละราวๆ {TARGET_CHARS_PER_LINE} ตัวอักษร (ห้ามเกิน {HARD_MAX_CHARS_PER_LINE})
  **สำคัญ: ต้องตัดบรรทัดตรงรอยต่อคำภาษาไทยเอง** เพราะโปรแกรมวาดตัวอักษรตามที่ให้มาเป๊ะๆ ตัดผิดที่แล้วคำจะขาดกลางคำ
- narration = ประโยคของ card นั้น เขียนแบบพูด ไม่ใช่แบบเขียน ยาวพอให้อ่าน 6-9 วินาที
  คำอังกฤษเขียนเป็นอังกฤษตามปกติ (ใช้ขึ้นซับบนจอ) ห้ามใส่ emoji หรือสัญลักษณ์ที่อ่านออกเสียงไม่ได้
  **ใส่จุลภาคคั่นตรงจุดที่คนพูดจะหยุดหายใจ** ประมาณทุก 10-15 คำ
- spoken = narration ประโยคเดียวกันเป๊ะ แต่**เขียนด้วยอักษรไทยล้วน ห้ามมีตัวอักษรละติน (a-z, A-Z) แม้แต่ตัวเดียว**
  ทับศัพท์คำอังกฤษทุกคำ เช่น Docker → ด็อกเกอร์, log → ล็อก, container → คอนเทนเนอร์,
  AI → เอไอ, CPU → ซีพียู, Netflix → เน็ตฟลิกซ์, cliffhanger → คลิฟแฮงเกอร์
  เพราะเครื่องอ่านจะสลับไปสำเนียงอังกฤษกลางประโยค พูดรัวจนฟังไม่ทันและไม่ชัด
  ตัวเลขให้เขียนเป็นคำอ่านไทย เช่น 2024 → สองพันยี่สิบสี่, 1-2 นาที → หนึ่งถึงสองนาที
  ถ้าคำอังกฤษเป็นการเล่นคำที่อ่านเป็นไทยได้ ให้ใช้คำอ่านนั้น ไม่ใช่สะกดทีละตัวอักษร
  เช่น TH-AI Passport → ไทยพาสปอร์ต (ไม่ใช่ ทีเอไอพาสปอร์ต)
  **ห้ามมีขีดกลาง (-) ใน spoken** เครื่องอ่านจะหยุดเงียบตรงขีด ชื่อรุ่นให้เขียนติดกัน
  เช่น F-35 → เอฟสามสิบห้า, GPT-4 → จีพีทีโฟร์
  คำสั่ง/แฟลกที่ทับศัพท์แล้วงง (เช่น --log-opt) ให้เลี่ยงไปพูดเป็นคำอธิบายแทน
- code = บล็อกโค้ด/คำสั่งสั้นๆ ไม่เกิน 4 บรรทัด ใส่เฉพาะ card ที่มีคำสั่งจริงให้ดู ถ้าไม่มีให้เป็น null
- query = **คำค้นภาษาอังกฤษ 2-4 คำ** สำหรับหาคลิป stock footage มาเป็นพื้นหลังของ card นั้น
  ต้องเป็นสิ่งที่**ถ่ายเป็นวิดีโอได้จริง** เช่น "server room racks", "developer typing keyboard",
  "data center lights" ห้ามใช้คำนามธรรมที่ถ่ายไม่ได้ เช่น "docker configuration", "log rotation"
- title/description/hashtags = สำหรับอัปขึ้น YouTube, hashtags 3-5 ตัว ขึ้นต้นด้วย #
- category = หมวดของคลิปนี้ คำสั้นๆ ภาษาไทย เช่น เทค, การเงิน, สุขภาพ, ไลฟ์สไตล์, เกม,
  ความรู้รอบตัว — ใช้บันทึกว่าหมวดไหนคนดูเยอะ ไม่ได้โชว์ในคลิป

ตอบเป็น JSON อย่างเดียว ห้ามมีข้อความอื่นนอก JSON:
{{"title": "...", "description": "...", "hashtags": ["#..."], "category": "...",
  "cards": [{{"lines": ["..."], "code": null, "query": "...",
             "narration": "...", "spoken": "..."}}]}}"""


TRENDS_PROMPT_TH = """คุณเป็นคนเลือกหัวข้อคลิป YouTube Shorts ภาษาไทย

จะได้รับรายการ "สิ่งที่คนไทยกำลังค้นหา/กำลังดู" ตอนนี้ หน้าที่คุณคือแปลงเป็น
**หัวข้อคลิปที่ทำได้จริง 5 หัวข้อ**

กฎเหล็ก:
- **ห้ามเสนอหัวข้อที่เป็นข่าวสด การเมือง คดีความ ผลการแข่งขัน หรือเรื่องของบุคคลจริง**
  (ดารา นักการเมือง นักกีฬา) เพราะคลิปจะกลายเป็นการกล่าวอ้างเรื่องคนจริงโดยไม่มีหลักฐาน
  ถ้ากระแสนั้นเป็นข่าวคน ให้**ข้ามไปเลย** หรือดึงเฉพาะแง่มุมที่อธิบายได้แบบไม่พาดพิงใคร
  เช่น กระแส "ชิป M6" → "ชิป M6 ต่างจาก M4 ยังไง" (โอเค),
  กระแส "นายก..." → ข้าม
- **ห้ามตั้งหัวข้อที่เป็นการคาดเดา/ยืนยันเรื่องของคนจริงเด็ดขาด** เช่น
  "ดาราคนนั้นจะกลับมาเล่นจริงไหม", "นักร้องคนนี้เลิกกับใคร", "ผู้บริหารคนนั้นจะลาออกไหม"
  — พวกนี้คือข่าวลือ บอทไม่มีทางรู้ แล้วจะเดาใส่ปากคนจริง
  ถ้ากระแสมาจากหนัง/ซีรีส์/เกม ให้เล่า**ตัวงาน**แทน เช่น "จักรวาลนี้เล่าเรื่องอะไรมาบ้าง"
  ไม่ใช่ "ใครจะกลับมาแสดง"
- เอาหัวข้อที่**อธิบายได้ด้วยข้อเท็จจริงที่อยู่ตัวแล้ว** ไม่ใช่เรื่องที่ต้องรู้ข่าวล่าสุดถึงจะพูดถูก
- หัวข้อละ 1 บรรทัด เขียนแบบที่พิมพ์ส่งให้บอทเขียนสคริปต์ได้ทันที
- kind = "evergreen" ถ้าเรื่องนี้ยังน่าดูอีก 6 เดือน, "spike" ถ้าตายพร้อมกระแส
- category = หมวดสั้นๆ ภาษาไทย เช่น เทค, การเงิน, สุขภาพ, ไลฟ์สไตล์, เกม, ความรู้รอบตัว
- from = คำ/ชื่อคลิปต้นทางที่จุดประกายหัวข้อนี้ (ก๊อปมาจากรายการที่ให้)
- why = เหตุผลสั้นๆ ว่าทำไมคนน่าจะดู

ตอบเป็น JSON อย่างเดียว:
{"topics": [{"topic": "...", "kind": "evergreen", "category": "...", "from": "...", "why": "..."}]}"""


# The English prompt is written in English on purpose: an English Script asked
# for in Thai comes back translated rather than written, and it reads like it.
# `spoken` survives the crossing — an English voice does not need words
# transliterated, but it does need numbers, symbols and initialisms spelled the
# way they are said.
EN = locales.get("en")
SYSTEM_PROMPT_EN = f"""You write YouTube Shorts scripts in English for a US audience.

Any topic goes (tech, money, health, lifestyle, general knowledge). Write like
someone who actually knows the subject telling a friend, not an encyclopedia.

**Never make claims about real people (celebrities, politicians, athletes),
breaking news, court cases or match results.** If the topic points that way,
cover only the checkable general-knowledge angle.

Write a vertical clip 40-50 seconds long, split into cards of 6-9 seconds.

Rules:
- {MIN_CARDS}-{MAX_CARDS} cards
- the first card is the hook: it must stop a thumb within 3 seconds by asking a
  question or naming a pain the viewer really has. Never open with "today we're
  going to talk about"
- the last card is a short takeaway the viewer can use
- each card has lines = 1-{MAX_LINES_PER_CARD} lines of on-screen text, about
  {EN["target_chars"]} characters each (never more than {EN["hard_max_chars"]})
  **Break the lines yourself at word boundaries.** The renderer draws exactly
  what you send; a line over the limit is rejected, and a long line shrinks the
  font until it is unreadable on a phone.
- narration = the sentence for that card, spoken English rather than written
  English, long enough to read aloud in 6-9 seconds. No emoji and no symbols
  that cannot be read out loud.
  **Put a comma wherever a speaker would draw breath**, roughly every 10-15 words.
- spoken = the same sentence, written the way it is said, and it must contain
  **no Thai characters at all**. Spell out anything the voice would stumble on:
  numbers as words (2026 -> twenty twenty six, 1-2 minutes -> one to two
  minutes), symbols as words (%, $, & -> percent, dollars, and), and
  initialisms with spaces so they are read letter by letter (CPU -> C P U).
  **No hyphens in spoken** — the voice pauses on them. GPT-4 -> GPT four.
  If nothing needs respelling, repeat narration verbatim.
- code = a short command or code block, at most 4 lines, only on a card that
  really shows one. Otherwise null.
- query = **2-4 English words** to search stock footage for that card's
  background. It must be something a camera can film: "server room racks",
  "developer typing keyboard", "data center lights". Never abstract phrases
  like "docker configuration" or "log rotation".
- title/description/hashtags = for the YouTube upload; 3-5 hashtags, each
  starting with #
- category = a short English word for what this clip is about (tech, money,
  health, lifestyle, gaming, general) — recorded to see which category holds
  viewers, never shown in the clip

Answer with JSON only, nothing outside the JSON:
{{"title": "...", "description": "...", "hashtags": ["#..."], "category": "...",
  "cards": [{{"lines": ["..."], "code": null, "query": "...",
             "narration": "...", "spoken": "..."}}]}}"""


TRENDS_PROMPT_EN = """You pick topics for English-language YouTube Shorts aimed at a US audience.

You will be given a list of what people in the US are searching for and
watching right now. Turn it into **5 topics that can actually be made**.

Hard rules:
- **Never propose a topic that is breaking news, politics, a court case, a
  match result, or anything about a real person** (celebrity, politician,
  athlete): the clip would end up asserting things about real people with no
  source. If a trend is about a person, **skip it**, or take only the angle
  that can be explained without naming anyone. Trend "M6 chip" -> "how the M6
  differs from the M4" (fine); trend "<politician>" -> skip.
- **Never propose a topic that speculates about a real person** ("is that actor
  coming back", "who did they break up with", "will that CEO resign") — that is
  rumour, the bot cannot know, and it would put words in a real person's mouth.
  If the trend comes from a film, series or game, cover **the work itself**.
- Take topics explainable from settled facts, not ones that need today's news
  to get right.
- One line per topic, written so it can be sent straight to the script writer.
- kind = "evergreen" if it is still worth watching in 6 months, "spike" if it
  dies with the trend.
- category = short English word: tech, money, health, lifestyle, gaming, general
- from = the term or video title that sparked it (copied from the list given)
- why = one short line on why people would watch

Answer with JSON only:
{"topics": [{"topic": "...", "kind": "evergreen", "category": "tech", "from": "...", "why": "..."}]}"""

SYSTEM_PROMPTS = {"th": SYSTEM_PROMPT_TH, "en": SYSTEM_PROMPT_EN}
TRENDS_PROMPTS = {"th": TRENDS_PROMPT_TH, "en": TRENDS_PROMPT_EN}


def system_prompt(locale: str = locales.DEFAULT) -> str:
    return SYSTEM_PROMPTS.get(locale, SYSTEM_PROMPT_TH)


def trends_prompt(locale: str = locales.DEFAULT) -> str:
    return TRENDS_PROMPTS.get(locale, TRENDS_PROMPT_TH)


class ScriptError(ValueError):
    """The model returned something we cannot render."""


# Latency here tracks how much the model decides to think, not the network.
# Measured on the NAS, same prompt: 93s/3,092 completion tokens, 112s/4,016,
# 197s/7,010, 207s/5,415, 347s/10,585 — about 30 tokens a second, every time.
# It does not stall at random; it thinks for longer. So a wall-clock cap is the
# right shape after all, it was simply set at 240s where a long think needs
# ~350s, and the retry doubled the wait on top.
#
# Streaming was tried and abandoned: reading the same answer as a stream took
# 400s against 137s unstreamed, so the idle-detection it buys costs three times
# the wall clock it was meant to save.
BUDGET_SECONDS = float(os.environ.get("MIMO_TIMEOUT_SECONDS", "600"))
# Below this there is no point starting another attempt.
MIN_ATTEMPT = 90.0
# There are two failure shapes, and they need different answers. Most slow runs
# are the model thinking: 93s/3,092 completion tokens, 112s/4,016, 197s/7,010,
# 207s/5,415, 347s/10,585 — those finish, and cutting them off is what broke
# the bot at 240s. But a request can also take the headers and never deliver a
# body at all: observed 2026-08-27 19:25:45, "200 OK" logged instantly, silence
# until the 600s deadline. Waiting out a hang costs ten minutes; killing a long
# think costs the clip. So neither: after HEDGE_AFTER a second identical
# request goes out alongside the first and whichever answers first wins. A
# healthy long think (347s) still lands; a hung one is overtaken by its twin.
HEDGE_AFTER = 240.0
# The twin goes to the *other* model on purpose. Proven the same evening: the
# same topic hung twice past 600s — including once with an identical hedge
# alongside it, so both requests were stuck in the same episode — and then
# answered in 137s an hour later. A hedge that shares the sick pool is no
# hedge; mimo-v2.5 wrote the same script in 149s while the pro model was
# healthy, so it is a real fallback and not a downgrade to nothing.
FALLBACK_MODEL = os.environ.get("MIMO_FALLBACK_MODEL", "mimo-v2.5")
PRIMARY_MODEL = os.environ.get("MIMO_MODEL", "mimo-v2.5-pro")


# The prompt a human pastes into Google Flow is short and is written while
# they wait, so it gets its own, much smaller budget than a Script.
FLOW_BUDGET_SECONDS = float(os.environ.get("FLOW_PROMPT_TIMEOUT_SECONDS", "180"))

FLOW_SYSTEM_PROMPT = """คุณเขียน prompt ภาษาอังกฤษให้คนเอาไปวางใน Google Flow (โมเดล Veo)
เพื่อสร้างวิดีโอพื้นหลังแนวตั้ง 8 วินาที สำหรับการ์ดหนึ่งใบของคลิป YouTube Shorts

ตอบกลับมาเป็น prompt เดียว ภาษาอังกฤษ ย่อหน้าเดียว ไม่เกิน 60 คำ
ห้ามมีหัวข้อ ห้ามมีคำอธิบาย ห้ามมีเครื่องหมายคำพูดครอบ ห้ามใส่หมายเลขข้อ

กติกา:
- 9:16 vertical. บอกช็อต มุมกล้อง แสง และการเคลื่อนกล้องให้ชัด (slow push in, static wide ฯลฯ)
- **ห้ามมีตัวหนังสือใดๆ ในภาพ** (no text, no captions, no UI, no logos, no signage)
  เพราะโปรแกรมจะวาดข้อความไทยทับอีกชั้น ตัวหนังสือซ้อนกันอ่านไม่ออก
- **ห้ามมีใบหน้าที่ระบุตัวตนได้ และห้ามอ้างอิงบุคคลจริง** — ถ่ายมือ ไหล่ เงา ฉากหลัง หรือระยะไกลแทน
- กลางจอต้องโล่ง ให้ subject อยู่ริมเฟรมหรือเป็นฉากกว้าง เพราะข้อความจะทับตรงกลาง
- ห้ามพูดถึงเสียง เพลง หรือคำบรรยาย เสียงทั้งหมดมาจากที่อื่น"""


def _client() -> AsyncOpenAI:
    # httpx logs "200 OK" when the headers arrive, so a response that stalls
    # mid-body reads as a success in the log while the call hangs.
    return AsyncOpenAI(
        api_key=os.environ["MIMO_API_KEY"],
        base_url=os.environ["MIMO_BASE_URL"],
        timeout=float(os.environ.get("MIMO_TIMEOUT_SECONDS", "180")),
        max_retries=1,
    )


async def _say(client: AsyncOpenAI, messages: list[dict], temperature: float,
               budget: float, models: tuple[str, str] | None = None) -> str:
    """One completion, hedged against a request that hangs.

    The deadline is enforced here rather than left to httpx, whose timeout is
    per read: a server that trickles bytes resets that clock forever and the
    call never returns.

    `models` is (who answers first, who the hedge goes to); the two must differ
    or the hedge shares whatever is making the first one sick.
    """

    async def once(model: str) -> str:
        started = time.monotonic()
        reply = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            # This is a reasoning model and its thinking budget drives the
            # latency: measured 161s / 10457 tokens at the default against
            # 79s / 3796 at "low", for a better script. "minimal" is rejected
            # with a 400.
            reasoning_effort=os.environ.get("MIMO_REASONING_EFFORT", "low"),
        )
        # The only way to tell a healthy long think from a sick endpoint after
        # the fact. Thinking runs at about 30 tokens a second whatever the
        # length; a stalled request never reaches this line at all while its
        # hedged twin does.
        spent = time.monotonic() - started
        used = getattr(reply, "usage", None)
        tokens = getattr(used, "completion_tokens", 0) or 0
        # Read defensively: the test doubles build a bare SimpleNamespace with
        # no finish_reason at all, and a real reply that got cut off mid-JSON
        # by the token cap reports "length" here rather than raising.
        finish_reason = getattr(reply.choices[0], "finish_reason", None)
        logger.info(
            "%s ตอบใน %.0f วินาที %d tokens (%.0f tokens/วินาที) finish_reason=%s",
            model, spent, tokens, tokens / spent if spent else 0, finish_reason,
        )
        content = reply.choices[0].message.content or ""
        if finish_reason == "length" or not content.strip():
            # Junk here would otherwise be returned as if it were a real
            # answer; raising lets the hedge/retry machinery treat it the same
            # as any other failed attempt instead of feeding it to _parse().
            raise ScriptError(
                f"{model} ตอบไม่ครบ (finish_reason={finish_reason}, {len(content)} ตัวอักษร)"
            )
        return content

    primary, hedge_to = models or (PRIMARY_MODEL, FALLBACK_MODEL)
    running = {asyncio.create_task(once(primary))}
    waited = 0.0
    hedged = False
    try:
        while True:
            slice_for = min(HEDGE_AFTER, budget) - waited if not hedged else budget - waited
            if slice_for <= 0:
                raise asyncio.TimeoutError
            started = time.monotonic()
            done, running = await asyncio.wait(
                running, timeout=slice_for, return_when=asyncio.FIRST_COMPLETED
            )
            waited += time.monotonic() - started
            if done:
                # Any answer will do; a failed twin is not worth reporting when
                # the other one is still running.
                for task in done:
                    if not task.exception():
                        return task.result()
                if not running:
                    raise next(iter(done)).exception()
                continue
            if hedged or waited >= budget:
                raise asyncio.TimeoutError
            logger.warning(
                "%s ยังไม่ตอบใน %.0f วินาที ยิงคำขอสำรองไปที่ %s คู่ไปด้วย",
                primary, waited, hedge_to,
            )
            running.add(asyncio.create_task(once(hedge_to)))
            hedged = True
    finally:
        for task in running:
            task.cancel()


def _too_wide(line: str, locale: str = locales.DEFAULT) -> int:
    """Characters to cut so the renderer can draw the line, 0 if it already can.

    The renderer shrinks the font until the text fits, so the line it cannot
    draw at all is one still too wide at its smallest size. Measured against
    the narrower of the two draw paths: text over footage is laid out at the
    1080px frame, not the oversized 1210px gradient card.

    That pixel floor is the whole test for Thai, whose glyphs fit at full size
    anyway (34 characters came to 719px of 864 at size 92). It is not enough
    for Latin, which runs about 50px a character against Thai's 21: a 38-
    character English line clears the floor at size 40 and is then *drawn* at
    size 40, unreadable on a phone. So a Locale can also hold the model to its
    character count, and the answer is whichever cut is larger.
    """
    spec = locales.get(locale)
    counted = max(0, len(line) - spec["hard_max_chars"]) if spec.get("enforce_char_count") else 0
    try:
        font = render._font(render.THAI_BOLD, render.MIN_TEXT_SIZE)
    except (OSError, RuntimeError):
        # No Waree, or Pillow without Raqm — off the container. The renderer
        # refuses to run at all in that state, so fall back to the count
        # rather than let every line through.
        return max(counted, len(line) - spec["hard_max_chars"], 0)
    usable = render.W - render.MARGIN * 2
    width = font.getlength(line)
    if width <= usable:
        return counted
    return max(counted, 1, round(len(line) * (width - usable) / width))


def validate(script: dict, locale: str = locales.DEFAULT) -> dict:
    """Reject a Script the renderer would mangle. Raises ScriptError.

    Messages stay in Thai even for an English Script: they are read by the
    human in Telegram, and the model is fed them as a correction, which it
    handles in either language.
    """
    spec = locales.get(locale)
    for key in ("title", "description", "hashtags", "cards", "category"):
        if key not in script:
            raise ScriptError(f"ไม่มีฟิลด์ {key}")

    cards = script["cards"]
    if not isinstance(cards, list) or not MIN_CARDS <= len(cards) <= MAX_CARDS:
        raise ScriptError(f"ต้องมี {MIN_CARDS}-{MAX_CARDS} card แต่ได้ {len(cards) if isinstance(cards, list) else '?'}")

    for i, card in enumerate(cards, 1):
        lines = card.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES_PER_CARD:
            raise ScriptError(f"card {i}: lines ต้องมี 1-{MAX_LINES_PER_CARD} บรรทัด")
        for line in lines:
            if not isinstance(line, str) or not line.strip():
                raise ScriptError(f"card {i}: มีบรรทัดว่าง")
            over = _too_wide(line, locale)
            if over:
                # Say how much to cut: this message is fed back to the model on
                # the retry, and it cannot measure the line itself. Only offer
                # the extra line where the card has one left to give.
                room = (
                    " (ขึ้นบรรทัดใหม่ได้ ไม่ทำให้คลิปยาวขึ้น)"
                    if len(lines) < MAX_LINES_PER_CARD else ""
                )
                raise ScriptError(
                    f"card {i}: บรรทัดกว้างเกินการ์ด ต้องตัดออกอีกราว {over} ตัว{room}: {line}"
                )
        if not str(card.get("narration", "")).strip():
            raise ScriptError(f"card {i}: ไม่มี narration")
        spoken = str(card.get("spoken", "")).strip()
        if not spoken:
            raise ScriptError(f"card {i}: ไม่มี spoken (narration ฉบับที่เสียงอ่านได้)")
        # A Latin word makes the Thai voice switch accent mid-sentence: it reads
        # the English at English pace, which lands as a rushed, unclear burst
        # inside Thai speech. The screen keeps the real spelling; only the voice
        # gets the transliteration. Mirrored for English, where Thai script in
        # `spoken` is something the voice simply cannot pronounce.
        forbidden = FORBIDDEN_IN_SPOKEN[spec["spoken_script"]]
        found = forbidden.findall(spoken)
        if found:
            wrong = "ละติน" if spec["spoken_script"] == "thai" else "ไทย"
            raise ScriptError(
                f"card {i}: spoken มีตัวอักษร{wrong} ({found[:3]}) ต้องเขียนเป็น"
                f"{'ไทย' if spec['spoken_script'] == 'thai' else 'อังกฤษ'}ทั้งหมด"
            )
        if not str(card.get("query", "")).strip():
            raise ScriptError(f"card {i}: ไม่มี query สำหรับหา footage")
    return script


def _parse(raw: str) -> dict:
    """Pull the JSON object out of a model reply that may be fenced."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ScriptError("โมเดลไม่ได้ตอบเป็น JSON")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ScriptError(f"JSON พัง: {exc}") from exc


NOTES = {
    "th": (
        "เคยทำคลิปเรื่องพวกนี้ไปแล้ว ห้ามเขียนซ้ำมุมเดิม ถ้าหัวข้อใกล้เคียงให้หามุมใหม่:\n",
        "คลิปที่คนดูจนจบมากที่สุดคือเรื่องพวกนี้ เขียนให้ใกล้เคียงแนวนี้:\n",
    ),
    "en": (
        "These clips have already been made. Do not repeat the same angle; find "
        "a new one if the topic is close:\n",
        "These are the clips people watched furthest through. Write in that "
        "direction:\n",
    ),
}


def _context_note(avoid: list[str], winners: list[str],
                  locale: str = locales.DEFAULT) -> str:
    """Tell the model what has been made already and what worked."""
    avoid_note, winners_note = NOTES.get(locale, NOTES["th"])
    parts = []
    if avoid:
        parts.append(avoid_note + "\n".join(f"- {title}" for title in avoid))
    if winners:
        parts.append(winners_note + "\n".join(f"- {title}" for title in winners))
    return "\n\n".join(parts)


async def suggest_topics(rows: list[dict], locale: str = locales.DEFAULT) -> list[dict]:
    """Turn raw trend rows into Topics the bot could actually be given.

    Kept separate from `generate()`: this decides *what* to make, which is the
    human's call, so its output is a list to choose from and never an input to
    a Script (docs/adr/0004).
    """
    listing = "\n".join(
        f"- [{row['source']}] {row['term']} ({row['traffic']:,})"
        + (f" — ข่าว: {row['headline']}" if row.get("headline") else "")
        for row in rows
    )
    raw = await _say(
        _client(),
        [{"role": "system", "content": trends_prompt(locale)},
         {"role": "user", "content": listing}],
        temperature=0.7,
        budget=BUDGET_SECONDS,
    )
    parsed = _parse(raw)
    topics = parsed.get("topics")
    if not isinstance(topics, list) or not topics:
        raise ScriptError("โมเดลไม่ได้เสนอหัวข้อมาเลย")
    return [t for t in topics if str(t.get("topic", "")).strip()][:5]


async def generate(
    topic: str,
    previous: dict | None = None,
    feedback: str = "",
    avoid: list[str] | None = None,
    winners: list[str] | None = None,
    style: str = "",
    locale: str = locales.DEFAULT,
) -> dict:
    """Write a Script for `topic`, optionally revising `previous` per `feedback`.

    `style` is the clause the running Experiment assigned to this Clip. It is
    appended rather than folded into SYSTEM_PROMPT so the Manifest can record
    the exact words that produced this Script — the base prompt will drift, and
    a Variant name alone would not say what it meant at the time.
    """
    messages = [{"role": "system", "content": system_prompt(locale)}]
    note = _context_note(avoid or [], winners or [], locale)
    if note:
        messages.append({"role": "system", "content": note})
    if style:
        messages.append({"role": "system", "content": style})
    label = "หัวข้อ" if locale == "th" else "Topic"
    messages.append({"role": "user", "content": f"{label}: {topic}"})
    if previous is not None:
        messages.append({"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)})
        messages.append({"role": "user", "content": f"แก้ตามนี้: {feedback}"})

    client = _client()
    last_error: Exception | None = None
    # The budget is shared across attempts, not granted afresh to each one:
    # two full-length attempts is twenty minutes of a human staring at
    # "กำลังเขียนสคริปต์...".
    deadline = time.monotonic() + BUDGET_SECONDS
    # Deadline-driven, not a fixed attempt count: a garbage 60-char reply can
    # come back in seconds, and stopping at two tries then leaves most of a
    # 600s budget unspent while a plain retry would have succeeded (observed
    # 2026-09-04). Capped at 4 so a client that always fails fast cannot spin
    # forever inside one budget.
    attempt = 0
    while attempt < 4:
        left = deadline - time.monotonic()
        if left < MIN_ATTEMPT:
            break
        # The retry only ever gets the remainder of the shared budget, and the
        # first attempt can eat almost all of it: measured 2026-08-29, a Script
        # came back after 343s and failed validate(), leaving 257s against a
        # worst case think of 347s — a retry that could not finish. So every
        # attempt after the first leads with the smaller model (149s measured
        # on the same prompt) and hedges back to the pro. Fixing JSON to match
        # a schema it has already been shown is not work that needs the pro
        # model; finishing inside the leftovers is.
        models = None if attempt == 0 else (FALLBACK_MODEL, PRIMARY_MODEL)
        attempt += 1
        try:
            raw = await _say(client, messages, temperature=0.8, budget=left,
                             models=models)
        except asyncio.TimeoutError:
            # Say what went wrong the *first* time too. A retry inherits
            # whatever is left of the shared budget, so a first attempt that
            # answered slowly and then failed validation leaves too little
            # time for the second — and reporting only the timeout hides the
            # schema slip that actually started it.
            timed_out = (
                f"mimo ไม่ตอบภายใน {BUDGET_SECONDS:.0f} วินาที "
                "(ปกติใช้ 90-350 วินาทีตามความยาวที่โมเดลคิด)"
            )
            if last_error is not None:
                timed_out += f" — รอบก่อนหน้า: {last_error}"
            last_error = ScriptError(timed_out)
            continue
        except ScriptError as exc:
            # _say itself rejected the reply (truncated by finish_reason or
            # empty) before it ever became text to parse. Same shape as an
            # unparseable reply below: retry with messages untouched, there is
            # nothing sane to feed back for a reply that was not really an
            # answer.
            last_error = ScriptError(
                f"{exc} — รอบก่อนหน้า: {last_error}" if last_error is not None else str(exc)
            )
            continue
        try:
            parsed = _parse(raw)
        except ScriptError as exc:
            # Parsing failed outright: raw is not JSON at all (the 60-char
            # garbage reply that started this). Feeding it back as an
            # "assistant" turn only pollutes the conversation the *next*
            # model reads, and it is not JSON the model itself agreed to, so
            # retry with messages unchanged instead of the append-and-correct
            # below.
            excerpt = raw[:300]
            logger.warning(
                "โมเดลไม่ตอบเป็น JSON: %s (raw %d ตัวอักษร): %r",
                exc, len(raw), excerpt,
            )
            msg = f"{exc} (raw {len(raw)} ตัวอักษร): {excerpt}"
            last_error = ScriptError(
                f"{msg} — รอบก่อนหน้า: {last_error}" if last_error is not None else msg
            )
            continue
        try:
            return validate(parsed, locale)
        except ScriptError as exc:
            excerpt = raw[:300]
            logger.warning(
                "สคริปต์ผิดกติกา: %s (raw %d ตัวอักษร): %r", exc, len(raw), excerpt,
            )
            # Chained the same as the other two failure shapes above: without
            # this, a fallback model's schema slip overwrites the pro model's
            # earlier failure and the final message shows only the symptom,
            # not the cause (observed 2026-09-04).
            msg = f"{exc} (raw {len(raw)} ตัวอักษร): {excerpt}"
            last_error = ScriptError(
                f"{msg} — รอบก่อนหน้า: {last_error}" if last_error is not None else msg
            )
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"สคริปต์ผิดกติกา: {exc} — ส่ง JSON ใหม่ให้ถูกกติกา"},
            ]
    raise ScriptError(str(last_error))


async def flow_prompt(topic: str, card: dict) -> str:
    """The English Veo prompt a human pastes into Google Flow for one Card.

    Asked for on demand rather than folded into the Script: a Script already
    takes 90-350s to think, most Clips never go the Flow route, and every extra
    field on the schema is latency every Clip pays.
    """
    user = "\n".join([
        f"หัวข้อคลิป: {topic}",
        f"ข้อความบนจอของการ์ดนี้: {' / '.join(card.get('lines') or [])}",
        f"คำที่จะพูดทับ: {card.get('narration', '')}",
        f"คำค้น footage ที่เคยคิดไว้: {card.get('query', '')}",
    ])
    raw = await _say(
        _client(),
        [{"role": "system", "content": FLOW_SYSTEM_PROMPT},
         {"role": "user", "content": user}],
        temperature=0.8,
        budget=FLOW_BUDGET_SECONDS,
    )
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    text = text.strip().strip('"').strip()
    if not text:
        raise ScriptError("โมเดลไม่ได้ตอบ prompt กลับมา")
    return text[:1200]
