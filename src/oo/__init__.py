"""oo: hop into your Ona environments by name."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("oo")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "dev"
