"""Post-register recovery email binder (pure Python, sync Playwright)."""
from __future__ import annotations

from .config import BindConfig, DomainEntry, DEFAULT_DOMAINS
from .runner import BindResult, bind_recovery_emails

__all__ = [
    "BindConfig",
    "BindResult",
    "DomainEntry",
    "DEFAULT_DOMAINS",
    "bind_recovery_emails",
]
