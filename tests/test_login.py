"""Login-path construction.

These exist because the original `login()` had a line that had never
been executed: it imported `globus_sdk.tokenstorage`, which was renamed
to `globus_sdk.token_storage` in globus-sdk v4. Worse, the surrounding
try/except reported it as "globus-sdk is not installed" -- sending users
to install a package they already had, while hiding the real cause.

Two lessons encoded here: exercise the construction path, and never let
an ImportError handler swallow an error it does not actually describe.
"""

import sys

import pytest

globus_sdk = pytest.importorskip("globus_sdk")

import globusfs
from globusfs.login import _json_token_storage, _require_sdk

UUID = "6c54cade-bde5-45c1-bdea-f4bd71dba2cc"


def test_token_storage_resolves_on_installed_sdk():
    """Whichever globus-sdk is present, we must find JSONTokenStorage."""
    assert _json_token_storage(_require_sdk()) is not None


def test_token_storage_falls_back_to_v3_module_name(monkeypatch):
    """v3 (`tokenstorage`) must work as well as v4 (`token_storage`).

    Simulates the v3 layout: hide the v4 module, publish a stand-in under
    the v3 name, and check the fallback picks it up.
    """
    import types

    sentinel = object()
    v3 = types.ModuleType("globus_sdk.tokenstorage")
    v3.JSONTokenStorage = sentinel
    monkeypatch.setitem(sys.modules, "globus_sdk.token_storage", None)
    monkeypatch.setitem(sys.modules, "globus_sdk.tokenstorage", v3)
    assert _json_token_storage(_require_sdk()) is sentinel


def test_missing_storage_error_names_the_real_problem(monkeypatch):
    """A failure must not claim globus-sdk is uninstalled when it isn't."""
    monkeypatch.setitem(sys.modules, "globus_sdk.token_storage", None)
    monkeypatch.setitem(sys.modules, "globus_sdk.tokenstorage", None)
    with pytest.raises(ImportError) as exc:
        _json_token_storage(_require_sdk())
    msg = str(exc.value)
    assert "JSONTokenStorage" in msg
    assert "pip install" not in msg, "must not blame a missing package"


def test_login_constructs_without_browser(tmp_path):
    """Build the app end to end; only the actual login needs a human."""
    app = globusfs.login(UUID, token_path=tmp_path / "tokens.json")
    assert type(app).__name__ == "UserApp"


def test_login_requests_refresh_tokens(tmp_path):
    """Not the SDK default. Without it, long runs die on token expiry."""
    app = globusfs.login(UUID, token_path=tmp_path / "tokens.json")
    assert app.config.request_refresh_tokens is True


def test_login_requests_transfer_and_collection_scopes(tmp_path):
    """Both services are needed: Transfer for ls/info, https for bytes."""
    app = globusfs.login(UUID, token_path=tmp_path / "tokens.json")
    reqs = {rs: [str(s) for s in sc] for rs, sc in app.scope_requirements.items()}
    assert (
        "urn:globus:auth:scope:transfer.api.globus.org:all"
        in reqs["transfer.api.globus.org"]
    )
    assert any(s.endswith("/https") for s in reqs[UUID])
    assert any(s.endswith("/data_access") for s in reqs[UUID])


def test_token_parent_directory_is_created(tmp_path):
    """Workers load tokens from disk; the path must exist to be written."""
    target = tmp_path / "nested" / "dir" / "tokens.json"
    globusfs.login(UUID, token_path=target)
    assert target.parent.is_dir()


class _FakeAuthorizer:
    def __init__(self, token="tok-123"):
        self.access_token = token
        self.ensured = 0

    def ensure_valid_token(self):
        self.ensured += 1


class _FakeApp:
    """Stands in for a logged-in GlobusApp."""

    def __init__(self, authorizer=None):
        self._authorizer = authorizer or _FakeAuthorizer()
        self.asked = []

    def get_authorizer(self, resource_server):
        self.asked.append(resource_server)
        return self._authorizer


def test_app_credentials_uses_public_authorizer_api():
    """Regression: an earlier version called a nonexistent get_token_data().

    GlobusApp's public surface is get_authorizer(resource_server); there
    is no get_token_data. Nothing caught it because the line had never
    been executed.
    """
    from globusfs.credentials import AppCredentials

    app = _FakeApp()
    assert AppCredentials(app).token_for(UUID) == "tok-123"
    assert app.asked == [UUID]


def test_app_credentials_refreshes_before_returning():
    """Refresh is the authorizer's job; make sure we actually ask.

    Without this an expired token fails a read mid-job rather than
    renewing.
    """
    from globusfs.credentials import AppCredentials

    auth = _FakeAuthorizer()
    AppCredentials(_FakeApp(auth)).token_for(UUID)
    assert auth.ensured == 1


def test_app_credentials_tolerates_authorizer_without_refresh():
    """Not every authorizer can refresh; those must still yield a token."""
    from globusfs.credentials import AppCredentials

    class NoRefresh:
        access_token = "static-tok"

    assert AppCredentials(_FakeApp(NoRefresh())).token_for(UUID) == "static-tok"


def test_app_credentials_is_thread_safe_by_serializing():
    """GlobusApp is documented as not thread-safe; fsspec shares instances."""
    import threading

    from globusfs.credentials import AppCredentials

    creds = AppCredentials(_FakeApp())
    out = []
    threads = [
        threading.Thread(target=lambda: out.append(creds.token_for(UUID)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == ["tok-123"] * 8
