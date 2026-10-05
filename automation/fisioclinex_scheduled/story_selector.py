"""Read-only Story selection independent of feed slots and business days."""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .manifest import parse_manifest
from .registry import read_registry, read_story_registry
from .story_state import StoryState
from .story_timing import initial_story_not_before


@dataclass(frozen=True)
class DueStory:
    short_slug: str
    publication_key: str
    not_before: datetime


def select_due_story(repository_root, *, now: datetime) -> DueStory | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("tick must be timezone-aware")
    root = Path(repository_root).resolve(strict=True)
    queue = root / "publication-state/queue"
    if queue.is_symlink():
        raise ValueError("unsafe queue")
    registry = root / "publication-state/publications.jsonl"
    feeds = read_registry(registry)
    registered = {r.publication_key for r in read_story_registry(registry)}
    due = []
    for path in sorted(queue.glob("*/manifest.json")):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("unsafe manifest")
        manifest = parse_manifest(json.loads(path.read_bytes()))
        story = manifest.story
        if (story is None or manifest.status.value != "published"
                or not manifest.publication.published_at or not manifest.publication.media_id):
            continue
        if not any(r.publication_key == manifest.publication_key
                   and r.media_id == manifest.publication.media_id
                   and datetime.fromisoformat(r.published_at.replace("Z", "+00:00")) == manifest.publication.published_at
                   for r in feeds):
            continue
        initial = story.status in {StoryState.PENDING, StoryState.ELIGIBLE} and story.attempt_count == 0 and story.retry_allowed is not False
        retry = (story.status in {StoryState.FAILED, StoryState.ELIGIBLE}
                 and story.attempt_count == 1 and story.retry_allowed is True
                 and story.last_error is not None
                 and story.last_error.classification == "confirmed_not_published")
        if (not (initial or retry) or story.publication_key in registered
                or story.not_before is None or now < story.not_before):
            continue
        # A legacy/preliminary barrier cannot advance a late confirmed feed.
        if initial and story.not_before < initial_story_not_before(manifest.publication.published_at):
            continue
        if retry and story.not_before < (story.last_error.occurred_at.astimezone(timezone.utc) + timedelta(minutes=30)):
            continue
        due.append(DueStory(manifest.short_slug, story.publication_key, story.not_before))
    return min(due, key=lambda item: (item.not_before, item.publication_key, item.short_slug), default=None)
