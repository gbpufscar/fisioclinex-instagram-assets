"""Explicit first Story attempt only. No scheduler, retry or Meta reconciliation."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .manifest import parse_manifest
from .meta_client import MetaClientError
from .publication_state import change_story_state
from .publication_writeback import append_registry, execution_locked, persist, write_manifest
from .queue_pages import verify_story_path
from .registry import read_registry, read_story_registry
from .shadow_runner import _validate_package
from .story_state import StoryState


class StoryPublicationError(RuntimeError):
    def __init__(self, phase: str):
        self.phase = phase
        super().__init__(f"Story action interrupted: {phase}")


def _persist(root, path, data, *, git_runner, registry=False):
    write_manifest(path, data)
    paths = (path,)
    if registry:
        parent = parse_manifest(data)
        story = parent.story
        registry_path = root / "publication-state/publications.jsonl"
        append_registry(registry_path, {
            "schema_version": 2, "surface": "story",
            "publication_key": story.publication_key,
            "feed_publication_key": parent.publication_key, "slug": parent.slug,
            "media_id": story.media_id, "published_at": story.published_at.isoformat(),
        })
        paths += (registry_path,)
    persist(root, paths=paths, message=f"queue: registrar Story {data['slug']}", git_runner=git_runner)


@execution_locked
def publish_story(repository_root, *, short_slug, confirmation, fetcher, meta_client,
                  git_runner, now_fn=lambda: datetime.now(timezone.utc)) -> dict:
    """Caller explicitly supplies an eligible, never-attempted S2 child.

    Lock covers validation through writeback locally. A successful Git push of
    publishing fences Actions workers in separate clones before any Meta call.
    A lost response/writeback remains publishing/ambiguous and cannot be retried.
    """
    root = Path(repository_root).resolve(strict=True)
    if (not isinstance(short_slug, str) or not short_slug
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in short_slug)
            or confirmation != f"PUBLICAR STORY {short_slug}"):
        raise StoryPublicationError("authorization")
    path = root / "publication-state/queue" / f"fisioclinex-{short_slug}" / "manifest.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise StoryPublicationError("prepare")
    try:
        import json
        data = json.loads(path.read_bytes())
        manifest = parse_manifest(data)
        story = manifest.story
        if (manifest.status.value != "published" or not manifest.publication.media_id
                or not manifest.publication.published_at or story is None):
            raise StoryPublicationError("feed_not_published")
        registry_path = root / "publication-state/publications.jsonl"
        feeds = read_registry(registry_path)
        if not any(r.publication_key == manifest.publication_key
                   and r.media_id == manifest.publication.media_id
                   and datetime.fromisoformat(r.published_at.replace("Z", "+00:00")) == manifest.publication.published_at
                   for r in feeds):
            raise StoryPublicationError("feed_writeback_incomplete")
        if any(r.publication_key == story.publication_key for r in read_story_registry(registry_path)):
            raise StoryPublicationError("story_already_registered")
        # S5A does not reopen failed children or execute a second attempt.
        if story.status != StoryState.ELIGIBLE or story.attempt_count != 0:
            raise StoryPublicationError("story_not_eligible")
        now = now_fn()
        if story.not_before is None or now < story.not_before:
            raise StoryPublicationError("story_barrier")
        _validate_package(root, path, manifest)
        url = verify_story_path(manifest.slug, root / "posts" / manifest.slug / f"{manifest.slug}-story.png",
                                fetcher=fetcher)
        working = change_story_state(data, StoryState.PUBLISHING, now=now)
        _persist(root, path, working, git_runner=git_runner)
    except StoryPublicationError:
        raise
    except Exception:
        raise StoryPublicationError("prepare_or_fence") from None

    phase = "create_story"
    container = None
    publish_started = False
    try:
        container = meta_client.create_story(url)
        if not isinstance(container, str) or not container:
            raise StoryPublicationError("create_story")
        working['story']['container_id'] = container
        # Save known ID before readiness/publish. Do not classify write errors as Meta failures.
        _persist(root, path, working, git_runner=git_runner)
        phase = "story_ready"
        meta_client.wait_finished(container)
        phase = "publish_story"
        publish_started = True
        media_id = meta_client.publish(container)
        if not isinstance(media_id, str) or not media_id:
            raise StoryPublicationError("publish_story")
    except Exception as exc:
        if phase == "create_story" and container is not None:
            raise StoryPublicationError("container_writeback") from None
        # Before media_publish no Story can be live. After it, only explicit
        # conclusive client evidence permits failed; unknown exceptions are ambiguous.
        conclusive = (isinstance(exc, MetaClientError) and not exc.ambiguous
                      and exc.http_status is not None and 400 <= exc.http_status < 500)
        target = StoryState.AMBIGUOUS if publish_started and not conclusive else StoryState.FAILED
        failed = change_story_state(working, target, now=now_fn(), phase=phase, container_id=container)
        try:
            _persist(root, path, failed, git_runner=git_runner)
        except Exception:
            raise StoryPublicationError("failure_writeback") from None
        return {"status": target.value, "publication_key": story.publication_key}

    completed = change_story_state(working, StoryState.PUBLISHED, now=now_fn(),
                                   container_id=container, media_id=media_id)
    try:
        _persist(root, path, completed, git_runner=git_runner, registry=True)
    except Exception:
        # Disk holds publishing or published. Neither permits another Meta call.
        raise StoryPublicationError("success_writeback") from None
    return {"status": "published", "publication_key": story.publication_key,
            "media_id": media_id, "published_at": completed['story']['published_at']}
