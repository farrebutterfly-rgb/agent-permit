"""agent-permit: a human approves one exact agent action, from the phone, before it happens."""

__version__ = "0.1.0"

from .store import Permit, Store, fingerprint  # noqa: E402

__all__ = ["Permit", "Store", "fingerprint", "__version__"]
