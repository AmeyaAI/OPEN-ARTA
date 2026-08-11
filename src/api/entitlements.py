"""Entitlements seam — the open-core boundary, in code.

The open core ships a no-op implementation that allows everything a single
team needs, forever (see README "Free forever"). Commercial editions (the
`ee/` tree and ARTA Cloud) provide their own implementation for org-level
capabilities: organizations, cross-project analytics, SAML/SCIM, audit
trails, fleet management.

Core code may call `get_entitlements().allows("<capability>")` before an
org-level feature; it must NEVER gate anything in the local loop
(generation, execution, validation, healing, reporting, single-project
traceability).
"""
from __future__ import annotations


class Entitlements:
    """Allow-everything default. Commercial editions subclass this."""

    edition = "community"

    def allows(self, capability: str) -> bool:  # noqa: ARG002 — uniform signature
        return True


_active = Entitlements()


def get_entitlements() -> Entitlements:
    return _active


def set_entitlements(impl: Entitlements) -> None:
    """Called by an ee/ edition at startup to install its implementation."""
    global _active
    _active = impl
