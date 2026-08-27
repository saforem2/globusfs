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
