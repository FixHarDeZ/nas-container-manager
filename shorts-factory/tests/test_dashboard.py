"""The dashboard reads and only reads.

`DATA_DIR` is read at import time by app.manifest/app.history/app.render, so the
environment is set before the import happens — hence the fixture ordering and
the reloads below.
"""
import importlib
import json
import os
from statistics import median

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """A /data that looks like a live one: two Manifests, state, say."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    clips = tmp_path / "clips"
    clips.mkdir()

    (clips / "20260801-120000-000.json").write_text(json.dumps({
        "id": "20260801-120000-000",
        "created_at": "2026-08-01T12:00:00",
        "topic": "ทำไม container กิน RAM",
        "variant": "shock_number",
        "explore": False,
        "outcome": "rendered",
        "published": True,
        "video_id": "vid1",
        "scripts": [{"at": "2026-08-01T12:02:00", "script": {
            "title": "RAM หายไปไหน", "category": "devops",
            "cards": [{"narration": "การ์ดหนึ่ง", "spoken": "การ์ดหนึ่ง"}],
        }}],
        "snapshots": [{"date": "2026-08-08", "age_days": 7, "views": 182, "percent": 41.2}],
        "render": {"duration": 47.3, "cards": [{"start": 0.0}]},
    }, ensure_ascii=False), encoding="utf-8")

    (clips / "20260802-120000-000.json").write_text(json.dumps({
        "id": "20260802-120000-000",
        "created_at": "2026-08-02T12:00:00",
        "topic": "หัวข้อที่ถูกทิ้ง",
        "variant": "question",
        "outcome": "discarded",
        "scripts": [],
    }, ensure_ascii=False), encoding="utf-8")

    (tmp_path / "state.json").write_text(json.dumps(
        {"mode": "idle", "offset": 42, "last_snapshot": "2026-08-08"}), encoding="utf-8")
    (tmp_path / "say.json").write_text(json.dumps(
        {"ทีเอไอ": "ไทย"}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "history.json").write_text(json.dumps([
        {"video_id": "vid1", "title": "RAM หายไปไหน", "topic": "ทำไม container กิน RAM",
         "uploaded_at": "2026-08-01T13:00:00"}]), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def config_dir(tmp_path, monkeypatch):
    """The one volume the dashboard may write. Empty: no schedule stored yet."""
    where = tmp_path / "config"
    where.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(where))
    return where


@pytest.fixture()
def client(data_dir, config_dir):
    """A client whose modules were imported *after* DATA_DIR was set."""
    from app import history, manifest, schedule
    for module in (manifest, history, schedule):
        importlib.reload(module)
    from app import dashboard
    importlib.reload(dashboard)
    return TestClient(dashboard.app)


def test_healthz(client):
    reply = client.get("/healthz")
    assert reply.status_code == 200
    assert reply.json() == {"ok": True}


# The single exception docs/adr/0009 grants, named here so adding a second one
# is a test failure rather than a judgement call.
WRITING_ROUTES = {"/settings"}


def test_only_the_schedule_route_can_write(client):
    """Read-only is a property of the code, not only of the :ro mount.

    Amended by docs/adr/0009 from "no route writes" to "one route writes, and
    it is this one". The list above is the whole permission: a new POST
    anywhere else fails here before anyone has to notice it in review.
    """
    from app import dashboard
    for route in dashboard.app.routes:
        allowed = getattr(route, "methods", set()) or set()
        if allowed <= {"GET", "HEAD"}:
            continue
        assert route.path in WRITING_ROUTES, f"{route.path} allows {allowed}"
        assert allowed <= {"POST"}, f"{route.path} allows {allowed}"


def test_the_writing_route_cannot_reach_data(client, data_dir, config_dir):
    """It writes to /config and nothing under /data, which is mounted :ro."""
    from app import schedule
    before = sorted(p.name for p in data_dir.iterdir())
    reply = client.post("/settings", data={
        "th_enabled": "on", "th_hours": "8,12", "th_minutes": "15",
        "en_hours": "20", "en_minutes": "15",
    })
    assert reply.status_code == 200
    assert (config_dir / "schedule.json").exists()
    assert sorted(p.name for p in data_dir.iterdir()) == before
    assert schedule.settings()["th"]["hours"] == [8, 12]
    assert schedule.settings()["en"]["enabled"] is False


def test_a_bad_schedule_is_rejected_whole(client, config_dir):
    """Half a schedule applied is the half nobody checked."""
    from app import schedule
    schedule.save({"th": {"enabled": True, "hours": [8], "auto_pick_minutes": 15}})
    reply = client.post("/settings", data={
        "th_enabled": "on", "th_hours": "8,99", "th_minutes": "15",
        "en_hours": "", "en_minutes": "15",
    })
    assert reply.status_code == 400
    assert "0-23" in reply.text
    assert schedule.settings()["th"]["hours"] == [8], "the stored schedule must not move"


def test_the_settings_page_shows_what_is_stored(client):
    from app import schedule
    schedule.save({"en": {"enabled": True, "hours": [21], "auto_pick_minutes": 30}})
    body = client.get("/settings").text
    assert 'value="21"' in body
    assert 'value="30"' in body


def test_clips_page_lists_newest_first(client):
    reply = client.get("/")
    assert reply.status_code == 200
    body = reply.text
    assert "หัวข้อที่ถูกทิ้ง" in body
    assert "ทำไม container กิน RAM" in body
    assert body.index("หัวข้อที่ถูกทิ้ง") < body.index("ทำไม container กิน RAM")


def test_clips_page_shows_day7_numbers(client):
    body = client.get("/").text
    assert "182" in body      # views from the day-7 snapshot
    assert "41.2" in body     # its retention percent


def test_clips_page_survives_a_broken_manifest(client, data_dir):
    (data_dir / "clips" / "broken.json").write_text("{ not json", encoding="utf-8")
    assert client.get("/").status_code == 200


def test_clip_page_shows_every_draft_and_the_snapshots(client):
    body = client.get("/clip/20260801-120000-000").text
    assert "RAM หายไปไหน" in body       # the draft's title
    assert "การ์ดหนึ่ง" in body          # the card narration
    assert "182" in body                # the snapshot row
    assert "vid1" in body               # the YouTube link


def test_clip_page_of_a_drafting_manifest_is_not_an_error(client):
    assert client.get("/clip/20260802-120000-000").status_code == 200


def test_unknown_clip_is_a_404_page_not_a_traceback(client):
    reply = client.get("/clip/nope")
    assert reply.status_code == 404
    assert "ไม่พบ" in reply.text


def test_experiment_page_shows_both_arms_and_a_verdict(client):
    from app import experiment
    body = client.get("/experiment").text
    for name in experiment.VARIANTS:
        assert name in body
    assert "สรุปไม่ได้" in body     # two clips is far below the threshold


def test_now_page_reads_state_and_say(client):
    body = client.get("/now").text
    assert "idle" in body
    assert "ไทย" in body            # the say.json substitution


def test_now_page_without_state_file(client, data_dir):
    (data_dir / "state.json").unlink()
    reply = client.get("/now")
    assert reply.status_code == 200


def test_no_drawing_library_in_this_process():
    """docs/adr/0007: Pillow and edge-tts stay out of the LAN-facing process.

    `test_no_route_can_write` only checks HTTP methods, so a chart drawn with
    app.retention's Pillow code would pass every other test in this file. Run
    in a subprocess because a full-suite run imports app.render long before
    this point, which would make an in-process sys.modules check meaningless.
    """
    import subprocess
    import sys
    subprocess.run(
        [sys.executable, "-c",
         "import app.dashboard, sys;"
         " assert 'PIL' not in sys.modules, 'Pillow reached the dashboard';"
         " assert 'edge_tts' not in sys.modules, 'edge-tts reached the dashboard'"],
        check=True,
    )


def test_clips_page_leads_with_the_gate_and_the_day7_figures(client):
    body = client.get("/").text
    assert ">1<small> / 30</small>" in body   # one clip in history.json, Gate is 30
    assert "41.2" in body          # median day-7 retention across the list


def test_clips_rows_carry_their_outcome_for_the_filter(client):
    body = client.get("/").text
    assert 'data-outcome="rendered"' in body
    assert 'data-outcome="discarded"' in body


def test_clip_chart_needs_two_snapshots(client, data_dir):
    from app import dashboard
    assert dashboard._chart([{"age_days": 7, "views": 182}]) is None
    chart = dashboard._chart([
        {"age_days": 1, "views": 0}, {"age_days": 7, "views": 182}])
    assert chart["top"] == 182
    assert len(chart["line"].split()) == 2


def test_now_page_still_shows_a_key_it_does_not_know_about(client, data_dir):
    """A dashboard that only rendered known keys would hide every new one."""
    (data_dir / "state.json").write_text(json.dumps(
        {"mode": "idle", "a_key_invented_tomorrow": "xyzzy"}), encoding="utf-8")
    assert "xyzzy" in client.get("/now").text
