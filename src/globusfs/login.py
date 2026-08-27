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


def collection_scopes(collection_id: str, data_access: bool = True) -> list[str]:
    """Scopes needed to read one collection over HTTPS.

    ``data_access`` is required for mapped collections that are not High
    Assurance, and harmless to request otherwise.
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
    data_access: bool = True,
):
    """Run the browser login flow and persist tokens.

    Returns a ``globus_sdk.UserApp`` usable for both Transfer and HTTPS.
    """
    try:
        import globus_sdk
        from globus_sdk.tokenstorage import JSONTokenStorage
    except ImportError as exc:  # pragma: no cover
        raise ImportError("pip install 'globusfs[auth]'") from exc

    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    requirements = {"transfer.api.globus.org": [TRANSFER_SCOPE]}
    if collection_id:
        requirements[collection_id] = collection_scopes(collection_id, data_access)

    config = globus_sdk.GlobusAppConfig(
        token_storage=JSONTokenStorage(str(token_path)),
        # Not the SDK default; without it long runs die on token expiry.
        request_refresh_tokens=True,
    )
    return globus_sdk.UserApp(
        "globusfs", client_id=client_id, config=config, scope_requirements=requirements
    )


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
