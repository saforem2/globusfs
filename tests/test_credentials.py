"""Credential provider behavior, especially around secrets and pickling."""

import pickle

import pytest

from globusfs import AnonymousCredentials, CallableToken, StaticToken


def test_anonymous_returns_none():
    assert AnonymousCredentials().token_for("any-uuid") is None


def test_static_token_returns_token():
    assert StaticToken("tok-abc").token_for("any-uuid") == "tok-abc"


def test_static_token_refuses_to_pickle():
    """The whole point: a secret must not ride into worker processes.

    fsspec pickles filesystems to workers, so a provider that pickled its
    token would copy it into every worker payload.
    """
    with pytest.raises(TypeError, match="refuses to pickle"):
        pickle.dumps(StaticToken("tok-abc"))


def test_static_token_not_in_repr():
    """repr() lands in logs and tracebacks; the token must not."""
    assert "tok-abc" not in repr(StaticToken("tok-abc"))


def _token_from_env(collection_id):
    import os

    return os.environ.get("GLOBUS_TOKEN")


def test_callable_token_is_picklable():
    """The pickle-safe path: the function crosses, the secret does not."""
    provider = pickle.loads(pickle.dumps(CallableToken(_token_from_env)))
    assert isinstance(provider, CallableToken)


def test_callable_token_pickle_carries_no_secret(monkeypatch):
    monkeypatch.setenv("GLOBUS_TOKEN", "super-secret-value")
    provider = CallableToken(_token_from_env)
    assert provider.token_for("uuid") == "super-secret-value"
    assert b"super-secret-value" not in pickle.dumps(provider)


class _FakeApp:
    """Minimal stand-in for a logged-in GlobusApp."""

    def get_authorizer(self, resource_server):
        class A:
            access_token = "tok-from-store"

        return A()


def _rebuild_app(**kwargs):
    """Module-level so it is picklable by reference, like login()."""
    return _FakeApp()


def test_app_credentials_without_respawn_refuses_to_pickle():
    """Fail loudly rather than shipping a lock or a live token."""
    from globusfs.credentials import AppCredentials

    with pytest.raises(TypeError, match="respawn recipe"):
        pickle.dumps(AppCredentials(_FakeApp()))


def test_app_credentials_with_respawn_pickles():
    """Regression: a threading.Lock made the whole filesystem unpicklable.

    fsspec ships filesystems to worker processes, so this broke every
    multi-worker dataloader and dask cluster on the authenticated path.
    Nothing caught it because the tests pickled StaticToken and
    CallableToken -- never the object filesystem() actually returns.
    """
    from globusfs.credentials import AppCredentials

    creds = AppCredentials(_FakeApp(), respawn=(_rebuild_app, {"collection_id": "x"}))
    restored = pickle.loads(pickle.dumps(creds))
    assert restored.token_for("x") == "tok-from-store"


def test_app_credentials_pickle_carries_no_token():
    """The token must stay on disk, not ride into every worker payload."""
    from globusfs.credentials import AppCredentials

    creds = AppCredentials(_FakeApp(), respawn=(_rebuild_app, {"collection_id": "x"}))
    assert creds.token_for("x") == "tok-from-store"
    assert b"tok-from-store" not in pickle.dumps(creds)


def test_restored_credentials_are_still_picklable():
    """A worker may re-forward the filesystem; the recipe must survive."""
    from globusfs.credentials import AppCredentials

    creds = AppCredentials(_FakeApp(), respawn=(_rebuild_app, {"collection_id": "x"}))
    once = pickle.loads(pickle.dumps(creds))
    twice = pickle.loads(pickle.dumps(once))
    assert twice.token_for("x") == "tok-from-store"
