"""Verified Assets snapshots for operational planning; no Studio fallback."""
import subprocess
from pathlib import Path

from .editorial_calendar import EditorialCalendarError, load_calendar, load_policy
from .workspace_sync import synchronize_workspace


def run_git(args, *, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)

CALENDAR = "publication-state/editorial-calendar.json"
POLICY = "publication-state/editorial-calendar-policy.json"


def read_current(root, *, branch="main", runner=run_git):
    try:
        root = Path(root).resolve(strict=True)
        synchronize_workspace(root, branch=branch, runner=runner)
        result = runner(("rev-parse", "HEAD"), cwd=root)
        if result.returncode:
            raise RuntimeError("HEAD unavailable")
        policy = load_policy(root / POLICY)
        return result.stdout.strip(), policy, load_calendar(root / CALENDAR, policy=policy)
    except Exception as exc:
        raise EditorialCalendarError(
            "Não foi possível verificar o calendário operacional atual. "
            "A publicação não será agendada até que a disponibilidade seja confirmada no Assets."
        ) from exc


def revalidate(root, expected_head, *, branch="main", runner=run_git):
    snapshot = read_current(root, branch=branch, runner=runner)
    if snapshot[0] != expected_head:
        raise EditorialCalendarError("calendar_changed_since_planning")
    return snapshot
