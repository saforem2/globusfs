"""Transfer-API-backed ls/info, against a fake TransferClient.

A fake rather than the real service: Transfer needs a user identity, and
the logic worth testing here (document translation, error mapping) is
ours, not Globus's.
"""

import pytest

from globusfs import GlobusFileSystem
from globusfs.errors import TransientBackendError
from globusfs.transfer import TransferMetadata

UUID = "6c54cade-bde5-45c1-bdea-f4bd71dba2cc"

LS_DOC = [
    {
        "name": "a.parquet",
        "type": "file",
        "size": 360059,
        "last_modified": "2026-01-02 03:04:05+00:00",
    },
    {
        "name": "sub",
        "type": "dir",
        "size": 4096,
        "last_modified": "2026-01-02 03:04:05+00:00",
    },
]


class FakeTransferError(Exception):
    def __init__(self, code="", message="", http_status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class FakeResponse:
    """Mimics GlobusHTTPResponse: indexable, with the document on .data."""

    def __init__(self, data):
        self.data = data

    def __getitem__(self, key):
        return self.data[key]


class FakeClient:
    def __init__(self, raises=None, endpoint=None):
        self._raises = raises
        self._endpoint = (
            endpoint
            if endpoint is not None
            else {"https_server": "https://example.data.globus.org"}
        )

    def get_endpoint(self, cid):
        return self._endpoint

    def operation_ls(self, cid, path=None):
        if self._raises:
            raise self._raises
        return LS_DOC

    def operation_stat(self, cid, path=None):
        if self._raises:
            raise self._raises
        return FakeResponse(LS_DOC[0])


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    """Stand in for globus_sdk so TransferAPIError matches our fake."""
    import globusfs.transfer as t

    class FakeSDK:
        TransferAPIError = FakeTransferError

    monkeypatch.setattr(t, "_require_sdk", lambda: FakeSDK)


def meta(**kw):
    return TransferMetadata(FakeClient(**kw))


def fs(**kw):
    return GlobusFileSystem(
        collection_id=UUID, https_url="https://x", skip_instance_cache=True, **kw
    )


def test_ls_translates_globus_documents():
    out = fs(metadata=meta()).ls(f"globus://{UUID}/data")
    assert [e["type"] for e in out] == ["file", "directory"]
    assert out[0]["size"] == 360059
    assert out[0]["name"] == "data/a.parquet"


def test_ls_detail_false_returns_names():
    assert fs(metadata=meta()).ls(f"globus://{UUID}/data", detail=False) == [
        "data/a.parquet",
        "data/sub",
    ]


def test_info_reports_real_size():
    """The point of using Transfer for metadata: a true size.

    With it, readers can address a parquet footer absolutely and dodge
    the 416-on-suffix-range quirk.
    """
    assert fs(metadata=meta()).info(f"globus://{UUID}/a.parquet")["size"] == 360059


def test_unix_special_types_report_as_file():
    """fsspec has no vocabulary for pipes/devices; only dir-ness matters."""
    client = FakeClient()
    client.operation_stat = lambda cid, path=None: FakeResponse(
        {"name": "p", "type": "pipe"}
    )
    info = TransferMetadata(client).info(UUID, "p")
    assert info["type"] == "file"
    assert info["globus_type"] == "pipe"


def test_missing_path_raises_filenotfound():
    err = FakeTransferError(code="ClientError.NotFound", http_status=404)
    with pytest.raises(FileNotFoundError):
        fs(metadata=meta(raises=err)).ls(f"globus://{UUID}/nope")


def test_backend_fault_is_not_filenotfound():
    """Same rule as the byte path: a fault must never look like absence."""
    err = FakeTransferError(
        code="ExternalError.DirListingFailed", message="GCS Manager Internal Error"
    )
    with pytest.raises(TransientBackendError) as exc:
        fs(metadata=meta(raises=err)).ls(f"globus://{UUID}/data")
    assert not isinstance(exc.value, FileNotFoundError)


def test_resolves_https_url_from_collection():
    f = GlobusFileSystem(collection_id=UUID, metadata=meta(), skip_instance_cache=True)
    assert f._resolve_base(UUID) == "https://example.data.globus.org"


def test_collection_without_https_is_a_clear_error():
    """Not every collection serves HTTPS; say so plainly."""
    f = GlobusFileSystem(
        collection_id=UUID,
        skip_instance_cache=True,
        metadata=meta(endpoint={"https_server": None}),
    )
    with pytest.raises(ValueError, match="no https_server"):
        f._resolve_base(UUID)


def test_listing_without_metadata_still_fails_loudly():
    with pytest.raises(NotImplementedError, match="Transfer API"):
        fs().ls("data")


def test_relative_paths_are_anchored_at_collection_root():
    """Regression: Globus resolves relative paths against /~/, not /.

    `ls("home")` became `/~/home` and 404'd while `/home` listed fine, so
    every nested listing failed after the first level.
    """
    seen = []

    class Recorder(FakeClient):
        def operation_ls(self, cid, path=None):
            seen.append(path)
            return LS_DOC

    TransferMetadata(Recorder()).ls(UUID, "home")
    assert seen == ["/home"]


def test_empty_path_lists_root():
    seen = []

    class Recorder(FakeClient):
        def operation_ls(self, cid, path=None):
            seen.append(path)
            return LS_DOC

    TransferMetadata(Recorder()).ls(UUID, "")
    assert seen == ["/"]


def test_resolved_urls_are_mapped_back_to_paths():
    """Regression: _open() resolves to a URL, then fsspec calls info() with it.

    Transfer speaks collection paths, so the URL has to be converted back
    or it is sent verbatim as a path (yielding a nonsense
    /https://host/... and a 403 from the endpoint).
    """
    f = GlobusFileSystem(
        collection_id=UUID,
        https_url="https://example.data.globus.org",
        metadata=meta(),
        skip_instance_cache=True,
    )
    assert (
        f._unresolve("https://example.data.globus.org/home/x.txt")
        == f"{UUID}/home/x.txt"
    )
    # Plain paths pass through untouched.
    assert f._unresolve("home/x.txt") == "home/x.txt"


def test_globus_response_objects_are_unwrapped():
    """Regression: dict() on a GlobusHTTPResponse raises KeyError.

    It iterates as a sequence; .data is the underlying document.
    """

    client = FakeClient()
    client.operation_stat = lambda cid, path=None: FakeResponse(
        {"name": "f.txt", "type": "file", "size": 12}
    )
    assert TransferMetadata(client).info(UUID, "f.txt")["size"] == 12


def _rebuild_meta_app(**kwargs):
    """Module-level, picklable by reference."""
    return object()


def test_metadata_without_respawn_pickles_the_client():
    """Test fakes carry no secrets, so this path stays permissive."""
    import pickle

    restored = pickle.loads(pickle.dumps(TransferMetadata(FakeClient())))
    assert isinstance(restored, TransferMetadata)


def test_metadata_respawn_keeps_the_token_out_of_the_pickle():
    """Regression: a live TransferClient smuggled the bearer token.

    AppCredentials.__reduce__ alone was not enough -- the filesystem also
    holds TransferMetadata, whose client holds an authorizer holding the
    token. The full pickle was ~15 KB of mostly credential material with
    the token recoverable from the bytes.
    """
    import pickle

    class TokenBearingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.authorizer_token = "secret-bearer-value"

    meta = TransferMetadata(
        TokenBearingClient(), respawn=(_rebuild_meta_app, {"collection_id": "x"})
    )
    blob = pickle.dumps(meta)
    assert b"secret-bearer-value" not in blob
    assert len(blob) < 1000, "a recipe should be small; a live client is not"
