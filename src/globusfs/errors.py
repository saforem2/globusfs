"""Exceptions, and the transient-vs-real 404 distinction.

Globus Connect Server load-balances across GridFTP backends. When a
backend is unhealthy it returns::

    Mapping collection to specified ID failed.
    GlobusError: v=1 c=ENDPOINT_ERROR
    GCS Manager Internal Error

with **HTTP status 404** -- byte-identical in status to a genuinely
missing file. Measured against a healthy public collection, failure
rates swung from 0/20 to 20/20 within minutes, hitting files,
directories, and the collection root alike.

This is the single most dangerous behavior of the platform for a
filesystem client: fsspec maps 404 to ``FileNotFoundError``, so without
this distinction a transient backend fault silently becomes "your data
does not exist" -- intermittently, and only under load.

The only signal is the response body.
"""

from __future__ import annotations

# Markers seen in GCS backend-failure bodies. ENDPOINT_ERROR is what the
# GCS Manager emits; INTERNAL_ERROR / GridFTP-Errno 108 is the older
# GridFTP-level form.
_TRANSIENT_MARKERS = (
    "ENDPOINT_ERROR",
    "INTERNAL_ERROR",
    "GCS Manager Internal Error",
)

# GridFTP-Errno 2 is PATH_NOT_FOUND: a real, permanent miss.
_PERMANENT_MARKERS = ("GridFTP-Errno: 2",)


class GlobusFSError(Exception):
    """Base for globusfs errors."""


class TransientBackendError(GlobusFSError):
    """A GCS backend failed a request that should have succeeded.

    Raised only after retries are exhausted. Deliberately *not* a
    subclass of ``FileNotFoundError``: the whole point is that callers
    must not confuse it with absence.
    """


def is_transient_body(body: str | None) -> bool:
    """True if a >=400 response body indicates backend flakiness.

    A permanent marker wins if both appear, so a real miss is never
    retried into a timeout.
    """
    if not body:
        # No body is not evidence of a transient fault. Treating unknown
        # as permanent keeps a real 404 fast; a genuine transient will
        # normally carry the marker.
        return False
    if any(m in body for m in _PERMANENT_MARKERS):
        return False
    return any(m in body for m in _TRANSIENT_MARKERS)


class SessionExpiredError(GlobusFSError):
    """The identity session no longer satisfies the collection's policy.

    Facilities like ALCF require an identity from a specific domain,
    authenticated recently. When that lapses, Globus answers with a wall
    of GridFTP text whose actionable part -- log in again -- is easy to
    miss:

        530-Login incorrect. : GlobusError: v=1 c=LOGIN_DENIED
        530-GridFTP-Message: None of your authenticated identities are
        from domains allowed by resource policies
        530-GridFTP-JSON-Result: {... "session_required_single_domain":
        ["alcf.anl.gov"] ...}

    This is recoverable and routine, so it gets its own exception rather
    than surfacing as a generic API error.
    """


# Markers for an identity/session problem rather than a fault or a miss.
_SESSION_MARKERS = (
    "session_required_single_domain",
    "session_required_identities",
    "session_required_mfa",
    "not_from_allowed_domain",
    "LOGIN_DENIED",
)


def is_session_problem(text: str | None) -> bool:
    """True if an error indicates an expired or insufficient session."""
    return bool(text) and any(m in text for m in _SESSION_MARKERS)


def required_domains(text: str) -> list[str]:
    """Extract the domains a collection demands, for the error message.

    Scans for the keys directly rather than parsing the embedded JSON:
    Globus truncates these blobs mid-document, so json.loads usually
    fails on exactly the errors worth explaining. Returns [] when it
    cannot tell, which only costs a less specific message.
    """
    import re

    for key in ("session_required_single_domain", "allowed_domains"):
        m = re.search(rf'"{key}":\s*\[([^\]]*)\]', text or "")
        if m:
            found = re.findall(r'"([^"]+)"', m.group(1))
            if found:
                return found
    return []
