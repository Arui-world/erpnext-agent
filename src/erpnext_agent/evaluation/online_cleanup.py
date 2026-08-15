"""Test-only cleanup registry for drafts created by online evaluation runs.

This is a *test-cleanup* concern. It tracks the draft documents the current run
created so they can be deleted afterwards. The actual deletion is performed by the
online runner through an injected deleter (production wires it to ERPNext's REST
resource endpoint using the owning user's own token); the Agent production path and
the MCP tool allowlist are untouched.

Safety rails live here and in the runner:
- Only ``(doctype, name)`` pairs explicitly registered during this run are deletable.
- Deletion is idempotent: HTTP 2xx and 404 both count as success.
- Cleanup never raises; failures are recorded as evidence for manual follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CleanupTarget:
    doctype: str
    name: str
    credential_id: str


class DraftCleanupRegistry:
    """Tracks drafts created during this evaluation run."""

    def __init__(self) -> None:
        self._targets: dict[tuple[str, str], CleanupTarget] = {}

    def register(self, *, doctype: str, name: str, credential_id: str) -> None:
        if not doctype or not name:
            return
        self._targets[(doctype, name)] = CleanupTarget(
            doctype=doctype,
            name=name,
            credential_id=credential_id,
        )

    def pending(self) -> list[CleanupTarget]:
        return list(self._targets.values())

    def forget(self, *, doctype: str, name: str) -> None:
        self._targets.pop((doctype, name), None)

    def __len__(self) -> int:
        return len(self._targets)
