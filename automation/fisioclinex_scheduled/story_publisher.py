"""Explicit Story publishing with one safe deferred retry; no scheduler."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
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
    """Caller explicitly supplies an eligible S2 child or its single safe retry.

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
        if story.status == StoryState.FAILED and story.retry_allowed is True and story.attempt_count == 1:
            data = change_story_state(data, StoryState.ELIGIBLE, now=now_fn())
            manifest = parse_manifest(data); story = manifest.story
        if (story.status != StoryState.ELIGIBLE or story.attempt_count >= 2 or story.retry_allowed is False
                or (story.attempt_count == 1 and story.retry_allowed is not True)):
            raise StoryPublicationError("story_not_eligible")
        now = now_fn()
        if story.not_before is None or now < story.not_before:
            raise StoryPublicationError("story_barrier")
        _validate_package(root, path, manifest)
        url = verify_story_path(manifest.slug, root / "posts" / manifest.slug / f"{manifest.slug}-story.png",
                                fetcher=fetcher)
        working = change_story_state(data, StoryState.PUBLISHING, now=now)
        working["story"]["attempt_stage"] = "started"
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
        working['story']['attempt_stage'] = 'container_created'
        # Save known ID before readiness/publish. Do not classify write errors as Meta failures.
        _persist(root, path, working, git_runner=git_runner)
        phase = "story_ready"
        meta_client.wait_finished(container)
        phase = "publish_story"
        working["story"]["attempt_stage"] = "publish_requested"
        _persist(root, path, working, git_runner=git_runner)
        publish_started = True
        media_id = meta_client.publish(container)
        if not isinstance(media_id, str) or not media_id:
            raise StoryPublicationError("publish_story")
    except Exception as exc:
        if (phase == "create_story" and container is not None) or (phase == "publish_story" and not publish_started):
            raise StoryPublicationError("container_writeback") from None
        # Before media_publish no Story can be live. After it, only explicit
        # conclusive client evidence permits failed; unknown exceptions are ambiguous.
        conclusive = isinstance(exc, MetaClientError) and exc.confirmed_not_published
        possibly_published = publish_started or (phase == "story_ready" and isinstance(exc, MetaClientError) and exc.ambiguous)
        target = StoryState.AMBIGUOUS if possibly_published and not conclusive else StoryState.FAILED
        failed = change_story_state(working, target, now=now_fn(), phase=phase, container_id=container)
        if target == StoryState.FAILED:
            failed = plan_retry(failed, permanent=isinstance(exc, MetaClientError) and exc.permanent)
        try:
            _persist(root, path, failed, git_runner=git_runner)
        except Exception:
            raise StoryPublicationError("failure_writeback") from None
        return {"status": target.value, "publication_key": story.publication_key}

    completed = change_story_state(working, StoryState.PUBLISHED, now=now_fn(),
                                   container_id=container, media_id=media_id)
    completed["story"]["attempt_stage"] = "publish_confirmed"
    try:
        _persist(root, path, completed, git_runner=git_runner, registry=True)
    except Exception:
        # Disk holds publishing or published. Neither permits another Meta call.
        raise StoryPublicationError("success_writeback") from None
    return {"status": "published", "publication_key": story.publication_key,
            "media_id": media_id, "published_at": completed['story']['published_at']}


def plan_retry(data: dict, *, permanent=False) -> dict:
    """Persist one +30 barrier after conclusive failure; never publish here."""
    import copy
    manifest = parse_manifest(data); story = manifest.story
    if story.status != StoryState.FAILED or story.last_error.classification != "confirmed_not_published":
        raise StoryPublicationError("unsafe_retry")
    result = copy.deepcopy(data)
    permitted = not permanent and story.attempt_count < 2 and story.retry_allowed is not False
    result['story']['retry_allowed'] = permitted
    result['story']['not_before'] = (story.last_error.occurred_at + timedelta(minutes=30)).isoformat() if permitted else None
    parse_manifest(result)
    return result


@execution_locked
def reconcile_story(repository_root, *, short_slug, confirmation, meta_client,
                    git_runner, now_fn=lambda: datetime.now(timezone.utc)) -> dict:
    """Read-only Meta reconciliation; never calls create_story or publish.

    The registry or a durable published manifest supplies exact media ID/time.
    Container PUBLISHED alone cannot supply those fields and remains unresolved.
    Before-request S5B evidence is safe only after acquiring the execution lock;
    cross-host callers must share the existing Actions production concurrency group.
    """
    import copy, json
    root = Path(repository_root).resolve(strict=True)
    if (not isinstance(short_slug, str) or not short_slug
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in short_slug)
            or confirmation != f"RECONCILIAR STORY {short_slug}"):
        raise StoryPublicationError("authorization")
    path = root / 'publication-state/queue' / f'fisioclinex-{short_slug}' / 'manifest.json'
    if path.is_symlink() or any(p.is_symlink() for p in path.parents[:3]):
        raise StoryPublicationError('prepare')
    try:
        data = json.loads(path.read_bytes()); manifest = parse_manifest(data); story = manifest.story
        registry = root/'publication-state/publications.jsonl'
        feeds = read_registry(registry)
        if (story is None or manifest.status.value != 'published'
                or not any(r.publication_key == manifest.publication_key
                           and r.media_id == manifest.publication.media_id
                           and datetime.fromisoformat(r.published_at.replace('Z','+00:00')) == manifest.publication.published_at
                           for r in feeds)):
            raise StoryPublicationError('feed_writeback_incomplete')
        registered = next((r for r in read_story_registry(registry) if r.publication_key == story.publication_key), None)
        if registered:
            if (story.status == StoryState.PUBLISHED and story.media_id == registered.media_id
                    and story.published_at == datetime.fromisoformat(registered.published_at.replace('Z','+00:00'))):
                return {'status':'published','evidence':'registry','publication_key':story.publication_key}
            if not story.container_id or (story.media_id and story.media_id != registered.media_id):
                raise StoryPublicationError('registry_evidence_incomplete_or_conflicting')
            result = copy.deepcopy(data)
            result['story'].update(status='published', media_id=registered.media_id,
                published_at=registered.published_at, last_error=None, retry_allowed=None,
                attempt_stage='publish_confirmed')
            parse_manifest(result)
            _persist(root,path,result,git_runner=git_runner,registry=True)
            return {'status':'published','evidence':'registry','publication_key':story.publication_key}
        if story.status == StoryState.PUBLISHED:
            _persist(root,path,data,git_runner=git_runner,registry=True)
            return {'status':'published','evidence':'durable_manifest','publication_key':story.publication_key}
        if story.status not in {StoryState.PUBLISHING, StoryState.AMBIGUOUS}:
            raise StoryPublicationError('reconciliation_not_required')
        now = now_fn()
        if story.status == StoryState.PUBLISHING and story.attempt_stage in {'started','container_created'} and not story.media_id:
            result = change_story_state(data, StoryState.FAILED, now=now,
                       phase='reconcile_before_publish', reconciliation='confirmed_not_published')
            result = plan_retry(result)
            _persist(root,path,result,git_runner=git_runner)
            return {'status':'failed','retry_allowed':result['story']['retry_allowed'],
                    'evidence':'durable_before_request','publication_key':story.publication_key}
        if story.status == StoryState.PUBLISHING:
            data = change_story_state(data, StoryState.AMBIGUOUS, now=now,phase='reconcile_unknown_outcome')
            _persist(root,path,data,git_runner=git_runner)
        status = None
        if story.container_id:
            try:
                status = meta_client.container_status(story.container_id)
            except Exception:
                pass  # Sanitized inconclusive result; never convert query failures to non-publication.
        # EXPIRED explicitly means the container was not published before expiry.
        # FINISHED/ERROR/PUBLISHED/IN_PROGRESS alone do not recover an exact media ID/time.
        if status == 'EXPIRED' and not story.media_id:
            result = change_story_state(data,StoryState.FAILED,now=now_fn(),
                       phase='reconcile_container_expired',reconciliation='confirmed_not_published')
            result = plan_retry(result)
            _persist(root,path,result,git_runner=git_runner)
            return {'status':'failed','retry_allowed':result['story']['retry_allowed'],
                    'evidence':'container_expired','publication_key':story.publication_key}
        return {'status':'ambiguous','evidence':'inconclusive','publication_key':story.publication_key}
    except StoryPublicationError:
        raise
    except Exception:
        raise StoryPublicationError('reconciliation_writeback_or_evidence') from None
