"""fsspec filesystem for Globus collections."""

from .core import GlobusFileSystem
from .credentials import (
    AnonymousCredentials,
    AppCredentials,
    CallableToken,
    GlobusCredentials,
    StaticToken,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "AnonymousCredentials",
    "AppCredentials",
    "CallableToken",
    "GlobusCredentials",
    "GlobusFileSystem",
    "StaticToken",
]
