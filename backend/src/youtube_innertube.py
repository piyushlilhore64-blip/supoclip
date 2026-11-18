"""YouTube metadata over YouTube's own JSON player endpoint.

Running ``yt-dlp`` just to read a length is expensive: on a small instance the
player clients and their JS challenges can hold the CPU long past the preflight
deadline, so the API freezes and the user sees a timeout instead of an answer.

YouTube's player endpoint returns the same ``videoDetails`` (title, length,
channel) as one small HTTPS POST. It answers in well under a second, and its
``playabilityStatus`` states plainly whether the request was asked to sign in,
which is exactly the difference between "add cookies" and "try again".

The endpoint is a private YouTube API. Every failure here is non-fatal: the
caller falls back to ``yt-dlp``, so a rotated key or a changed response only
costs the speed-up.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

from .youtube_cookies import parse_cookies

logger = logging.getLogger(__name__)

# The public WEB client key the youtube.com page itself uses.
_INNERTUBE_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_CLIENT_NAME = "WEB"
_CLIENT_VERSION = "2.20241202.01.00"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_ENDPOINT = "https://www.youtube.com/youtubei/v1/player"

# Short on purpose: this is a convenience path, not the only one.
_DEFAULT_TIMEOUT_SECONDS = 6.0

STATUS_OK = "ok"
STATUS_BOT_CHECK = "bot_check"
STATUS_AGE_RESTRICTED = "age_restricted"
STATUS_GEO_RESTRICTED = "geo_restricted"
STATUS_UNAVAILABLE = "unavailable"


class InnertubeUnavailable(RuntimeError):
    """The player endpoint did not answer usefully; the caller should fall back."""


def cookie_header(cookie_file: Optional[str]) -> Optional[str]:
    """Build a ``Cookie`` header from a Netscape cookie file, or None."""
    if not cookie_file:
        return None
    try:
        with open(cookie_file, "r", encoding="utf-8", errors="ignore") as handle:
            records = parse_cookies(handle.read())
    except (OSError, ValueError) as exc:
        logger.debug("Could not read YouTube cookies for the player probe: %s", exc)
        return None
    pairs = [
        f"{record['name']}={record['value']}"
        for record in records
        if record.get("name") and record.get("value") is not None
    ]
    return "; ".join(pairs) or None


def fetch_player_result(
    video_id: str,
    *,
    cookie_file: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """Ask the player endpoint about one video.

    Returns ``{"status": ..., "reason": ..., "video": {...}}``. Raises
    ``InnertubeUnavailable`` when the endpoint cannot be used at all, so the
    caller can fall back to ``yt-dlp`` without changing its own error handling.
    """
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InnertubeUnavailable("no time left for the player probe")
        timeout = min(timeout, max(1.0, remaining))

    body = {
        "context": {
            "client": {
                "clientName": _CLIENT_NAME,
                "clientVersion": _CLIENT_VERSION,
                "hl": "en",
                "gl": "US",
                "userAgent": _USER_AGENT,
            }
        },
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
    }
    headers = {
        "content-type": "application/json",
        "user-agent": _USER_AGENT,
        "x-youtube-client-name": "1",
        "x-youtube-client-version": _CLIENT_VERSION,
        "origin": "https://www.youtube.com",
    }
    cookies = cookie_header(cookie_file)
    if cookies:
        headers["cookie"] = cookies

    try:
        response = requests.post(
            _ENDPOINT,
            params={"key": _INNERTUBE_API_KEY, "prettyPrint": "false"},
            json=body,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise InnertubeUnavailable(str(exc)) from exc

    if response.status_code >= 500:
        raise InnertubeUnavailable(f"player endpoint answered {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise InnertubeUnavailable("player endpoint answered with non-JSON") from exc
    if not isinstance(payload, dict):
        raise InnertubeUnavailable("player endpoint answered with an unexpected body")

    playability = payload.get("playabilityStatus") or {}
    details = payload.get("videoDetails") or {}
    status = str(playability.get("status") or "")
    reason = str(playability.get("reason") or "")
    length = details.get("lengthSeconds")

    video: Dict[str, Any] = {
        "id": details.get("videoId"),
        "title": details.get("title"),
        "description": str(details.get("shortDescription") or ""),
        "duration": length,
        "uploader": details.get("author"),
        "upload_date": None,
        "view_count": details.get("viewCount"),
        "like_count": None,
        "thumbnail": _thumbnail(details),
        "format_id": None,
        "resolution": None,
        "fps": None,
        "filesize": None,
        "age_restricted": _is_age_gate(status, reason),
    }
    try:
        if length is not None:
            video["duration"] = float(length)
            video["view_count"] = _as_int(details.get("viewCount"))
    except (TypeError, ValueError):
        video["duration"] = None

    lowered = reason.lower()
    if video["duration"] is not None:
        return {"status": STATUS_OK, "reason": reason, "video": video}
    if status == "LOGIN_REQUIRED" or "not a bot" in lowered:
        return {"status": STATUS_BOT_CHECK, "reason": reason, "video": video}
    if _is_age_gate(status, reason):
        return {"status": STATUS_AGE_RESTRICTED, "reason": reason, "video": video}
    if "country" in lowered:
        return {"status": STATUS_GEO_RESTRICTED, "reason": reason, "video": video}
    return {"status": STATUS_UNAVAILABLE, "reason": reason or status, "video": video}


def _thumbnail(details: Dict[str, Any]) -> Optional[str]:
    thumbnails = (details.get("thumbnail") or {}).get("thumbnails") or []
    for entry in reversed(thumbnails):
        url = entry.get("url") if isinstance(entry, dict) else None
        if url:
            return str(url)
    return None


def _is_age_gate(status: str, reason: str) -> bool:
    text = f"{status} {reason}".lower()
    if "age_verification" in text or "age_check" in text:
        return True
    if "confirm your age" in text:
        return True
    return "age" in text and "restrict" in text


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
