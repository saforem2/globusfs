"""Interactive login helper.

Authenticating to Globus requires a browser and a human identity, so this
cannot be automated. What it can do is make the flow one command and put
the tokens somewhere workers can read them.

Two settings here are load-bearing for HPC use:

* ``refresh_tokens=True``. The SDK default is False, and without it an
  access token expires (typically hours) and a long training run dies
  mid-epoch.
* Tokens land in a file, by default under ``~/.globusfs``. Dataloader
  workers cannot run a browser flow, so they must *load* tokens rather
  than acquire them. On a cluster, point this at shared storage.
"""

from __future__ import annotations

import os
from pathlib import Path

# Globus's public tutorial native client, intended for exactly this.
DEFAULT_CLIENT_ID = "61338d24-54d5-408f-a10d-66c06b59f6d2"
TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"

DEFAULT_TOKEN_PATH = Path(
    os.environ.get("GLOBUSFS_TOKENS", Path.home() / ".globusfs" / "tokens.json")
)


def needs_data_access(collection_id: str, client=None) -> bool:
    """True if a collection requires the ``data_access`` scope.

    Only mapped, non-High-Assurance collections do. Guest collections
    manage access through ACLs and High Assurance ones forbid it, so
    requesting it where it does not apply costs the user a consent
    prompt for nothing.

    Needs an authenticated ``TransferClient``: the Transfer API rejects
    unauthenticated metadata lookups (400, no credentials). Without one,
    or if the lookup fails, errs toward True -- a needless prompt is a
    smaller failure than omitting a scope that is genuinely required.
    """
    if client is None:
        return True
    try:
        doc = client.get_endpoint(collection_id)
    except Exception:  # noqa: BLE001 - detection must never block login
        return True
    if doc.get("high_assurance"):
        return False
    return "guest" not in (doc.get("entity_type") or "").lower()


def _require_sdk():
    """Import globus_sdk, or explain how to get it.

    Deliberately narrow: it reports a *missing package* only when the
    package is genuinely missing. An earlier version wrapped a wider
    block and told users to install a dependency they already had,
    hiding the real error (a renamed submodule).
    """
    try:
        import globus_sdk
    except ImportError as exc:  # pragma: no cover - trivial
        raise ImportError(
            "Logging in to Globus needs globus-sdk. "
            "Install with: pip install 'globusfs[auth]'"
        ) from exc
    return globus_sdk


def _json_token_storage(globus_sdk):
    """Locate ``JSONTokenStorage`` across globus-sdk versions.

    Renamed from ``globus_sdk.tokenstorage`` (v3) to
    ``globus_sdk.token_storage`` (v4). Both are supported, so the
    dependency floor does not have to jump a major version.
    """
    try:
        from globus_sdk.token_storage import JSONTokenStorage  # v4+
    except ImportError:
        try:
            from globus_sdk.tokenstorage import JSONTokenStorage  # v3
        except ImportError as exc:
            raise ImportError(
                f"Cannot locate JSONTokenStorage in globus-sdk "
                f"{getattr(globus_sdk, '__version__', '?')}: tried "
                f"globus_sdk.token_storage (v4) and globus_sdk.tokenstorage "
                f"(v3). Please report this with your globus-sdk version."
            ) from exc
    return JSONTokenStorage


def collection_scopes(collection_id: str, data_access: bool = True) -> list[str]:
    """Scopes needed to read one collection over HTTPS.

    ``data_access`` applies only to **mapped**, non-High-Assurance
    collections. Guest and High Assurance collections do not use it, and
    requesting it there forces an avoidable extra consent prompt -- so
    pass ``data_access=False`` for those. :func:`login` detects the
    collection type automatically when it can.
    """
    base = f"https://auth.globus.org/scopes/{collection_id}"
    scopes = [f"{base}/https"]
    if data_access:
        scopes.append(f"{base}/data_access")
    return scopes


def login(
    collection_id: str | None = None,
    client_id: str = DEFAULT_CLIENT_ID,
    token_path: Path | str = DEFAULT_TOKEN_PATH,
    data_access: bool | None = None,
    run_flow: bool = True,
):
    """Log in to Globus and persist tokens.

    Runs the login flow if the required scopes are not already held, then
    returns a ``globus_sdk.UserApp`` usable for both Transfer and HTTPS.
    Calling it again once tokens exist is cheap and does not re-prompt.

    Parameters
    ----------
    data_access:
        Whether to request the ``data_access`` scope. ``None`` (default)
        detects it: mapped non-High-Assurance collections need it, guest
        and High Assurance collections do not, and asking for it where it
        does not apply costs an extra consent prompt. Detection is a
        single unauthenticated metadata lookup; pass True/False to skip
        it.
    run_flow:
        Set False to build the app without logging in (tests, or when the
        caller drives the flow itself). The returned app will still
        trigger a login on first use if it lacks tokens.
    """
    globus_sdk = _require_sdk()
    JSONTokenStorage = _json_token_storage(globus_sdk)

    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    if data_access is None and collection_id:
        # Detection needs an authenticated client, so log in for Transfer
        # alone first; that consent is needed either way and is not
        # wasted. Then re-enter with the answer.
        probe = login(None, client_id, token_path, run_flow=run_flow)
        try:
            data_access = needs_data_access(
                collection_id, globus_sdk.TransferClient(app=probe)
            )
        except Exception:  # noqa: BLE001 - fall back to the common case
            data_access = True
    elif data_access is None:
        data_access = False

    # Transfer needs data_access as a *dependent* scope, written
    # `transfer:all[*<collection>/data_access]` -- not as a standalone
    # requirement. Requesting it flat yields tokens that look complete but
    # fail every operation_ls with 403 ConsentRequired.
    transfer_scope = globus_sdk.Scope(TRANSFER_SCOPE)
    if collection_id and data_access:
        transfer_scope = transfer_scope.with_dependency(
            globus_sdk.Scope(
                f"https://auth.globus.org/scopes/{collection_id}/data_access",
                optional=True,
            )
        )
    requirements = {"transfer.api.globus.org": [transfer_scope]}
    if collection_id:
        # The HTTPS scope stays a direct requirement of the collection
        # itself; only Transfer needs the dependent form.
        requirements[collection_id] = collection_scopes(collection_id, data_access)

    config = globus_sdk.GlobusAppConfig(
        token_storage=JSONTokenStorage(str(token_path)),
        # Not the SDK default; without it long runs die on token expiry.
        request_refresh_tokens=True,
    )
    app = globus_sdk.UserApp(
        "globusfs", client_id=client_id, config=config, scope_requirements=requirements
    )
    # Constructing the app does NOT authenticate. Without this the call
    # returns cleanly, writes no tokens, and the failure only shows up
    # later as a surprise login prompt from deep inside a read.
    if run_flow and app.login_required():
        app.login()
    return app


def filesystem(collection_id: str, app=None, **kwargs):
    """Build a ready-to-use :class:`GlobusFileSystem` for one collection.

    Convenience wrapper: logs in if needed, resolves the collection's
    HTTPS URL, and wires up Transfer-backed listing.
    """
    import globus_sdk

    from .core import GlobusFileSystem
    from .credentials import AppCredentials
    from .transfer import TransferMetadata

    app = app or login(collection_id)
    client = globus_sdk.TransferClient(app=app)
    return GlobusFileSystem(
        collection_id=collection_id,
        credentials=AppCredentials(app),
        metadata=TransferMetadata(client),
        **kwargs,
    )
