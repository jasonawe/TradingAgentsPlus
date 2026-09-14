"""Permission tiers + scope policy (v3 spec §5).

Tools declare one of three permission tiers:
- READ: server-side, no approval
- WRITE: HITL required (create/update/delete state)
- WORKFLOW: long-running, async, requires session context
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable

from pydantic import BaseModel, Field


class PermissionType(str, Enum):
    READ = "read"
    WRITE = "write"
    WORKFLOW = "workflow"


class PermissionPolicy(BaseModel):
    """Scope-based allow/deny policy for HITL approval.

    Parameters
    ----------
    auto_approve_scopes:
        Symbol prefixes / scopes that skip HITL (e.g. ``["dev", "test"]``).
    require_approval_scopes:
        Symbol prefixes that always require approval.
    """

    auto_approve_scopes: list[str] = Field(default_factory=list)
    require_approval_scopes: list[str] = Field(default_factory=list)

    def requires_approval(self, scope: str) -> bool:
        s = (scope or "").lower()
        for prefix in self.auto_approve_scopes:
            if s.startswith(prefix.lower()):
                return False
        for prefix in self.require_approval_scopes:
            if s.startswith(prefix.lower()):
                return True
        return True

    def annotate(self, scopes: Iterable[str]) -> dict[str, bool]:
        return {s: self.requires_approval(s) for s in scopes}
