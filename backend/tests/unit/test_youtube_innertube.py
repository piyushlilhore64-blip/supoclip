"""The player-endpoint probe that keeps the preflight off the CPU."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from src import youtube_innertube


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _player_payload(*, status="OK", reason=None, length="213", details=True):
    payload = {"playabilityStatus": {"status": status}}
    if reason is not None:
        payload["playabilityStatus"]["reason"] = reason
    if details:
        payload["videoDetails"] = {
            "videoId": "dQw4w9WgXcQ",
            "title": "A title",
            "lengthSeconds": length,
            "author": "A channel",
            "viewCount": "1234",
            "shortDescription": "desc",
            "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/small.jpg"}, {"url": "https://i.ytimg.com/big.jpg"}]},
        }
    return payload


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("the test must not reach the network")

    monkeypatch.setattr(youtube_innertube.requests, "post", boom)


def _serve(monkeypatch, response):
    monkeypatch.setattr(youtube_innertube.requests, "post", lambda *a, **k: response)


def test_player_result_returns_duration_and_title(monkeypatch):
    _serve(monkeypatch, _Response(payload=_player_payload()))

    result = youtube_innertube.fetch_player_result("dQw4w9WgXcQ")

    assert result["status"] == youtube_innertube.STATUS_OK
    assert result["video"]["duration"] == 213.0
    assert result["video"]["title"] == "A title"
    assert result["video"]["uploader"] == "A channel"
    assert result["video"]["view_count"] == 1234
    assert result["video"]["thumbnail"] == "https://i.ytimg.com/big.jpg"


def test_login_required_is_reported_as_the_bot_check(monkeypatch):
    _serve(
        monkeypatch,
        _Response(
            payload=_player_payload(
                status="LOGIN_REQUIRED",
                reason="Sign in to confirm you\u2019re not a bot",
                details=False,
            )
        ),
    )

    result = youtube_innertube.fetch_player_result("jNQXAC9IVRw")

    assert result["status"] == youtube_innertube.STATUS_BOT_CHECK
    assert "not a bot" in result["reason"]


def test_age_gate_and_geo_are_separate(monkeypatch):
    _serve(monkeypatch, _Response(payload=_player_payload(status="AGE_VERIFICATION_REQUIRED", details=False)))
    assert youtube_innertube.fetch_player_result("x")["status"] == youtube_innertube.STATUS_AGE_RESTRICTED

    _serve(
        monkeypatch,
        _Response(payload=_player_payload(status="UNPLAYABLE", reason="The uploader has not made this video available in your country", details=False)),
    )
    assert youtube_innertube.fetch_player_result("x")["status"] == youtube_innertube.STATUS_GEO_RESTRICTED


def test_non_json_and_server_errors_are_fallbacks(monkeypatch):
    _serve(monkeypatch, _Response(text="<html>"))
    with pytest.raises(youtube_innertube.InnertubeUnavailable):
        youtube_innertube.fetch_player_result("x")

    _serve(monkeypatch, _Response(status_code=503, text="busy"))
    with pytest.raises(youtube_innertube.InnertubeUnavailable):
        youtube_innertube.fetch_player_result("x")


def test_expired_deadline_skips_the_call(monkeypatch):
    _serve(monkeypatch, _Response(payload=_player_payload()))

    with pytest.raises(youtube_innertube.InnertubeUnavailable):
        youtube_innertube.fetch_player_result("x", deadline=0.0)


def test_cookie_header_uses_the_materialised_file(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PSID\tpsid-value\n"
        ".youtube.com\tTRUE\t/\tFALSE\t1893456000\tVISITOR_INFO1_LIVE\tvisitor\n",
        encoding="utf-8",
    )

    header = youtube_innertube.cookie_header(str(cookie_file))

    assert header == "__Secure-3PSID=psid-value; VISITOR_INFO1_LIVE=visitor"
    assert youtube_innertube.cookie_header(None) is None
    assert youtube_innertube.cookie_header(str(tmp_path / "missing.txt")) is None


def test_cookies_reach_the_player_request(monkeypatch, tmp_path):
    seen = {}

    def fake_post(_url, **kwargs):
        seen.update(kwargs.get("headers") or {})
        return _Response(payload=_player_payload())

    monkeypatch.setattr(youtube_innertube.requests, "post", fake_post)
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsid-value\n",
        encoding="utf-8",
    )

    youtube_innertube.fetch_player_result("dQw4w9WgXcQ", cookie_file=str(cookie_file))

    assert "SID=sid-value" in seen.get("cookie", "")


def test_preflight_probe_maps_statuses(monkeypatch):
    from src import youtube_utils
    from src.youtube_proxy import (
        YouTubeAgeRestrictedError,
        YouTubeBotCheckError,
        YouTubeGeoRestrictedError,
    )

    from src.config import Config

    monkeypatch.setattr(youtube_utils, "proxy_configured", lambda: False)
    monkeypatch.setattr(Config, "resolve_youtube_data_api_key", lambda self: None)

    monkeypatch.setattr(
        youtube_utils.youtube_innertube,
        "fetch_player_result",
        lambda *_a, **_k: {"status": youtube_innertube.STATUS_OK, "video": {"duration": 12.0}},
    )
    assert youtube_utils.fetch_youtube_player_metadata("https://youtu.be/dQw4w9WgXcQ") == {"duration": 12.0}

    monkeypatch.setattr(
        youtube_utils.youtube_innertube,
        "fetch_player_result",
        lambda *_a, **_k: {"status": youtube_innertube.STATUS_BOT_CHECK, "reason": "Sign in to confirm"},
    )
    with pytest.raises(YouTubeBotCheckError):
        youtube_utils.fetch_youtube_player_metadata("https://youtu.be/dQw4w9WgXcQ")

    monkeypatch.setattr(
        youtube_utils.youtube_innertube,
        "fetch_player_result",
        lambda *_a, **_k: {"status": youtube_innertube.STATUS_AGE_RESTRICTED},
    )
    with pytest.raises(YouTubeAgeRestrictedError):
        youtube_utils.fetch_youtube_player_metadata("https://youtu.be/dQw4w9WgXcQ")

    monkeypatch.setattr(
        youtube_utils.youtube_innertube,
        "fetch_player_result",
        lambda *_a, **_k: {"status": youtube_innertube.STATUS_GEO_RESTRICTED},
    )
    with pytest.raises(YouTubeGeoRestrictedError):
        youtube_utils.fetch_youtube_player_metadata("https://youtu.be/dQw4w9WgXcQ")

    monkeypatch.setattr(
        youtube_utils.youtube_innertube,
        "fetch_player_result",
        lambda *_a, **_k: {"status": youtube_innertube.STATUS_UNAVAILABLE, "reason": "private"},
    )
    assert youtube_utils.fetch_youtube_player_metadata("https://youtu.be/dQw4w9WgXcQ") is None

    monkeypatch.setattr(
        youtube_utils.youtube_innertube,
        "fetch_player_result",
        lambda *_a, **_k: (_ for _ in ()).throw(youtube_innertube.InnertubeUnavailable("boom")),
    )
    assert youtube_utils.fetch_youtube_player_metadata("https://youtu.be/dQw4w9WgXcQ") is None


def test_proxy_or_data_api_paths_skip_the_probe(monkeypatch):
    from src import youtube_utils
    from src.config import Config

    called = []
    monkeypatch.setattr(
        youtube_utils.youtube_innertube,
        "fetch_player_result",
        lambda *_a, **_k: called.append(1) or {"status": youtube_innertube.STATUS_OK, "video": {}},
    )

    monkeypatch.setattr(youtube_utils, "proxy_configured", lambda: True)
    assert youtube_utils.fetch_youtube_player_metadata("https://youtu.be/dQw4w9WgXcQ") is None

    monkeypatch.setattr(youtube_utils, "proxy_configured", lambda: False)
    monkeypatch.setattr(Config, "resolve_youtube_data_api_key", lambda self: "key")
    assert youtube_utils.fetch_youtube_player_metadata("https://youtu.be/dQw4w9WgXcQ") is None
    assert called == []

    assert youtube_utils.fetch_youtube_player_metadata("not a youtube link") is None


def test_the_preflight_passes_the_gate_through(monkeypatch):
    """A missing import here used to turn every gate into "couldn't verify"."""
    from src.api.routes import tasks
    from src.youtube_proxy import YouTubeBotCheckError

    def _gate(*_args, **_kwargs):
        raise YouTubeBotCheckError("Sign in to confirm you\u2019re not a bot")

    monkeypatch.setattr(tasks, "fetch_youtube_player_metadata", _gate)

    with pytest.raises(YouTubeBotCheckError):
        asyncio.run(
            tasks._read_youtube_metadata("https://www.youtube.com/watch?v=jNQXAC9IVRw", 30.0)
        )


def test_a_failed_preflight_names_its_exception(monkeypatch, caplog):
    from src.api.routes import tasks

    async def _nothing(_url, _timeout):
        raise RuntimeError("all providers refused")

    monkeypatch.setattr(tasks, "_read_youtube_metadata", _nothing)

    with caplog.at_level("WARNING", logger="src.api.routes.tasks"):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                tasks._preflight_youtube_source("https://www.youtube.com/watch?v=jNQXAC9IVRw", 3600)
            )

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "VIDEO_METADATA_UNAVAILABLE"
    assert "RuntimeError: all providers refused" in caplog.text
