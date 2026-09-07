"""Uploads a finished Clip to YouTube.

Deliberately no google-api-python-client: refreshing a token is one POST and a
resumable upload is two requests, and httpx is already here.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from app import locales

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CAPTIONS_URL = "https://www.googleapis.com/upload/youtube/v3/captions"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"

MAX_TITLE = 100
MAX_DESCRIPTION = 5000


class UploadError(RuntimeError):
    """The upload did not happen; the Clip is still on disk."""


def _env(name: str, locale: str = locales.DEFAULT, default: str = "") -> str:
    """One YouTube setting for one Locale.

    Each Locale publishes to its own channel (docs/adr/0008), so every
    credential is looked up under that Locale's prefix: YOUTUBE_ for Thai,
    YOUTUBE_EN_ for English. There is no shared fallback on purpose — an
    English clip that silently borrowed the Thai channel's refresh token would
    publish to the wrong audience, and nothing about the upload would say so.
    """
    return os.environ.get(locales.get(locale)["youtube_prefix"] + name, default)


def configured(locale: str = locales.DEFAULT) -> bool:
    return all(
        _env(name, locale)
        for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")
    )


async def _access_token(client: httpx.AsyncClient,
                        locale: str = locales.DEFAULT) -> str:
    reply = await client.post(
        TOKEN_URL,
        data={
            "client_id": _env("CLIENT_ID", locale),
            "client_secret": _env("CLIENT_SECRET", locale),
            "refresh_token": _env("REFRESH_TOKEN", locale),
            "grant_type": "refresh_token",
        },
    )
    if reply.status_code != 200:
        # `invalid_grant` here almost always means the consent screen is still
        # in Testing, where refresh tokens expire after 7 days.
        raise UploadError(f"ขอ access token ไม่ผ่าน ({reply.status_code}): {reply.text[:300]}")
    return reply.json()["access_token"]


async def set_thumbnail(video_id: str, image: Path,
                        locale: str = locales.DEFAULT) -> None:
    """Use `image` as the video's thumbnail.

    Custom thumbnails need a phone-verified channel; without that YouTube
    answers 403 and the video simply keeps its auto-generated thumbnail. That
    is a nuisance, not a failed upload, so this raises and the caller reports
    it without treating the clip as lost.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        token = await _access_token(client, locale)
        reply = await client.post(
            THUMBNAIL_URL,
            params={"videoId": video_id},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
            content=image.read_bytes(),
        )
    if reply.status_code not in (200, 201):
        detail = reply.text[:300]
        if reply.status_code == 403:
            raise UploadError("ช่องยังไม่ได้ยืนยันเบอร์โทร เลยตั้งปกเองไม่ได้")
        raise UploadError(f"ตั้งปกไม่สำเร็จ ({reply.status_code}): {detail}")
    logger.info("ตั้งปกให้ %s แล้ว", video_id)


async def add_captions(video_id: str, srt: Path,
                       locale: str = locales.DEFAULT) -> None:
    """Attach a subtitle track.

    Unlike the video upload this is a single multipart request, not the
    resumable flow: one JSON part for the snippet and one part for the SRT.
    Needs the `youtube.force-ssl` scope.
    """
    spec = locales.get(locale)
    snippet = {
        "snippet": {
            "videoId": video_id,
            "language": spec["captions"],
            "name": spec["label"],
            "isDraft": False,
        }
    }
    async with httpx.AsyncClient(timeout=120) as client:
        token = await _access_token(client, locale)
        reply = await client.post(
            CAPTIONS_URL,
            params={"part": "snippet", "uploadType": "multipart"},
            headers={"Authorization": f"Bearer {token}"},
            files={
                "metadata": (None, json.dumps(snippet), "application/json; charset=UTF-8"),
                "file": ("captions.srt", srt.read_bytes(), "application/octet-stream"),
            },
        )
    if reply.status_code not in (200, 201):
        raise UploadError(f"ใส่ซับไม่สำเร็จ ({reply.status_code}): {reply.text[:300]}")
    logger.info("ใส่ซับให้ %s แล้ว", video_id)


def metadata(script: dict, locale: str = locales.DEFAULT) -> dict:
    tags = [tag.lstrip("#") for tag in script.get("hashtags", [])]
    description = script.get("description", "")
    if tags:
        description = f"{description}\n\n{' '.join('#' + t for t in tags)}"
    return {
        "snippet": {
            "title": script["title"][:MAX_TITLE],
            "description": description[:MAX_DESCRIPTION],
            "tags": tags,
            "categoryId": _env("CATEGORY_ID", locale) or "28",
        },
        "status": {
            "privacyStatus": _env("PRIVACY", locale) or "public",
            "selfDeclaredMadeForKids": False,
        },
    }


async def upload(clip: Path, script: dict,
                 locale: str = locales.DEFAULT) -> tuple[str, str]:
    """Upload the Clip. Returns (video_id, the privacy status YouTube applied).

    The status is read back rather than assumed: a project that has not passed
    Google's API compliance audit has its uploads forced to `private`, and the
    only honest way to know is to look at what came back.
    """
    if not configured(locale):
        raise UploadError(
            f"ยังไม่ได้ตั้งค่าช่อง YouTube ของภาษา{locales.get(locale)['label']} "
            "(รัน scripts/youtube_auth.py ให้ช่องนั้นก่อน)"
        )

    size = clip.stat().st_size
    async with httpx.AsyncClient(timeout=600) as client:
        token = await _access_token(client, locale)
        headers = {"Authorization": f"Bearer {token}"}

        start = await client.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **headers,
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            content=json.dumps(metadata(script, locale)),
        )
        if start.status_code not in (200, 201):
            raise UploadError(f"เริ่มอัปโหลดไม่ได้ ({start.status_code}): {start.text[:300]}")

        session_url = start.headers.get("location")
        if not session_url:
            raise UploadError("YouTube ไม่ได้ส่ง upload session กลับมา")

        done = await client.put(
            session_url,
            headers={**headers, "Content-Type": "video/mp4", "Content-Length": str(size)},
            content=clip.read_bytes(),
        )
        if done.status_code not in (200, 201):
            raise UploadError(f"อัปโหลดล้มเหลว ({done.status_code}): {done.text[:300]}")

        body = done.json()
        privacy = body.get("status", {}).get("privacyStatus", "unknown")
        logger.info("อัปโหลดแล้ว: %s (%s)", body.get("id"), privacy)
        return body["id"], privacy
