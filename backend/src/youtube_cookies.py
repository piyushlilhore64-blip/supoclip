"""Turn pasted YouTube cookies into a cookie file yt-dlp can use.

YouTube answers most datacenter IPs with "Sign in to confirm you're not a bot".
yt-dlp cannot answer that gate anonymously, so metadata lookups and media
downloads both fail from a hosted server. A browser session's cookies are the
free fix: yt-dlp sends them and YouTube stops asking.

``YOUTUBE_COOKIES`` (environment or the admin setting) accepts three shapes:

* Netscape ``cookies.txt`` text, as browser extensions export it
* base64 of that text, because one line survives a single-line form field
* a JSON array, as Cookie-Editor and EditThisCookie export it

Only a signed-in session helps, so input without YouTube's session cookies is
rejected with a message that says what to export instead of failing later with
the same bot check.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import get_config

logger = logging.getLogger(__name__)

# Exceeding this means the value is not a cookie export.
_MAX_COOKIE_INPUT_BYTES = 512 * 1024

# Cookies YouTube issues to a signed-in browser. Without at least one of these
# the file is a visitor session, which does not pass the bot check.
_SESSION_COOKIE_NAMES = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "LOGIN_INFO",
        "SIDCC",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "__Secure-1PSIDCC",
        "__Secure-3PSIDCC",
    }
)

_YOUTUBE_COOKIE_DOMAINS = ("youtube.com", "google.com")

_NETSCAPE_HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# Exported for SupoClip. Rows are: domain, includeSubdomains, path, "
    "secure, expiry, name, value\n"
)


class YouTubeCookiesError(ValueError):
    """The configured cookie value is present but not usable."""


def _looks_like_json_array(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("[") and stripped.rstrip().endswith("]")


def _looks_like_netscape(text: str) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # domain \t includeSubdomains \t path \t secure \t expiry \t name \t value
        if len(line.split("\t")) >= 7:
            return True
    return False


def _maybe_decode_base64(text: str) -> Optional[str]:
    """Decode a single-line base64 cookie export, or return None.

    ``validate=False`` keeps '-'/'_' and a missing '=' padding from making a
    plain cookie value look decodable.
    """
    candidate = "".join(text.split())
    if not candidate:
        return None
    if _looks_like_netscape(candidate):
        return None
    try:
        raw = base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Only accept the decode when the result is actually a cookie export;
    # otherwise a long cookie value would be silently mangled.
    if _looks_like_json_array(decoded) or _looks_like_netscape(decoded):
        return decoded
    return None


def _parse_netscape(text: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _include_subdomains, path, secure, expiry, name, value = parts[:7]
        cookies.append(
            {
                "domain": domain,
                "path": path or "/",
                "secure": secure.strip().upper() == "TRUE",
                "expiry": expiry,
                "name": name,
                "value": value,
            }
        )
    return cookies


def _parse_json(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise YouTubeCookiesError(
            "The YouTube cookies value looks like JSON but could not be parsed. "
            "Export cookies as a Netscape cookies.txt file and paste it again."
        ) from exc

    if isinstance(payload, dict):
        # Some exporters wrap the list under a key.
        payload = payload.get("cookies") or payload.get("items") or []
    if not isinstance(payload, list):
        raise YouTubeCookiesError(
            "The YouTube cookies value must be a cookies.txt file, base64 of one, "
            "or a JSON cookie array."
        )

    cookies: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        domain = entry.get("domain")
        if not isinstance(name, str) or not isinstance(domain, str):
            continue
        expiry = entry.get("expirationDate", entry.get("expiry", entry.get("expires")))
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float, str)):
            expiry = 0
        cookies.append(
            {
                "domain": domain,
                "path": str(entry.get("path") or "/"),
                "secure": bool(entry.get("secure")),
                "expiry": int(float(expiry)) if str(expiry).strip() not in {"", "None"} else 0,
                "name": name,
                "value": str(entry.get("value") or ""),
            }
        )
    return cookies


def parse_cookies(raw: str) -> list[dict[str, Any]]:
    """Parse any supported cookie export into cookie records."""
    if raw is None:
        raise YouTubeCookiesError("No YouTube cookies were provided.")
    text = raw.strip()
    if not text:
        raise YouTubeCookiesError("No YouTube cookies were provided.")
    if len(text.encode("utf-8", errors="ignore")) > _MAX_COOKIE_INPUT_BYTES:
        raise YouTubeCookiesError(
            "The YouTube cookies value is too large to be a cookie export."
        )

    decoded = _maybe_decode_base64(text)
    if decoded is not None:
        text = decoded.strip()

    if _looks_like_json_array(text):
        return _parse_json(text)
    return _parse_netscape(text)


def _is_youtube_cookie(domain: str) -> bool:
    host = (domain or "").lstrip(".").lower()
    if not host:
        return False
    if any(host == suffix or host.endswith("." + suffix) for suffix in _YOUTUBE_COOKIE_DOMAINS):
        return True
    # ``#HttpOnly_.youtube.com`` appears in some exports.
    return any(suffix in host for suffix in _YOUTUBE_COOKIE_DOMAINS)


def validate_cookies(cookies: Iterable[dict[str, Any]]) -> None:
    """Reject exports that cannot pass YouTube's bot check."""
    records = [cookie for cookie in cookies if cookie.get("name")]
    if not records:
        raise YouTubeCookiesError(
            "No cookies were found in the YouTube cookies value. Paste a "
            "cookies.txt export from a browser signed in to YouTube."
        )
    if not any(_is_youtube_cookie(str(cookie.get("domain", ""))) for cookie in records):
        raise YouTubeCookiesError(
            "These cookies contain no youtube.com or google.com entries. "
            "Export cookies while youtube.com is open."
        )
    if not any(str(cookie.get("name")) in _SESSION_COOKIE_NAMES for cookie in records):
        raise YouTubeCookiesError(
            "These cookies are not a signed-in YouTube session, so YouTube would "
            "still ask for sign-in. Sign in to youtube.com in the browser, then "
            "export the cookies again."
        )


def _netscape_text(cookies: Iterable[dict[str, Any]]) -> str:
    lines = [_NETSCAPE_HEADER]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expiry = cookie.get("expiry") or 0
        lines.append(
            "\t".join(
                [
                    domain,
                    include_subdomains,
                    str(cookie.get("path") or "/"),
                    secure,
                    str(expiry),
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def cookie_directory(temp_dir: Optional[str] = None) -> Path:
    base = Path(temp_dir or get_config().temp_dir)
    directory = base / "youtube-cookies"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def write_cookie_file(raw: str, temp_dir: Optional[str] = None) -> Path:
    """Write the parsed cookies to a private file and return its path.

    The name is derived from the content, so a repeated lookup reuses the same
    file and a changed setting writes a new one.
    """
    cookies = parse_cookies(raw)
    validate_cookies(cookies)
    text = _netscape_text(cookies)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    directory = cookie_directory(temp_dir)
    destination = directory / f"youtube-{digest}.txt"
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    temporary = directory / f".youtube-{digest}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    # Old exports are no longer referenced; keep the directory small.
    for stale in directory.glob("youtube-*.txt"):
        if stale.name != destination.name:
            try:
                stale.unlink()
            except OSError:
                pass
    return destination


_unusable_value: Optional[str] = None


def cookie_setting() -> Optional[str]:
    """The raw configured cookie value, or None when unset."""
    return get_config().youtube_cookies or None


def last_cookie_error() -> Optional[str]:
    """Why the configured cookies were rejected, if they were."""
    return _unusable_value


def youtube_cookiefile(temp_dir: Optional[str] = None) -> Optional[str]:
    """Path to a usable cookie file, or None when cookies are unset/unusable.

    A bad value never breaks a download: yt-dlp runs without cookies and the
    caller's error message explains that the configured cookies were rejected.
    """
    global _unusable_value

    raw = cookie_setting()
    if not raw:
        _unusable_value = None
        return None
    try:
        path = write_cookie_file(raw, temp_dir)
    except YouTubeCookiesError as exc:
        if str(exc) != _unusable_value:
            logger.warning("Ignoring unusable YouTube cookies setting: %s", exc)
        _unusable_value = str(exc)
        return None
    except OSError as exc:
        if str(exc) != _unusable_value:
            logger.warning("Could not write the YouTube cookie file: %s", exc)
        _unusable_value = (
            "The YouTube cookies could not be saved to a temporary file. "
            "Check that TEMP_DIR is writable."
        )
        return None
    _unusable_value = None
    return str(path)


def cookies_configured() -> bool:
    return bool(cookie_setting())
