"""Credential providers for Globus collections.

Kept separate from the filesystem because token acquisition is the part
that varies: a public collection needs nothing, a portal has a token
already, and an interactive user needs a browser login. The filesystem
only ever asks for a bearer string.

Two constraints drive the design here, both from the globus-sdk docs:

* ``GlobusApp`` is explicitly **not thread safe**, but fsspec shares one
  filesystem instance across threads. Anything wrapping an app has to
  serialize access.
* fsspec pickles filesystems to worker processes
  (``AbstractFileSystem.__reduce__`` re-instantiates from the constructor
  args). A live bearer token in those args would be written into every
  worker payload, so providers serialize the *recipe* for getting a
  token, never the token itself.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class GlobusCredentials(Protocol):
    """Supplies bearer tokens for Globus collections."""

    def token_for(self, collection_id: str) -> str | None:
        """Return a bearer token for ``collection_id``, or None for anonymous.

        Must be safe to call from multiple threads.
        """
        ...


class AnonymousCredentials:
    """No credentials; for collections that allow public read.

    The default. Real collections will 401, which is the correct and
    legible failure -- better than pulling in an auth dependency that
    most reads of public data would not need.
    """

    def token_for(self, collection_id: str) -> str | None:
        return None

    def __repr__(self) -> str:
        return "AnonymousCredentials()"


class StaticToken:
    """A token the caller already has.

    Intended for portals and services that run their own OAuth flow, and
    for tests. Note the token lives in memory for the life of the object;
    ``__reduce__`` deliberately refuses to pickle it rather than silently
    copying a secret into worker processes.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def token_for(self, collection_id: str) -> str | None:
        return self._token

    def __reduce__(self):
        raise TypeError(
            "StaticToken refuses to pickle: this would copy a bearer token "
            "into every worker process and into any serialized fsspec state. "
            "Use CallableToken with a function that reads the token from the "
            "environment or a file, so workers re-read it instead."
        )

    def __repr__(self) -> str:
        # Never render the token.
        return "StaticToken(token=<redacted>)"


class CallableToken:
    """Fetch a token on demand from a user-supplied callable.

    The pickle-safe way to carry credentials into dataloader workers: the
    *function* crosses the process boundary, the secret does not. Point it
    at an env var or a file on shared storage.

    The callable may be invoked concurrently, so it must be thread-safe.
    """

    def __init__(self, fn: Callable[[str], str | None]) -> None:
        self._fn = fn

    def token_for(self, collection_id: str) -> str | None:
        return self._fn(collection_id)

    def __repr__(self) -> str:
        return f"CallableToken(fn={getattr(self._fn, '__name__', '<callable>')})"


class AppCredentials:
    """Wrap a globus_sdk ``UserApp``/``ClientApp``.

    Every call is serialized through a lock because GlobusApp is not
    thread safe and fsspec will share this across threads.

    Two things to get right when constructing the app:

    * Set ``request_refresh_tokens=True``. It defaults to False, and
      without it a long training run dies when the access token expires
      mid-epoch.
    * Point token storage at a path on shared storage. A UserApp cannot
      run a browser login inside a dataloader worker, so workers must be
      able to *load* tokens rather than acquire them.
    """

    def __init__(self, app, scope_suffix: str = "https") -> None:
        self._app = app
        self._scope_suffix = scope_suffix
        self._lock = threading.Lock()

    def token_for(self, collection_id: str) -> str | None:
        """Current access token for a collection, refreshing if needed.

        Goes through ``GlobusApp.get_authorizer()`` rather than reading
        stored token data directly: the authorizer owns refresh, so an
        expired access token is renewed here instead of failing a read
        partway through a long job.
        """
        with self._lock:
            authorizer = self._app.get_authorizer(collection_id)
            # RefreshTokenAuthorizer renews on demand; others just hold one.
            ensure = getattr(authorizer, "ensure_valid_token", None)
            if ensure is not None:
                ensure()
            return getattr(authorizer, "access_token", None)

    def __repr__(self) -> str:
        return f"AppCredentials(scope_suffix={self._scope_suffix!r})"
