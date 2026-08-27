"""Transfer-API-backed metadata: the half HTTPS cannot provide.

The Globus HTTPS interface serves bytes but has no directory listings,
and its ``HEAD`` responses are unclassifiable (a 404 from HEAD carries no
body, and the body is the only thing separating a backend fault from a
real miss). So ``ls``/``info`` come from the Transfer API instead, which
returns real name/size/type/last_modified via ``operation_ls`` and
``operation_stat``.

The Transfer API also supplies true file sizes, which neutralizes a third
quirk: GCS answers suffix ranges (``bytes=-8``) with 416, the usual way
parquet readers seek to a footer. With the size known, readers address
the footer absolutely.

Kept in its own module so the byte path stays usable with no globus-sdk
installed -- anonymous public collections need nothing else.
"""

from __future__ import annotations

from typing import Any

from .errors import TransientBackendError, is_transient_body


def _require_sdk():
    try:
        import globus_sdk
    except ImportError as exc:  # pragma: no cover - trivial
        raise ImportError(
            "Listing Globus collections needs the Transfer API. "
            "Install with: pip install 'globusfs[auth]'"
        ) from exc
    return globus_sdk


def _stat_to_info(entry: dict[str, Any], path: str) -> dict[str, Any]:
    """Convert a Globus ``file`` document to an fsspec info dict.

    Globus types are ``dir``/``file`` plus the unix special types
    (``chr``, ``blk``, ``pipe``, ``other``). Anything that is not a
    directory is reported to fsspec as a file, since fsspec has no richer
    vocabulary and callers only ever read bytes.
    """
    gtype = entry.get("type")
    return {
        "name": path,
        "size": entry.get("size"),
        "type": "directory" if gtype == "dir" else "file",
        "globus_type": gtype,
        "last_modified": entry.get("last_modified"),
    }


class TransferMetadata:
    """Metadata lookups for one collection, via a ``TransferClient``.

    Parameters
    ----------
    client:
        A ``globus_sdk.TransferClient``. Injected rather than constructed
        here so the caller owns login, and so tests can pass a fake.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_authorizer(cls, authorizer: Any) -> TransferMetadata:
        globus_sdk = _require_sdk()
        return cls(globus_sdk.TransferClient(authorizer=authorizer))

    def https_url(self, collection_id: str) -> str:
        """Resolve a collection UUID to the base URL that serves bytes."""
        ep = self._client.get_endpoint(collection_id)
        url = ep.get("https_server")
        if not url:
            raise ValueError(
                f"Collection {collection_id} reports no https_server, so it "
                f"cannot serve files over HTTPS. Only Globus Connect Server "
                f"collections with HTTPS enabled are readable this way."
            )
        return str(url).rstrip("/")

    @staticmethod
    def _abs(path: str) -> str:
        """Anchor a path at the collection root.

        Globus resolves a *relative* path against ``/~/`` (the mapped
        user's home), so ``home`` becomes ``/~/home`` and 404s while
        ``/home`` lists fine. Paths reaching here have already been
        stripped of their protocol and leading slash by fsspec, so
        absoluteness has to be restored or every nested listing fails.
        """
        return "/" + path.lstrip("/") if path else "/"

    def info(self, collection_id: str, path: str) -> dict[str, Any]:
        """Stat one path. Raises FileNotFoundError if genuinely absent."""
        globus_sdk = _require_sdk()
        try:
            res = self._client.operation_stat(collection_id, path=self._abs(path))
        except globus_sdk.TransferAPIError as exc:
            raise self._translate(exc, collection_id, path) from exc
        # GlobusHTTPResponse is not dict()-able (dict() iterates it as a
        # sequence and fails); .data is the underlying document.
        return _stat_to_info(res.data, path)

    def ls(
        self, collection_id: str, path: str, detail: bool = True
    ) -> list[dict[str, Any]] | list[str]:
        globus_sdk = _require_sdk()
        try:
            res = self._client.operation_ls(collection_id, path=self._abs(path))
        except globus_sdk.TransferAPIError as exc:
            raise self._translate(exc, collection_id, path) from exc

        base = path.rstrip("/")
        out = [
            _stat_to_info(e, f"{base}/{e['name']}" if base else e["name"]) for e in res
        ]
        return out if detail else [e["name"] for e in out]

    @staticmethod
    def _translate(exc: Any, collection_id: str, path: str) -> Exception:
        """Map a TransferAPIError to the right Python exception.

        The same transient-vs-permanent care as the HTTPS path: a backend
        fault must never surface as ``FileNotFoundError``, or callers will
        conclude healthy data is missing.
        """
        code = getattr(exc, "code", "") or ""
        message = getattr(exc, "message", "") or str(exc)

        if is_transient_body(f"{code} {message}"):
            return TransientBackendError(
                f"{collection_id}:{path}: Globus backend fault ({code}). "
                f"Transient, not a missing file."
            )
        if "NotFound" in code or "ClientError.NotFound" == code:
            return FileNotFoundError(f"{collection_id}:{path}")
        if getattr(exc, "http_status", None) == 404:
            return FileNotFoundError(f"{collection_id}:{path}")
        return exc
