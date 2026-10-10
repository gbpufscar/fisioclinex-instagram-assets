"""Safe fast-forward-only maintenance for the isolated publication workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceSyncError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceSyncResult:
    state: str
    ahead: int
    behind: int


def _require(result, operation: str) -> str:
    if result.returncode:
        raise WorkspaceSyncError(f"{operation} failed")
    return (result.stdout or "").strip()


def _divergence(value: str) -> tuple[int, int]:
    parts = value.split()
    if len(parts) != 2:
        raise WorkspaceSyncError("workspace divergence is invalid")
    try:
        ahead, behind = (int(item) for item in parts)
    except ValueError as exc:
        raise WorkspaceSyncError("workspace divergence is invalid") from exc
    if ahead < 0 or behind < 0:
        raise WorkspaceSyncError("workspace divergence is invalid")
    return ahead, behind


def synchronize_workspace(workspace: Path, *, branch: str, runner) -> WorkspaceSyncResult:
    """Fetch and fast-forward only a clean, behind-only publication workspace."""
    root = Path(workspace).resolve(strict=True)
    status = _require(
        runner(("status", "--porcelain=v1", "-z", "--untracked-files=all"), cwd=root),
        "workspace status",
    )
    if status:
        raise WorkspaceSyncError("publication workspace is dirty")
    upstream = _require(
        runner(("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"), cwd=root),
        "workspace upstream",
    )
    if upstream != f"origin/{branch}":
        raise WorkspaceSyncError("publication workspace upstream is missing or different")
    _require(runner(("fetch", "--prune", "origin", branch), cwd=root), "workspace fetch")
    ahead, behind = _divergence(
        _require(
            runner(("rev-list", "--left-right", "--count", "HEAD...@{upstream}"), cwd=root),
            "workspace divergence",
        )
    )
    if ahead:
        state = "diverged" if behind else "ahead"
        raise WorkspaceSyncError(f"publication workspace is {state}")
    if behind:
        _require(
            runner(("merge", "--ff-only", f"origin/{branch}"), cwd=root),
            "workspace fast-forward",
        )
        final = _divergence(
            _require(
                runner(("rev-list", "--left-right", "--count", "HEAD...@{upstream}"), cwd=root),
                "workspace divergence after fast-forward",
            )
        )
        if final != (0, 0):
            raise WorkspaceSyncError("publication workspace did not synchronize")
        return WorkspaceSyncResult("fast_forwarded", 0, behind)
    return WorkspaceSyncResult("up_to_date", 0, 0)
