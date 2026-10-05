"""One locked, fenced Story action per enabled tick; no automatic reconciliation."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .manifest import parse_manifest
from .publication_state import change_story_state
from .publication_writeback import execution_locked
from .registry import read_story_registry
from .story_publisher import publish_story, reconcile_story, _persist
from .story_selector import select_due_story
from .story_state import StoryState


def run_story_scheduler(repository_root, *, enabled=False, fetcher=None,
                        meta_client=None, git_runner=None,
                        now_fn=lambda: datetime.now(timezone.utc)):
    # Disabled means no lock, queue read, selection, credential access or Meta.
    if enabled is not True:
        return {"status": "disabled", "selected": False}
    return _run_enabled(repository_root, fetcher=fetcher, meta_client=meta_client,
                        git_runner=git_runner, now_fn=now_fn)


@execution_locked
def _run_enabled(repository_root, *, fetcher, meta_client, git_runner, now_fn):
    root = Path(repository_root).resolve(strict=True)
    candidate = select_due_story(root, now=now_fn())
    if candidate is None:
        return {"status": "idle", "selected": False}
    # Re-read after selection. Only durable registry evidence permits repair;
    # this path never queries Meta to reconcile publishing/ambiguous states.
    registered = read_story_registry(root / "publication-state/publications.jsonl")
    if any(r.publication_key == candidate.publication_key for r in registered):
        return reconcile_story.__wrapped__(root, short_slug=candidate.short_slug,
            confirmation=f"RECONCILIAR STORY {candidate.short_slug}",
            meta_client=None, git_runner=git_runner, now_fn=now_fn)
    path = root / "publication-state/queue" / f"fisioclinex-{candidate.short_slug}" / "manifest.json"
    data = json.loads(path.read_bytes())
    if parse_manifest(data).story.status == StoryState.PENDING:
        data = change_story_state(data, StoryState.ELIGIBLE, now=now_fn())
        _persist(root, path, data, git_runner=git_runner)
    return publish_story.__wrapped__(root, short_slug=candidate.short_slug,
        confirmation=f"PUBLICAR STORY {candidate.short_slug}", fetcher=fetcher,
        meta_client=meta_client, git_runner=git_runner, now_fn=now_fn)
