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
exactly that dialect, including 416 for out-of-range. So the read path
is inherited rather than rewritten.

What has to be overridden
-------------------------
Two things, both about *not trusting a 404*:

``_raise_not_found_for_status``
    fsspec maps 404 to ``FileNotFoundError``. On GCS a 404 is just as
    likely to be a transient backend fault (see :mod:`globusfs.errors`),
    so this inspects the body first. Every read path in HTTPFileSystem
    funnels through this one method, which is why the fix is small.

``_info`` / ``_ls``
    The HTTPS interface has no directory listings at all, so metadata
    comes from the Transfer API instead.

The Transfer API also supplies real file sizes, which sidesteps a third
quirk: GCS returns 416 for suffix ranges (``bytes=-8``), the usual way
parquet readers seek to a footer. With the size known, readers can
address the footer absolutely.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fsspec.asyn import sync
from fsspec.implementations.http import HTTPFileSystem

from .credentials import AnonymousCredentials, GlobusCredentials
from .errors import TransientBackendError, is_transient_body

logger = logging.getLogger(__name__)

__all__ = ["GlobusFileSystem"]

DEFAULT_RETRIES = 5
DEFAULT_BACKOFF = 0.3


class _RetryingFetcher:
    """Block fetcher that routes through the transient-404 retry path.

    ``HTTPFile.async_fetch_range`` calls ``raise_for_status()`` directly,
    so a backend fault reaches the caller as a bare 404 with no
    classification -- the exact failure this package exists to prevent.
    Substituting the fetcher puts file-object reads on the same footing
    as ``cat_file``.

    A class rather than a closure so the file object stays picklable.
    """

    def __init__(self, fs: GlobusFileSystem, url: str) -> None:
        self.fs = fs
        self.url = url

    def __call__(self, start, end):
        return sync(self.fs.loop, self.fs._cat_file, self.url, start=start, end=end)


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
    retries:
        Attempts before giving up on a transient backend fault.
    """

    protocol = "globus"
    sep = "/"

    def __init__(
        self,
        collection_id: str | None = None,
        credentials: GlobusCredentials | None = None,
        https_url: str | None = None,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        metadata: Any = None,
        **kwargs: Any,
    ) -> None:
        self.collection_id = collection_id
        self.credentials = credentials or AnonymousCredentials()
        self._https_url = https_url.rstrip("/") if https_url else None
        self.retries = retries
        self.backoff = backoff
        # TransferMetadata (or a stand-in). Optional: the byte path works
        # without it, so reading public data needs no globus-sdk.
        self.metadata = metadata
        self._base_cache: dict[str, str] = {}
        super().__init__(**kwargs)

    @property
    def fsid(self) -> str:
        return "globus"

    # ---------------------------------------------------------------- paths

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        """``globus://uuid/a/b`` -> ``uuid/a/b``; leave bare paths alone."""
        for prefix in ("globus://", "globus:/"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        return path.lstrip("/").rstrip("/") or ""

    def split_path(self, path: str) -> tuple[str, str]:
        """Split a stripped path into ``(collection_id, remainder)``."""
        stripped = self._strip_protocol(path)
        if not stripped:
            if not self.collection_id:
                raise ValueError(
                    "No collection: use globus://<collection-uuid>/path or pass "
                    "collection_id= to GlobusFileSystem."
                )
            return self.collection_id, ""

        head, _, tail = stripped.partition("/")
        # A leading segment is the collection only when no default is set,
        # or when it actually differs from the default. This lets both
        # "uuid/a/b" and "a/b" work against a configured default.
        if self.collection_id and head != self.collection_id:
            return self.collection_id, stripped
        return head, tail

    def _url(self, path: str) -> str:
        """Full HTTPS URL for a globus path.

        Idempotent: an already-resolved ``https://`` URL passes through
        unchanged. fsspec re-enters with whatever it was handed (``_open``
        resolves, then the parent calls ``info`` with the result), so
        resolving twice must not double the prefix.
        """
        if path.startswith(("http://", "https://")):
            return path
        collection, rel = self.split_path(path)
        return (
            f"{self._resolve_base(collection)}/{rel}"
            if rel
            else (self._resolve_base(collection) + "/")
        )

    def _resolve_base(self, collection_id: str) -> str:
        """Map a collection UUID to its ``https_url``, caching the result."""
        if self._https_url:
            return self._https_url
        if collection_id in self._base_cache:
            return self._base_cache[collection_id]
        if self.metadata is None:
            raise ValueError(
                f"Cannot resolve collection {collection_id} to a URL: pass "
                f"https_url= if you know it, or metadata= (a TransferMetadata) "
                f"to look it up."
            )
        base = self.metadata.https_url(collection_id)
        self._base_cache[collection_id] = base
        return base

    # ------------------------------------------------------------ requests

    def _auth_headers(self, path: str) -> dict[str, str]:
        collection, _ = self.split_path(path)
        token = self.credentials.token_for(collection)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _check(self, response, url: str) -> None:
        """Classify a response, distinguishing a backend fault from a miss.

        This is async because the distinction lives in the response body
        and aiohttp bodies must be awaited -- which is exactly why the
        inherited sync ``_raise_not_found_for_status`` hook cannot do the
        job on its own.
        """
        if response.status == 404:
            body = await response.text(errors="ignore")
            if is_transient_body(body):
                raise TransientBackendError(
                    f"{url}: GCS backend fault reported as HTTP 404. This is a "
                    f"transient failure of the collection, not a missing file. "
                    f"Body: {body[:200]!r}"
                )
            raise FileNotFoundError(url)
        response.raise_for_status()

    async def _retry(self, attempt_factory, what: str):
        """Run ``attempt_factory()``, retrying transient backend faults.

        Each attempt re-issues the request, because the failing backend is
        selected at connect time and is sticky for that connection.
        """
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return await attempt_factory()
            except TransientBackendError as exc:
                last = exc
                if attempt < self.retries:
                    logger.debug(
                        "%s: transient backend fault (attempt %d/%d), retrying",
                        what,
                        attempt,
                        self.retries,
                    )
                    await asyncio.sleep(self.backoff * attempt)
        raise TransientBackendError(
            f"{what}: {self.retries} attempts all hit GCS backend faults. The "
            f"collection is degraded; this is not a missing file."
        ) from last

    async def _cat_file(self, path, start=None, end=None, **kwargs):
        """Read bytes, retrying past transient backend faults.

        Overridden rather than inherited so that a 404 gets classified
        before fsspec turns it into ``FileNotFoundError``.
        """
        url = self._url(path)

        async def attempt():
            headers = dict(kwargs.pop("headers", {}))
            headers.update(self._auth_headers(path))
            if start is not None or end is not None:
                if start == end:
                    return b""
                headers["Range"] = await self._process_limits(url, start, end)
            session = await self.set_session()
            async with session.get(
                self.encode_url(url), headers=headers, **self.kwargs
            ) as r:
                if r.status >= 400:
                    await self._check(r, url)
                return await r.read()

        return await self._retry(attempt, f"cat_file({path})")

    # -------------------------------------------------------------- listing

    # NOTE: both the async and sync forms are overridden on purpose.
    # fsspec's mirror_sync_methods only wires sync `ls` -> `_ls` when the
    # sync version is still AbstractFileSystem's default; HTTPFileSystem
    # binds its own, so overriding `_ls` alone leaves the parent's HTML
    # link-scraping in place and the override silently never runs.

    _NEED_META = (
        "{op}() needs the Transfer API: the Globus HTTPS interface has no "
        "directory listings and its HEAD responses cannot be classified. "
        "Construct with metadata=TransferMetadata(...) -- see the README."
    )

    def _metadata(self, op: str):
        if self.metadata is None:
            raise NotImplementedError(self._NEED_META.format(op=op))
        return self.metadata

    def ls(self, path, detail=True, **kwargs):
        collection, rel = self.split_path(path)
        return self._metadata("ls").ls(collection, rel, detail=detail)

    async def _ls(self, path, detail=True, **kwargs):
        return self.ls(path, detail=detail, **kwargs)

    def info(self, path, **kwargs):
        """Stat a path, preferring the Transfer API.

        Falls back to a **ranged GET** when no Transfer client is
        configured, so anonymous public reads keep working: ``open()``
        needs a size, and a plain HEAD cannot supply one safely (a HEAD
        404 has no body, so it cannot be told apart from a backend
        fault). A ranged GET returns a body and puts the total in
        ``Content-Range``.

        The fallback can only ever report a file. Directory detection
        genuinely requires Transfer.
        """
        collection, rel = self.split_path(path)
        if self.metadata is not None:
            return self.metadata.info(collection, rel)
        return {
            "name": self._strip_protocol(path),
            "size": self._size_via_range(path),
            "type": "file",
        }

    def _size_via_range(self, path) -> int | None:
        """Total size via an open-ended ranged GET.

        Uses ``Range: bytes=0-`` rather than ``bytes=0-0``: GCS reports
        the total as ``*`` in Content-Range (``bytes 0-0/*``), so a
        one-byte probe cannot see the size. The open-ended form answers
        ``bytes 0-<last>/*`` with a matching Content-Length, which does.
        The body is never read, so this costs headers, not bytes.
        """
        url = self._url(path)

        async def attempt():
            headers = {"Range": "bytes=0-"}
            headers.update(self._auth_headers(path))
            session = await self.set_session()
            async with session.get(
                self.encode_url(url), headers=headers, **self.kwargs
            ) as r:
                if r.status >= 400:
                    await self._check(r, url)
                start, _, last = (
                    r.headers.get("Content-Range", "")
                    .removeprefix("bytes ")
                    .partition("/")[0]
                    .partition("-")
                )
                if start.isdigit() and last.isdigit():
                    return int(last) - int(start) + 1
                cl = r.headers.get("Content-Length")
                return int(cl) if cl and cl.isdigit() else None

        return sync(self.loop, self._retry, attempt, f"info({path})")

    def _open(self, path, mode="rb", **kwargs):
        """Open by resolved URL, with retry-aware block fetches.

        Two fixes over the parent. It builds its file object straight
        from the path it is handed (``uuid/rel``, not a URL), so the URL
        is resolved first. And ``HTTPFile.async_fetch_range`` calls
        ``raise_for_status()`` directly, bypassing the transient-404
        classification -- so the returned file's fetcher is wrapped to
        route block reads through :meth:`_cat_file`, which retries.
        """
        f = super()._open(self._url(path), mode=mode, **kwargs)
        # The cache binds `self.fetcher = f._fetch_range` at construction,
        # so rebinding the method afterwards is too late -- the cache must
        # be repointed as well.
        f._fetch_range = _RetryingFetcher(self, self._url(path))
        if getattr(f, "cache", None) is not None:
            f.cache.fetcher = f._fetch_range
        return f

    async def _info(self, path, **kwargs):
        return self.info(path, **kwargs)
