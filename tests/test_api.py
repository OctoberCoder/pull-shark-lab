import io
import urllib.error

import pytest

from pull_shark import GitHubAPI, GitHubError


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_http_error(code: int, body: bytes = b'{"message":"nope"}', retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError(
        url="https://api.github.com/x", code=code, msg="err", hdrs=headers, fp=io.BytesIO(body)
    )


def test_sets_auth_and_accept_headers(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    api = GitHubAPI(token="tok_123", repo="o/r", sleeper=lambda s: None)
    assert api.request("GET", "/user") == {"ok": True}
    assert captured["headers"]["Authorization"] == "Bearer tok_123"
    assert "application/vnd.github+json" in captured["headers"]["Accept"]


def test_retries_on_500_then_succeeds(monkeypatch):
    responses = [make_http_error(500), FakeResponse(b'{"ok": true}')]
    slept = []

    def fake_urlopen(req, timeout=None):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    api = GitHubAPI(token="t", repo="o/r", sleeper=slept.append)
    assert api.request("GET", "/user") == {"ok": True}
    assert slept == [60.0]


def test_honours_retry_after_header(monkeypatch):
    responses = [make_http_error(403, retry_after=42), FakeResponse(b'{"ok": true}')]
    slept = []

    def fake_urlopen(req, timeout=None):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    api = GitHubAPI(token="t", repo="o/r", sleeper=slept.append)
    api.request("GET", "/user")
    assert slept == [42.0]


def test_gives_up_after_attempts_and_raises(monkeypatch):
    def always_500(req, timeout=None):
        raise make_http_error(500)

    monkeypatch.setattr("urllib.request.urlopen", always_500)
    api = GitHubAPI(token="t", repo="o/r", sleeper=lambda s: None)
    with pytest.raises(GitHubError) as exc:
        api.request("GET", "/user", attempts=3)
    assert exc.value.status == 500


def test_404_is_not_retried(monkeypatch):
    calls = []

    def not_found(req, timeout=None):
        calls.append(1)
        raise make_http_error(404)

    monkeypatch.setattr("urllib.request.urlopen", not_found)
    api = GitHubAPI(token="t", repo="o/r", sleeper=lambda s: None)
    with pytest.raises(GitHubError):
        api.request("GET", "/repos/o/r/contents/missing")
    assert len(calls) == 1
