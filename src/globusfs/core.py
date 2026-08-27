"""fsspec filesystem for Globus collections.

URLs look like ``globus://<collection-uuid>/path/to/file``. The UUID is
the authority because it is the stable identifier for a collection; the
``https_url`` that actually serves bytes is a deployment detail resolved
at runtime.

Why this subclasses HTTPFileSystem
----------------------------------
Globus Connect Server serves full HTTP range semantics -- verified
against a live collection: 206 responses, correct ``Content-Range``,
mid-file seeks, and multipart ranges. ``HTTPFileSystem`` already speaks
exactly that dialect, including the 416-on-out-of-range case. So the
read path is inherited rather than rewritten.

Why listing is a different service
----------------------------------
The HTTPS interface has no directory listings, and it does not fail
cleanly when asked for one. Against a real collection::

    GET /ability/          -> 200, Content-Length: 0
    GET /ability           -> 404
    GET /missing.parquet   -> 404

A directory is a 200 with zero length -- indistinguishable from an empty
file. Trusting HTTPS for metadata would silently report every directory
as an empty file and produce empty datasets rather than errors. So
``info``/``ls`` go to the Transfer API (``operation_stat`` /
``operation_ls``), which returns real name/size/type/last_modified,
while bytes come over HTTPS. Neither service can back a filesystem
alone; together they cover it.

That split also neutralizes a second quirk: GCS returns 416 for suffix
ranges (``bytes=-8``), which is how parquet readers usually seek to the
footer. Because ``info`` knows the true size from Transfer, readers can
always use absolute offsets.
"""

from __future__ import annotations

from typing import Any

from fsspec.implementations.http import HTTPFileSystem

from .credentials import AnonymousCredentials, GlobusCredentials

__all__ = ["GlobusFileSystem"]


class GlobusFileSystem(HTTPFileSystem):
    """Read files from Globus collections over HTTPS.

    Parameters
    ----------
    collection_id:
        Default collection UUID, used when a path omits the authority.
    credentials:
        A :class:`~globusfs.credentials.GlobusCredentials`. Defaults to
        anonymous, which works for public collections and fails with a
        clean 401 elsewhere.
    https_url:
        Override the collection's base URL instead of resolving it via
        the Transfer API. Useful offline, in tests, and when the caller
        already knows it.
    """

    protocol = "globus"
    sep = "/"

    def __init__(
        self,
        collection_id: str | None = None,
        credentials: GlobusCredentials | None = None,
        https_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.collection_id = collection_id
        self.credentials = credentials or AnonymousCredentials()
        self._https_url = https_url.rstrip("/") if https_url else None
        super().__init__(**kwargs)

    @property
    def fsid(self) -> str:
        return "globus"

    # NOTE: everything below is the not-yet-implemented surface. The
    # scaffolding is deliberately explicit about what is missing so an
    # unfinished backend fails loudly rather than half-working.

    def _resolve_base(self, collection_id: str) -> str:
        """Map a collection UUID to its ``https_url``.

        Resolved via Transfer ``get_endpoint`` and cached, unless the
        caller passed ``https_url`` up front.
        """
        if self._https_url:
            return self._https_url
        raise NotImplementedError(
            "Transfer-API resolution of collection UUID -> https_url is not "
            "implemented yet. Pass https_url= explicitly for now."
        )

    async def _info(self, path, **kwargs):
        raise NotImplementedError(
            "info() must come from the Transfer API (operation_stat), not "
            "HTTPS: a directory over HTTPS returns 200 with Content-Length 0, "
            "which is indistinguishable from an empty file."
        )

    async def _ls(self, path, detail=True, **kwargs):
        raise NotImplementedError(
            "ls() requires the Transfer API (operation_ls); the Globus HTTPS "
            "interface does not support directory listings."
        )
