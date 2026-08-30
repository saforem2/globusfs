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
    app = globusfs.login(UUID, token_path=tmp_path / "tokens.json", run_flow=False)
    assert type(app).__name__ == "UserApp"


def test_login_requests_refresh_tokens(tmp_path):
    """Not the SDK default. Without it, long runs die on token expiry."""
    app = globusfs.login(UUID, token_path=tmp_path / "tokens.json", run_flow=False)
    assert app.config.request_refresh_tokens is True


def test_login_requests_transfer_and_collection_scopes(tmp_path):
    """Both services are needed: Transfer for ls/info, https for bytes."""
    app = globusfs.login(UUID, token_path=tmp_path / "tokens.json", run_flow=False)
    reqs = {rs: [str(s) for s in sc] for rs, sc in app.scope_requirements.items()}
    # Prefix match, not equality: Transfer's scope carries data_access as
    # a dependency -- see test_data_access_is_a_dependent_scope_of_transfer.
    assert reqs["transfer.api.globus.org"][0].startswith(
        "urn:globus:auth:scope:transfer.api.globus.org:all"
    )
    assert any(s.endswith("/https") for s in reqs[UUID])
    assert any(s.endswith("/data_access") for s in reqs[UUID])


def test_token_parent_directory_is_created(tmp_path):
    """Workers load tokens from disk; the path must exist to be written."""
    target = tmp_path / "nested" / "dir" / "tokens.json"
    globusfs.login(UUID, token_path=target, run_flow=False)
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


def test_login_actually_runs_the_flow(tmp_path, monkeypatch):
    """Regression: login() used to construct an app and never log in.

    It returned cleanly, wrote no token file, and the missing
    authentication only surfaced later as an unexpected browser prompt
    from inside an unrelated read.
    """
    calls = []

    class FakeApp:
        def __init__(self, *a, **kw):
            self.config = kw.get("config")

        def login_required(self):
            return True

        def login(self):
            calls.append("login")

    monkeypatch.setattr(globus_sdk, "UserApp", FakeApp)
    # data_access=False skips the detection probe, which would otherwise
    # log in once for Transfer before re-entering with the answer.
    globusfs.login(UUID, token_path=tmp_path / "t.json", data_access=False)
    assert calls == ["login"], "login() must run the flow, not just build the app"


def test_login_does_not_reprompt_when_tokens_exist(tmp_path, monkeypatch):
    """Re-running login() with valid tokens must be a no-op."""
    calls = []

    class FakeApp:
        def __init__(self, *a, **kw):
            self.config = kw.get("config")

        def login_required(self):
            return False

        def login(self):
            calls.append("login")

    monkeypatch.setattr(globus_sdk, "UserApp", FakeApp)
    globusfs.login(UUID, token_path=tmp_path / "t.json", data_access=False)
    assert calls == []


def test_data_access_is_a_dependent_scope_of_transfer():
    """Regression: data_access must be nested inside the transfer scope.

    Requested flat, the login succeeds and the tokens look complete, but
    every operation_ls fails with 403 ConsentRequired:

        urn:globus:auth:scope:transfer.api.globus.org:all[*.../data_access]

    is the form the API demands.
    """
    app = globusfs.login(UUID, token_path="/tmp/globusfs-test.json", run_flow=False)
    transfer = [str(s) for s in app.scope_requirements["transfer.api.globus.org"]]
    assert len(transfer) == 1
    assert transfer[0].startswith("urn:globus:auth:scope:transfer.api.globus.org:all[")
    assert f"{UUID}/data_access" in transfer[0]


def test_data_access_can_be_disabled():
    """High Assurance collections do not use data_access."""
    app = globusfs.login(
        UUID,
        token_path="/tmp/globusfs-test.json",
        data_access=False,
        run_flow=False,
    )
    transfer = [str(s) for s in app.scope_requirements["transfer.api.globus.org"]]
    assert transfer == ["urn:globus:auth:scope:transfer.api.globus.org:all"]


def test_data_access_is_detected_per_collection_type():
    """Guest collections must not be asked for data_access.

    Only mapped, non-High-Assurance collections use it. Requesting it on
    a guest collection costs the user a consent prompt for nothing --
    found when a real ALCF guest collection prompted needlessly.
    """
    from globusfs.login import needs_data_access

    class Client:
        def __init__(self, doc):
            self.doc = doc

        def get_endpoint(self, cid):
            return self.doc

    assert needs_data_access(UUID, Client({"entity_type": "GCSv5_mapped_collection"}))
    assert not needs_data_access(
        UUID, Client({"entity_type": "GCSv5_guest_collection"})
    )
    assert not needs_data_access(
        UUID, Client({"entity_type": "GCSv5_mapped_collection", "high_assurance": True})
    )


def test_data_access_detection_defaults_to_true_when_unknown():
    """Erring toward a needless prompt beats omitting a required scope."""
    from globusfs.login import needs_data_access

    class Broken:
        def get_endpoint(self, cid):
            raise RuntimeError("network down")

    assert needs_data_access(UUID) is True  # no client
    assert needs_data_access(UUID, Broken()) is True


def test_uuid_detection():
    from globusfs.login import is_uuid

    assert is_uuid(UUID)
    assert is_uuid(UUID.upper())
    assert is_uuid(f"  {UUID}  ")
    assert not is_uuid("alcf#dtn_eagle")
    assert not is_uuid("my-collection")
    assert not is_uuid("")


class _SearchClient:
    """Fake TransferClient for endpoint_search."""

    def __init__(self, results):
        self.results = results

    def endpoint_search(self, name, **kw):
        return self.results


def test_display_name_resolves_to_uuid():
    """Regression: a display name went straight into a scope URL.

    `globusfs.filesystem("alcf#dtn_eagle")` built
    `https://auth.globus.org/scopes/alcf#dtn_eagle/https`, which Globus
    rejects with UNKNOWN_SCOPE_ERROR -- and the `#` truncates the URL as
    a fragment, making the message doubly confusing. ALCF publishes
    names rather than UUIDs, so names must resolve.
    """
    from globusfs.login import resolve_collection

    client = _SearchClient([{"id": UUID, "display_name": "alcf#dtn_eagle"}])
    assert resolve_collection("alcf#dtn_eagle", client) == UUID


def test_uuid_passes_through_without_a_lookup():
    from globusfs.login import resolve_collection

    class Boom:
        def endpoint_search(self, *a, **k):
            raise AssertionError("must not search when given a UUID")

    assert resolve_collection(UUID, Boom()) == UUID


def test_unknown_name_is_a_clear_error():
    from globusfs.login import resolve_collection

    with pytest.raises(ValueError, match="No Globus collection named"):
        resolve_collection("nope", _SearchClient([]))


def test_ambiguous_name_refuses_to_guess():
    """Two collections sharing a name must not silently pick one."""
    from globusfs.login import resolve_collection

    dupes = [
        {"id": UUID, "display_name": "shared"},
        {"id": "11111111-2222-3333-4444-555555555555", "display_name": "shared"},
    ]
    with pytest.raises(ValueError, match="matches 2 collections"):
        resolve_collection("shared", _SearchClient(dupes))


def test_partial_search_matches_are_ignored():
    """endpoint_search is fuzzy; only an exact name counts."""
    from globusfs.login import resolve_collection

    fuzzy = [{"id": UUID, "display_name": "alcf#dtn_eagle_dashboard"}]
    with pytest.raises(ValueError, match="No Globus collection named"):
        resolve_collection("alcf#dtn_eagle", _SearchClient(fuzzy))


def test_name_without_client_explains_the_fix():
    from globusfs.login import resolve_collection

    with pytest.raises(ValueError, match="not a collection UUID"):
        resolve_collection("alcf#dtn_eagle")
