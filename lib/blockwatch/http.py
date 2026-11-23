"""Bounded HTTPS access for the Blockwatch helper.

One function, used by every provider:
  - https only (http is allowed for loopback wallet RPC endpoints)
  - no credentials in the URL, no cross-host redirects
  - connect/read timeout, response byte cap
  - one retry for 429, honouring Retry-After within a small cap
  - provider errors become BlockwatchError with a short, plain reason
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .util import BlockwatchError, plain

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_BYTES = 2_000_000
USER_AGENT = f"blockwatch/{__version__} (read-only address watcher)"


class HttpError(BlockwatchError):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def checked_url(url):
    """Reject schemes and shapes the helper will not talk to."""
    if not isinstance(url, str):
        raise BlockwatchError("endpoint is not text")
    url = url.strip()
    if len(url) > 500:
        raise BlockwatchError("endpoint is too long")
    if any(ch < " " or ch == "\x7f" for ch in url) or any(ch.isspace() for ch in url):
        raise BlockwatchError("endpoint contains whitespace or control characters")
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        raise BlockwatchError("endpoint has no host")
    if parts.username or parts.password:
        raise BlockwatchError("endpoint must not contain credentials")
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return url
    raise BlockwatchError("endpoint must use https (http is allowed for loopback only)")


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the same origin, never down to http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        old_port = old.port or (443 if old.scheme == "https" else 80)
        new_port = new.port or (443 if new.scheme == "https" else 80)
        if (new.scheme, new.hostname, new_port) != (old.scheme, old.hostname, old_port):
            raise urllib.error.HTTPError(newurl, code, "cross-origin redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_RedirectGuard())


def _error_text(body, status):
    """A short plain reason from a provider error body."""
    snippet = body.decode("utf-8", "replace").strip()
    if snippet:
        try:
            document = json.loads(snippet)
        except ValueError:
            document = None
        if isinstance(document, dict):
            for key in ("message", "error", "detail"):
                value = document.get(key)
                if isinstance(value, str) and value.strip():
                    snippet = value.strip()
                    break
            else:
                context = document.get("context")
                if isinstance(context, dict) and isinstance(context.get("error"), str):
                    snippet = context["error"].strip()
    snippet = "".join(ch for ch in snippet if ch >= " " and ch != "\x7f")[:200]
    if not snippet:
        snippet = urllib.error.HTTPStatus(status).phrase if status in urllib.error.HTTPStatus._value2member_map_ else "request failed"
    return plain(snippet, 200)


def _perform(request, timeout, max_bytes):
    """One request/response, mapped to BlockwatchError. Returns raw bytes."""
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        try:
            detail = error.read(4096)
        except Exception:  # noqa: BLE001 - the body is optional context
            detail = b""
        retry_after = error.headers.get("Retry-After") if error.headers else None
        raise HttpError(error.code, _error_text(detail, error.code), retry_after) from None
    except urllib.error.URLError as error:
        raise BlockwatchError(f"network error: {error.reason}") from None
    except TimeoutError:
        raise BlockwatchError("network timeout") from None
    except OSError as error:
        raise BlockwatchError(f"network error: {error.strerror or error}") from None
    if len(raw) > max_bytes:
        raise BlockwatchError("response is larger than the allowed limit")
    return raw


def _retry_delay(value):
    try:
        return max(0.0, min(5.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def request_json(url, method="GET", payload=None, headers=None, timeout=DEFAULT_TIMEOUT,
                 max_bytes=DEFAULT_MAX_BYTES, auth=None, retry_429=True):
    """Fetch one JSON document. Returns the decoded value; raises BlockwatchError."""
    url = checked_url(url)
    request_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
    }
    if headers:
        request_headers.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if auth:
        import base64

        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode("utf-8")).decode("ascii")
        request_headers["Authorization"] = "Basic " + token

    attempts = 2 if retry_429 else 1
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            raw = _perform(request, timeout, max_bytes)
        except HttpError as error:
            if error.status == 429 and attempt + 1 < attempts:
                time.sleep(_retry_delay(error.retry_after))
                continue
            raise
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BlockwatchError("provider returned invalid JSON") from None
        return document

    raise BlockwatchError("request failed")


def path_segment(value):
    return urllib.parse.quote(str(value), safe="")


def with_query(url, key, value):
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append((key, value))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(query), parts.fragment))
