"""HTTP test hook for python heredocs in bash scripts.

Production scripts call `urllib.request.urlopen(url)` directly. Tests can't
easily intercept that without monkey-patching at module load. This lib
provides a `install_test_hook_if_set()` function that scripts call ONCE at
the top of their python heredoc. If the OC_TEST_HTTP_FIXTURE_DIR env var
is set, urllib.request.urlopen is replaced with a fixture-reading version.

Fixture directory layout:
    <fixture_dir>/<sha1-of-url>.body         # response bytes
    <fixture_dir>/<sha1-of-url>.status       # optional, default 200

If a request URL has no fixture, the hook raises an HTTPError 404 so test
authors notice missing fixtures loudly rather than tests silently passing
on real HTTP.

In production (env var unset) this lib is a no-op import.
"""
from __future__ import annotations

import hashlib
import io
import os
import urllib.request


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def install_test_hook_if_set() -> None:
    """Install the urlopen monkey-patch if OC_TEST_HTTP_FIXTURE_DIR is set.

    Once installed, urllib.request.urlopen reads from <fixture_dir> instead
    of touching the network.
    """
    fixture_dir = os.environ.get("OC_TEST_HTTP_FIXTURE_DIR")
    if not fixture_dir:
        return
    fixture_dir = os.path.expanduser(fixture_dir)

    record_file = os.environ.get("OC_TEST_HTTP_RECORD")

    def fake_urlopen(req_or_url, *args, **kwargs):
        if hasattr(req_or_url, "full_url"):
            url = req_or_url.full_url
            req = req_or_url
        elif hasattr(req_or_url, "get_full_url"):
            url = req_or_url.get_full_url()
            req = req_or_url
        else:
            url = str(req_or_url)
            req = None

        # Record this request if requested (URL + outgoing body)
        if record_file:
            req_body = b""
            if req is not None and req.data:
                req_body = req.data if isinstance(req.data, bytes) else req.data.encode()
            try:
                with open(record_file, "ab") as rf:
                    rf.write(b"URL: " + url.encode() + b"\n")
                    rf.write(b"BODY: " + req_body + b"\n---\n")
            except Exception:
                pass

        h = _url_hash(url)
        body_path = os.path.join(fixture_dir, h + ".body")
        status_path = os.path.join(fixture_dir, h + ".status")
        if not os.path.exists(body_path):
            raise urllib.error.HTTPError(
                url, 404, f"no fixture for {url} (hash={h})", {}, io.BytesIO(b"")
            )
        body = open(body_path, "rb").read()
        status = 200
        if os.path.exists(status_path):
            try:
                status = int(open(status_path).read().strip())
            except ValueError:
                pass
        return _FakeResponse(body, status)

    urllib.request.urlopen = fake_urlopen
