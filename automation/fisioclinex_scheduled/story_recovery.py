"""Explicit, idempotent recovery for a missing Story after a published feed."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .publication_writeback import persist, write_manifest
from .queue_pages import official_slide_url


class StoryRecoveryError(RuntimeError):
    def __init__(self, phase: str):
        super().__init__(f"Story recovery interrupted in phase {phase}")
        self.phase = phase


def recover_story(
    repository_root: str | Path,
    *,
    short_slug: str,
    confirmation: str,
    meta_client,
    git_runner,
    now_fn=lambda: datetime.now(timezone.utc),
) -> dict:
    if (
        not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", short_slug or "")
        or short_slug.startswith("fisioclinex-")
        or confirmation != f"PUBLICAR STORY {short_slug}"
    ):
        raise StoryRecoveryError("authorization")
    root = Path(repository_root).resolve(strict=True)
    slug = f"fisioclinex-{short_slug}"
    manifest_path = root / "publication-state" / "queue" / slug / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise StoryRecoveryError("prepare")
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    publication = original.get("publication", {})
    if original.get("status") != "published" or not publication.get("media_id"):
        raise StoryRecoveryError("feed_not_published")
    if original.get("story_media_id"):
        raise StoryRecoveryError("story_already_published")
    story_path = root / "posts" / slug / f"{slug}-story.png"
    if story_path.is_symlink() or not story_path.is_file():
        raise StoryRecoveryError("story_asset_missing")

    state = copy.deepcopy(original)
    container_id = None
    try:
        story_url = official_slide_url(slug, f"{slug}-story.png")
        container_id = meta_client.create_story(story_url)
        meta_client.wait_finished(container_id)
        story_media_id = meta_client.publish(container_id)
    except Exception as exc:
        state["story_container_id"] = container_id
        state["failure"] = {
            "phase": getattr(exc, "phase", "story"),
            "occurred_at": now_fn().isoformat(),
            "requires_human_review": True,
        }
        write_manifest(manifest_path, state)
        try:
            persist(
                root,
                paths=(manifest_path,),
                message=f"queue: registrar falha Story {slug}",
                git_runner=git_runner,
            )
        except Exception:
            pass
        raise StoryRecoveryError(getattr(exc, "phase", "story")) from None

    published_at = now_fn().isoformat()
    state["story_container_id"] = container_id
    state["story_media_id"] = story_media_id
    state["story_published_at"] = published_at
    state["failure"] = {
        "phase": None,
        "occurred_at": None,
        "requires_human_review": False,
    }
    write_manifest(manifest_path, state)
    persist(
        root,
        paths=(manifest_path,),
        message=f"queue: registrar Story {slug}",
        git_runner=git_runner,
    )
    return {
        "slug": slug,
        "status": "story_published",
        "feed_media_id": publication["media_id"],
        "story_media_id": story_media_id,
        "story_published_at": published_at,
    }
