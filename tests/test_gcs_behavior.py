"""Characterization tests for Globus Connect Server's HTTP behavior.

These hit a real public collection (NCSA Taiga, via the ISAAC direct
download catalog) and are marked ``network``.

Their job is to pin the server behaviors the design depends on. Two of
them exist specifically to document *failure* behavior, because the
failure mode here is unusually nasty: GCS load-balances across GridFTP
backends, and a failing backend returns

    Mapping collection to specified ID failed.
    GlobusError: v=1 c=ENDPOINT_ERROR
    GCS Manager Internal Error

rendered as **HTTP 404** -- byte-identical in status to a genuinely
missing file. Observed failure rates against this collection have
ranged from 0/20 to 20/20 within minutes, affecting files, directories,
and the collection root alike. Any client that treats 404 as "absent"
will report healthy data as missing.

Tests that must reach real data therefore retry, and assert on the
*body* to tell a transient backend error from a real miss.
"""

import pytest

requests = pytest.importorskip("requests")

pytestmark = pytest.mark.network

BASE = "https://g-05a4b6.2d513.8443.data.globus.org"
FILE = f"{BASE}/ability/ALL_2007-01.parquet"
SIZE = 360059  # from the ISAAC manifest

# Marker distinguishing a flaky backend from a real 404.
TRANSIENT = "ENDPOINT_ERROR"
ATTEMPTS = 12


def is_transient(resp) -> bool:
    """True if a >=400 response is backend flakiness rather than a real miss."""
    return TRANSIENT in (resp.text or "") or "INTERNAL_ERROR" in (resp.text or "")


def fetch(method, url, **kw):
    """Retry past transient backend errors, on a fresh connection each time.

    Fresh connections matter: the bad backend is chosen at connect time
    and is sticky for that connection's life.
    """
    last = None
    for _ in range(ATTEMPTS):
        resp = requests.request(method, url, timeout=30, **kw)
        if resp.status_code < 400 or not is_transient(resp):
            return resp
        last = resp
    pytest.skip(f"collection degraded: {ATTEMPTS} attempts all hit {TRANSIENT}")
    return last


def test_range_requests_supported():
    """The load-bearing assumption: GCS serves 206 for byte ranges."""
    r = fetch("GET", FILE, headers={"Range": "bytes=0-15"})
    assert r.status_code == 206
    assert len(r.content) == 16
    assert r.content[:4] == b"PAR1"


def test_midfile_seek():
    r = fetch("GET", FILE, headers={"Range": "bytes=100000-100031"})
    assert r.status_code == 206
    assert r.headers["Content-Range"].startswith("bytes 100000-100031")


def test_footer_read_with_absolute_offsets():
    """Parquet footer reads work when addressed absolutely."""
    r = fetch("GET", FILE, headers={"Range": f"bytes={SIZE - 8}-{SIZE - 1}"})
    assert r.status_code == 206
    assert r.content.endswith(b"PAR1")


def test_suffix_range_rejected():
    """GCS returns 416 for bytes=-N, which is how readers footer-seek.

    Documented because it is why info() must supply a real size: with the
    size known, readers can use absolute offsets instead.
    """
    r = fetch("GET", FILE, headers={"Range": "bytes=-8"})
    assert r.status_code == 416


def test_head_reports_size_and_range_support():
    r = fetch("HEAD", FILE)
    assert r.status_code == 200
    assert r.headers["Accept-Ranges"] == "bytes"
    assert int(r.headers["Content-Length"]) == SIZE


def test_transient_404_is_distinguishable_by_body():
    """The core defense: a flaky-backend 404 is identifiable, and only by body.

    Skips when the collection happens to be healthy -- the point is that
    *when* it fires, the body says ENDPOINT_ERROR. A real miss does not.
    """
    for _ in range(ATTEMPTS):
        r = requests.get(FILE, headers={"Range": "bytes=0-3"}, timeout=30)
        if r.status_code == 404:
            assert is_transient(r), (
                "a 404 on a known-present file should carry the backend-error "
                f"marker; got body {r.text[:200]!r}"
            )
            return
    pytest.skip("collection healthy right now; no transient 404 observed")


def test_real_miss_is_not_flagged_transient():
    """A genuinely absent path must NOT look transient, or retries never end."""
    for _ in range(ATTEMPTS):
        r = requests.get(f"{BASE}/ability/definitely-not-here.parquet", timeout=30)
        if r.status_code == 404 and not is_transient(r):
            return  # got a clean, real 404 -- exactly what we want to see
    pytest.skip("could not observe a clean 404 (collection degraded)")
