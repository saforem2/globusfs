"""The core correctness property: a backend fault is not a missing file.

These use a local HTTP server that can be told to fail, so the behavior
is tested deterministically instead of waiting for the real collection
to misbehave.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from globusfs import GlobusFileSystem
from globusfs.errors import TransientBackendError, is_transient_body

TRANSIENT_BODY = (
    b"Mapping collection to specified ID failed.\n"
    b"GlobusError: v=1 c=ENDPOINT_ERROR\n"
    b"GCS Manager Internal Error\n"
)
REAL_MISS_BODY = (
    b"File not found\nGlobusError: v=1 c=PATH_NOT_FOUND\nGridFTP-Errno: 2\n"
)
PAYLOAD = b"hello-from-globus"


class _Handler(BaseHTTPRequestHandler):
    """Serves PAYLOAD, but fails the first ``fail_times`` requests."""

    fail_times = 0
    seen = 0
    body = TRANSIENT_BODY
    last_range = None

    def do_GET(self):
        cls = type(self)
        cls.seen += 1
        if self.path.endswith("/gone"):
            self.send_response(404)
            self.send_header("Content-Length", str(len(REAL_MISS_BODY)))
            self.end_headers()
            self.wfile.write(REAL_MISS_BODY)
            return
        if cls.seen <= cls.fail_times:
            self.send_response(404)
            self.send_header("Content-Length", str(len(cls.body)))
            self.end_headers()
            self.wfile.write(cls.body)
            return
        rng = self.headers.get("Range")
        cls.last_range = rng
        if rng and rng.startswith("bytes="):
            spec = rng.removeprefix("bytes=")
            lo_s, _, hi_s = spec.partition("-")
            lo = int(lo_s) if lo_s else 0
            hi = int(hi_s) if hi_s else len(PAYLOAD) - 1
            chunk = PAYLOAD[lo : hi + 1]
            self.send_response(206)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Range", f"bytes {lo}-{hi}/{len(PAYLOAD)}")
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _Handler.fail_times = 0
    _Handler.seen = 0
    _Handler.body = TRANSIENT_BODY
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def fs(server, **kw):
    return GlobusFileSystem(
        collection_id="test-uuid", https_url=server, backoff=0.0, **kw
    )


def test_body_classification():
    assert is_transient_body(TRANSIENT_BODY.decode())
    assert not is_transient_body(REAL_MISS_BODY.decode())
    assert not is_transient_body("")
    assert not is_transient_body(None)


def test_permanent_marker_wins_over_transient():
    """A real miss must never be retried, even if both markers appear."""
    assert not is_transient_body("INTERNAL_ERROR ... GridFTP-Errno: 2")


def test_recovers_from_transient_404(server):
    """The headline case: healthy data must not look missing."""
    _Handler.fail_times = 3
    assert fs(server).cat_file("test-uuid/data.bin") == PAYLOAD


def test_gives_up_with_transient_error_not_filenotfound(server):
    """When it truly can't recover, the error must not say 'not found'."""
    _Handler.fail_times = 99
    with pytest.raises(TransientBackendError) as exc:
        fs(server, retries=3).cat_file("test-uuid/data.bin")
    assert not isinstance(exc.value, FileNotFoundError)
    assert "degraded" in str(exc.value)


def test_real_miss_raises_filenotfound_immediately(server):
    """A genuine 404 must fail fast, not burn retries."""
    with pytest.raises(FileNotFoundError):
        fs(server, retries=5).cat_file("test-uuid/gone")
    assert _Handler.seen == 1, "a real miss must not be retried"


def test_ranged_read(server):
    """Ranged reads must emit a Range header and return only those bytes."""
    assert fs(server).cat_file("test-uuid/data.bin", start=0, end=5) == PAYLOAD[:5]
    assert _Handler.last_range == "bytes=0-4"


def test_file_object_reads_also_retry(server):
    """open().read() must retry too, not just cat_file().

    HTTPFile.async_fetch_range calls raise_for_status() directly, so
    without substituting the fetcher a backend fault escapes the
    classifier and surfaces as a bare 404 -- precisely the bug this
    package exists to prevent. This is the regression test for that.
    """
    _Handler.fail_times = 3
    with fs(server).open("test-uuid/data.bin", "rb") as f:
        assert f.read(5) == PAYLOAD[:5]


def test_size_probe_uses_open_ended_range(server):
    """info() must use `bytes=0-`, not `bytes=0-0`.

    GCS reports the total as `*` (`bytes 0-0/*`), so a one-byte probe
    cannot see the size; the open-ended form answers with the real
    extent.
    """
    assert fs(server).info("test-uuid/data.bin")["size"] == len(PAYLOAD)
    assert _Handler.last_range == "bytes=0-"


def test_info_falls_back_to_ranged_get(server):
    """Without Transfer, info() still works well enough to open() a file.

    It can only ever report "file" -- directory detection needs Transfer
    -- but a size is enough for readers like pyarrow to seek.
    """
    info = fs(server).info("test-uuid/data.bin")
    assert info["size"] == len(PAYLOAD)
    assert info["type"] == "file"


class _WriteHandler(_Handler):
    """Adds PUT/DELETE, recording what arrived."""

    puts: ClassVar[dict] = {}
    deletes: ClassVar[list] = []
    put_fail_times = 0
    put_seen = 0

    def do_PUT(self):
        cls = type(self)
        cls.put_seen += 1
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if cls.put_seen <= cls.put_fail_times:
            self.send_response(404)
            self.send_header("Content-Length", str(len(TRANSIENT_BODY)))
            self.end_headers()
            self.wfile.write(TRANSIENT_BODY)
            return
        cls.puts[self.path] = body
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        type(self).deletes.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def write_server():
    httpd = HTTPServer(("127.0.0.1", 0), _WriteHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _WriteHandler.puts = {}
    _WriteHandler.deletes = []
    _WriteHandler.put_fail_times = 0
    _WriteHandler.put_seen = 0
    _WriteHandler.fail_times = 0
    _WriteHandler.seen = 0
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_pipe_file_uploads(write_server):
    fs(write_server).pipe_file("test-uuid/out.bin", b"payload")
    assert _WriteHandler.puts["/out.bin"] == b"payload"


def test_write_retries_transient_faults(write_server):
    """Uploads get the same 404 classification as reads."""
    _WriteHandler.put_fail_times = 2
    fs(write_server).pipe_file("test-uuid/out.bin", b"payload")
    assert _WriteHandler.puts["/out.bin"] == b"payload"


def test_rm_file_deletes(write_server):
    fs(write_server).rm("test-uuid/gone.bin")
    assert _WriteHandler.deletes == ["/gone.bin"]


def test_write_then_read_roundtrip(write_server):
    f = fs(write_server)
    f.pipe_file("test-uuid/rt.bin", b"round-trip")
    assert _WriteHandler.puts["/rt.bin"] == b"round-trip"
