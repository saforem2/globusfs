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
