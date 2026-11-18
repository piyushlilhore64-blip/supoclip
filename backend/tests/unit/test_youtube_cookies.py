import base64
import json
import os
import stat

import pytest

from src.youtube_cookies import (
    YouTubeCookiesError,
    last_cookie_error,
    parse_cookies,
    write_cookie_file,
    youtube_cookiefile,
)

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PSID\tsecret-psid\n"
    ".youtube.com\tTRUE\t/\tFALSE\t1893456000\tVISITOR_INFO1_LIVE\tvisitor\n"
    ".google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsecret-sid\n"
)

JSON_COOKIES = json.dumps(
    [
        {
            "domain": ".youtube.com",
            "name": "SAPISID",
            "value": "secret-sapisid",
            "path": "/",
            "secure": True,
            "expirationDate": 1893456000,
        },
        {
            "domain": ".youtube.com",
            "name": "PREF",
            "value": "hl=en",
            "path": "/",
            "secure": True,
        },
    ]
)


def test_netscape_text_is_accepted():
    cookies = parse_cookies(NETSCAPE)

    assert {cookie["name"] for cookie in cookies} == {
        "__Secure-3PSID",
        "VISITOR_INFO1_LIVE",
        "SID",
    }


def test_base64_wrapped_netscape_is_accepted():
    encoded = base64.b64encode(NETSCAPE.encode("utf-8")).decode("ascii")

    cookies = parse_cookies(encoded)

    assert any(cookie["name"] == "SID" for cookie in cookies)


def test_json_array_is_accepted():
    cookies = parse_cookies(JSON_COOKIES)

    assert [cookie["name"] for cookie in cookies] == ["SAPISID", "PREF"]
    assert cookies[0]["expiry"] == 1893456000
    assert cookies[1]["expiry"] == 0


def test_cookie_file_is_written_once_with_private_permissions(tmp_path):
    path = write_cookie_file(NETSCAPE, str(tmp_path))

    written = path.read_text(encoding="utf-8")
    assert "__Secure-3PSID\tsecret-psid" in written
    assert "SID\tsecret-sid" in written
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert write_cookie_file(NETSCAPE, str(tmp_path)) == path


def test_a_newer_export_replaces_the_previous_file(tmp_path):
    first = write_cookie_file(NETSCAPE, str(tmp_path))
    second = write_cookie_file(
        NETSCAPE.replace("secret-psid", "rotated-psid"), str(tmp_path)
    )

    assert first != second
    assert not first.exists()
    assert "rotated-psid" in second.read_text(encoding="utf-8")


def test_export_without_youtube_domain_is_rejected():
    only_other_sites = ".example.com\tTRUE\t/\tFALSE\t1893456000\tSID\tx\n"

    with pytest.raises(YouTubeCookiesError, match="youtube.com"):
        write_cookie_file(only_other_sites)


def test_visitor_only_export_is_rejected_as_signed_out():
    visitor_only = ".youtube.com\tTRUE\t/\tFALSE\t1893456000\tVISITOR_INFO1_LIVE\tv\n"

    with pytest.raises(YouTubeCookiesError, match="signed-in"):
        write_cookie_file(visitor_only)


def test_unset_setting_yields_no_cookie_file(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES", raising=False)

    assert youtube_cookiefile() is None
    assert last_cookie_error() is None


def test_configured_setting_yields_a_file_and_reports_bad_values(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_COOKIES", NETSCAPE)

    path = youtube_cookiefile(str(tmp_path))

    assert path is not None and os.path.isfile(path)
    assert last_cookie_error() is None

    monkeypatch.setenv("YOUTUBE_COOKIES", "not a cookie export")

    assert youtube_cookiefile(str(tmp_path)) is None
    assert "cookies.txt" in (last_cookie_error() or "")


def test_bot_check_detection_uses_ytdlp_wording():
    from src.youtube_proxy import is_bot_check_error

    ytdlp_curly = (
        "ERROR: [youtube] buNO0FkOcCc: Sign in to confirm you\u2019re not a bot. "
        "This helps protect our community."
    )

    assert is_bot_check_error(ytdlp_curly)
    assert is_bot_check_error("The provided YouTube account cookies are no longer valid")
    assert not is_bot_check_error("HTTP Error 403: Forbidden")
    assert not is_bot_check_error("This video is not available in your country")


def test_ytdlp_options_carry_the_cookie_file(tmp_path, monkeypatch):
    from src.youtube_utils import YouTubeDownloader, _build_info_options

    monkeypatch.setenv("YOUTUBE_COOKIES", NETSCAPE)
    monkeypatch.setenv("TEMP_DIR", str(tmp_path))

    info_options = _build_info_options()
    download_options = YouTubeDownloader().get_optimal_download_options("abc12345678")

    assert os.path.isfile(info_options["cookiefile"])
    assert info_options["cookiefile"] == download_options["cookiefile"]
