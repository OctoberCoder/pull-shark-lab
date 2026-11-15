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


def test_401_is_not_retried(monkeypatch):
    calls = []

    def unauthorized(req, timeout=None):
        calls.append(1)
        raise make_http_error(401)

    monkeypatch.setattr("urllib.request.urlopen", unauthorized)
    api = GitHubAPI(token="t", repo="o/r", sleeper=lambda s: None)
    with pytest.raises(GitHubError):
        api.request("GET", "/user")
    assert len(calls) == 1


def test_403_without_rate_limit_signal_is_not_retried(monkeypatch):
    calls = []

    def forbidden(req, timeout=None):
        calls.append(1)
        raise make_http_error(
            403, body=b'{"message":"Resource not accessible by personal access token"}'
        )

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    api = GitHubAPI(token="t", repo="o/r", sleeper=lambda s: None)
    with pytest.raises(GitHubError):
        api.request("GET", "/user")
    assert len(calls) == 1


def test_403_secondary_rate_limit_is_retried(monkeypatch):
    responses = [
        make_http_error(403, body=b'{"message":"You have exceeded a secondary rate limit"}'),
        FakeResponse(b'{"ok": true}'),
    ]
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


def test_network_error_is_retried(monkeypatch):
    responses = [urllib.error.URLError("connection reset"), FakeResponse(b'{"ok": true}')]
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


def test_malformed_json_body_is_retried(monkeypatch):
    responses = [FakeResponse(b"<html>proxy error</html>"), FakeResponse(b'{"ok": true}')]
    slept = []

    def fake_urlopen(req, timeout=None):
        return responses.pop(0)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    api = GitHubAPI(token="t", repo="o/r", sleeper=slept.append)
    assert api.request("GET", "/user") == {"ok": True}
    assert slept == [60.0]


def test_retry_after_is_clamped(monkeypatch):
    responses = [make_http_error(403, retry_after=3600), FakeResponse(b'{"ok": true}')]
    slept = []

    def fake_urlopen(req, timeout=None):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    api = GitHubAPI(token="t", repo="o/r", sleeper=slept.append)
    api.request("GET", "/user")
    assert slept == [300.0]


def test_non_numeric_retry_after_is_ignored(monkeypatch):
    headers = {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}
    responses = [
        urllib.error.HTTPError(
            url="https://api.github.com/x",
            code=503,
            msg="err",
            hdrs=headers,
            fp=io.BytesIO(b'{"message":"busy"}'),
        ),
        FakeResponse(b'{"ok": true}'),
    ]
    slept = []

    def fake_urlopen(req, timeout=None):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    api = GitHubAPI(token="t", repo="o/r", sleeper=slept.append)
    api.request("GET", "/user")
    assert slept == [60.0]


def test_error_message_includes_method_and_path(monkeypatch):
    def not_found(req, timeout=None):
        raise make_http_error(404)

    monkeypatch.setattr("urllib.request.urlopen", not_found)
    api = GitHubAPI(token="t", repo="o/r", sleeper=lambda s: None)
    with pytest.raises(GitHubError) as exc:
        api.request("GET", "/repos/o/r/contents/missing")
    assert "GET" in str(exc.value)
    assert "/repos/o/r/contents/missing" in str(exc.value)
