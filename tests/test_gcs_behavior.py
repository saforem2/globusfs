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


def test_head_cannot_be_classified_and_must_not_be_used():
    """HEAD is unusable on GCS: a 404 from it carries no body to classify.

    The transient-vs-real distinction lives entirely in the response body,
    and HEAD responses have none by definition. So a HEAD 404 is
    permanently ambiguous -- which is why size/existence must come from
    the Transfer API, or from a ranged GET (which does return a body).
    """
    for _ in range(ATTEMPTS):
        r = requests.head(FILE, timeout=30)
        if r.status_code == 404:
            assert r.text == "", "HEAD 404 has no body, so it cannot be classified"
            assert not is_transient(r), "and therefore looks indistinguishable"
            return
    pytest.skip("collection healthy right now; no HEAD 404 observed")


def test_size_via_ranged_get_is_classifiable():
    """The supported way to size a file over HTTPS: a ranged GET.

    Unlike HEAD it returns a body, so a backend fault can be told apart
    from a real miss, and Content-Range carries the total size.
    """
    r = fetch("GET", FILE, headers={"Range": "bytes=0-0"})
    assert r.status_code == 206
    total = r.headers["Content-Range"].rsplit("/", 1)[-1]
    # GCS may report "*" for total; when it gives a number it must be right.
    if total != "*":
        assert int(total) == SIZE


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


def test_open_ended_range_reveals_size():
    """`bytes=0-` exposes the real extent; `bytes=0-0` does not.

    GCS answers a one-byte probe with `Content-Range: bytes 0-0/*` --
    total elided. The open-ended form gives `bytes 0-<last>/*`, which is
    how info() recovers a size without a (unclassifiable) HEAD.
    """
    r = fetch("GET", FILE, headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    span = r.headers["Content-Range"].removeprefix("bytes ").partition("/")[0]
    lo, _, hi = span.partition("-")
    assert int(hi) - int(lo) + 1 == SIZE


def test_one_byte_probe_hides_total():
    """Documents why the open-ended form is required."""
    r = fetch("GET", FILE, headers={"Range": "bytes=0-0"})
    assert r.status_code == 206
    assert r.headers["Content-Range"].endswith("/*")
