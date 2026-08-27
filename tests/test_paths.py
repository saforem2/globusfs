"""URL parsing: globus://<collection-uuid>/path."""

import pytest

from globusfs import GlobusFileSystem

BASE = "https://example.data.globus.org"
UUID = "6c54cade-bde5-45c1-bdea-f4bd71dba2cc"


def fs(**kw):
    return GlobusFileSystem(https_url=BASE, skip_instance_cache=True, **kw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (f"globus://{UUID}/a/b.parquet", f"{UUID}/a/b.parquet"),
        (f"globus://{UUID}/a/b.parquet/", f"{UUID}/a/b.parquet"),
        (f"{UUID}/a/b.parquet", f"{UUID}/a/b.parquet"),
    ],
)
def test_strip_protocol(raw, expected):
    assert GlobusFileSystem._strip_protocol(raw) == expected


def test_split_path_uses_authority_as_collection():
    assert fs().split_path(f"globus://{UUID}/a/b") == (UUID, "a/b")


def test_split_path_with_default_collection():
    """With a default set, a bare path needs no UUID."""
    assert fs(collection_id=UUID).split_path("a/b") == (UUID, "a/b")


def test_explicit_uuid_matching_default_is_not_double_counted():
    assert fs(collection_id=UUID).split_path(f"globus://{UUID}/a/b") == (UUID, "a/b")


def test_no_collection_anywhere_is_an_error():
    with pytest.raises(ValueError, match="No collection"):
        fs().split_path("")


def test_url_construction():
    assert fs().split_path(f"globus://{UUID}/a/b.parquet")[1] == "a/b.parquet"
    assert fs()._url(f"globus://{UUID}/a/b.parquet") == f"{BASE}/a/b.parquet"


def test_listing_fails_loudly():
    """Unimplemented surface must raise, not silently return nothing."""
    with pytest.raises(NotImplementedError, match="Transfer API"):
        fs(collection_id=UUID).ls("a")
    with pytest.raises(NotImplementedError, match="Transfer API"):
        fs(collection_id=UUID).info("a")
