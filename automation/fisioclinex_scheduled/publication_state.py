"""Strict manifest state mutations for manual workflow publication."""

from __future__ import annotations

import copy
from datetime import datetime


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
    return result


def mark_failed(
    data: dict,
    *,
    phase: str,
    failed_at: datetime,
    children: tuple[str, ...],
    carousel_id: str | None,
    media_id: str | None,
) -> dict:
    result = copy.deepcopy(data)
    result["status"] = "failed_after_meta"
    result["child_container_ids"] = list(children)
    result["carousel_container_id"] = carousel_id
    result["publication"]["media_id"] = media_id
    result["failure"] = {
        "phase": phase,
        "occurred_at": failed_at.isoformat(),
        "requires_human_review": True,
    }
    return result


def mark_published(data: dict, *, media_id: str, published_at: datetime) -> dict:
    result = copy.deepcopy(data)
    result["status"] = "published"
    result["publication"]["media_id"] = media_id
    result["publication"]["published_at"] = published_at.isoformat()
    result["failure"] = {
        "phase": None,
        "occurred_at": None,
        "requires_human_review": False,
    }
    return result
