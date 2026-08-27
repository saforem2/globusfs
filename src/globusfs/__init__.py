"""fsspec filesystem for Globus collections."""

from .core import GlobusFileSystem
from .credentials import (
    AnonymousCredentials,
    AppCredentials,
    CallableToken,
    GlobusCredentials,
    StaticToken,
)
from .errors import GlobusFSError, TransientBackendError
from .login import filesystem, login
from .transfer import TransferMetadata

__version__ = "0.1.0.dev0"

__all__ = [
    "AnonymousCredentials",
    "AppCredentials",
    "CallableToken",
    "GlobusCredentials",
    "GlobusFSError",
    "GlobusFileSystem",
    "StaticToken",
    "TransferMetadata",
    "TransientBackendError",
    "filesystem",
    "login",
]
