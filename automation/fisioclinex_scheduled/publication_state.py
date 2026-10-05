"""Strict manifest state mutations for manual workflow publication."""

from __future__ import annotations

import copy
from datetime import datetime

from .manifest import parse_manifest
from .story_state import transition_story, story_json


class PublicationStateError(ValueError):
    pass


def authorize(short_slug: str, confirmation: str, selected_short_slug: str) -> None:
    if (
        not isinstance(short_slug, str)
        or short_slug != selected_short_slug
        or short_slug.startswith("fisioclinex-")
        or confirmation != f"PUBLICAR {short_slug}"
    ):
        raise PublicationStateError("manual publication authorization is invalid")


def begin_publishing(
    data: dict,
    *,
    run_id: str,
    workflow_run_id: str,
    started_at: datetime,
    asset_commit: str,
) -> dict:
    if "story" in data:
        parse_manifest(data)
    if not isinstance(workflow_run_id, str) or not workflow_run_id.isdigit():
        raise PublicationStateError("workflow run ID is invalid")
    if data.get("status") != "queued" or data["publication"].get("media_id") is not None:
        raise PublicationStateError("queue item is not publishable")
    result = copy.deepcopy(data)
    result["status"] = "publishing"
    result["attempts"] += 1
    result["publication_run_id"] = run_id
    result["started_at"] = started_at.isoformat()
    result["pushed"] = True
    result["verified"] = True
    result["publication"]["workflow_run_id"] = workflow_run_id
    result["publication"]["asset_commit"] = asset_commit
    result["failure"] = {
        "phase": None,
        "occurred_at": None,
        "requires_human_review": False,
    }
    result["child_container_ids"] = []
    result["carousel_container_id"] = None
    result["single_image_container_id"] = None
    if "story" not in result:
        result["story_container_id"] = None
        result["story_media_id"] = None
        result["story_published_at"] = None
    if "story" in result:
        parse_manifest(result)
    return result


def mark_failed(
    data: dict,
    *,
    phase: str,
    failed_at: datetime,
    children: tuple[str, ...],
    carousel_id: str | None,
    single_image_id: str | None,
    media_id: str | None,
    story_container_id: str | None = None,
    story_media_id: str | None = None,
) -> dict:
    if "story" in data and data["publication"].get("published_at") is not None:
        raise PublicationStateError("confirmed feed is immutable; use the Story state transition")
    result = copy.deepcopy(data)
    result["status"] = "failed_after_meta"
    result["child_container_ids"] = list(children)
    result["carousel_container_id"] = carousel_id
    result["single_image_container_id"] = single_image_id
    if result["publication"].get("published_at") is not None:
        if media_id not in {None, result["publication"].get("media_id")}:
            raise PublicationStateError("confirmed feed identity cannot be changed")
    else:
        result["publication"]["media_id"] = media_id
    if "story" in result:
        # A pre-feed failure leaves the unattempted child intact.
        result["failure"] = {"phase": phase, "occurred_at": failed_at.isoformat(), "requires_human_review": True}
        parse_manifest(result)
        return result
    result["story_container_id"] = story_container_id
    result["story_media_id"] = story_media_id
    result["failure"] = {
        "phase": phase,
        "occurred_at": failed_at.isoformat(),
        "requires_human_review": True,
    }
    return result


def mark_feed_published(data: dict, *, media_id: str, published_at: datetime) -> dict:
    result = copy.deepcopy(data)
    _preserve_feed(data, media_id, published_at)
    result["publication"]["media_id"] = media_id
    result["publication"]["published_at"] = published_at.isoformat()
    if "story" in result:
        result["status"] = "published"
        result["failure"] = {"phase": None, "occurred_at": None, "requires_human_review": False}
    parse_manifest(result)
    return result


def mark_published(
    data: dict, *, media_id: str, published_at: datetime,
    story_container_id: str, story_media_id: str, story_published_at: datetime,
) -> dict:
    if "story" in data:
        raise PublicationStateError("use separate feed and Story transitions")
    _preserve_feed(data, media_id, published_at)
    result = copy.deepcopy(data)
    result["status"] = "published"
    result["publication"]["media_id"] = media_id
    result["publication"]["published_at"] = published_at.isoformat()
    result["story_container_id"] = story_container_id
    result["story_media_id"] = story_media_id
    result["story_published_at"] = story_published_at.isoformat()
    result["failure"] = {
        "phase": None,
        "occurred_at": None,
        "requires_human_review": False,
    }
    return result


def _preserve_feed(data: dict, media_id: str, published_at: datetime) -> None:
    existing = data["publication"]
    if existing.get("published_at") is not None:
        known_time = datetime.fromisoformat(existing["published_at"].replace("Z", "+00:00"))
        if existing.get("media_id") != media_id or known_time != published_at:
            raise PublicationStateError("confirmed feed result cannot be changed")


def change_story_state(data: dict, target, *, now: datetime, **evidence) -> dict:
    """Pure child mutation. Feed identity, success and schedule are never changed."""
    manifest = parse_manifest(data)
    if manifest.story is None:
        raise PublicationStateError("legacy item has no independent Story state")
    child = transition_story(
        manifest.story, target, now=now, slug=manifest.slug,
        package_sha256=manifest.package_sha256,
        feed_published_at=manifest.publication.published_at, **evidence,
    )
    result = copy.deepcopy(data)
    result["story"] = story_json(child)
    parse_manifest(result)
    return result
