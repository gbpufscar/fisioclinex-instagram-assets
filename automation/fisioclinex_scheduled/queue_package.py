"""Validation and manifest construction for an approved queue package."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .fingerprint import build_story_publication_key, build_publication_key, fingerprint_package
from .manifest import Manifest, parse_manifest
from .story_state import StoryPublication, StoryState, story_json
from content_policy import (
    ACTIVE_ARTIFACT_STATUS,
    CONTENT_POLICY_VERSION,
    ContentPolicyError,
    reject_local_validation_package,
)
from publication_package import PublicationPackageError, validate_publication_package


class QueuePackageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QueuePackage:
    root: Path
    slug: str
    short_slug: str
    slides: tuple[Path, ...]
    story_path: Path
    caption_path: Path
    package_sha256: str
    publication_key: str
    visual_schema_version: str = "2.0"


def validate_queue_package(folder: str | Path, *, new_publication: bool = False) -> QueuePackage:
    raw = Path(folder).expanduser()
    if raw.is_symlink() or not raw.is_dir():
        raise QueuePackageError("package directory is invalid")
    root = raw.resolve(strict=True)
    try:
        reject_local_validation_package(root)
        package = validate_publication_package(root, new_publication=new_publication)
    except (ContentPolicyError, PublicationPackageError) as exc:
        raise QueuePackageError(str(exc)) from exc
    # Keep the operational publication fingerprint contract stable; visual files are
    # independently bound to every PNG by the hashes validated above.
    relative = ["legenda.txt", *(path.name for path in package.slides), package.story.name]
    digest = fingerprint_package(root, relative)
    return QueuePackage(
        root=root,
        slug=package.slug,
        short_slug=package.slug.removeprefix("fisioclinex-"),
        slides=package.slides,
        story_path=package.story,
        caption_path=package.caption,
        package_sha256=digest,
        publication_key=build_publication_key(package.slug, digest),
        visual_schema_version=package.manifest["schema_version"],
    )


def build_manifest(
    package: QueuePackage,
    *,
    queued_at: datetime,
    priority: int = 100,
    not_before: datetime | None = None,
    slot=None,
    include_story_state: bool | None = None,
) -> Manifest:
    if queued_at.tzinfo is None or queued_at.utcoffset() is None:
        raise QueuePackageError("queued_at must include a timezone")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise QueuePackageError("priority must be an integer")
    if not_before is not None and (
        not_before.tzinfo is None or not_before.utcoffset() is None
    ):
        raise QueuePackageError("not_before must include a timezone")
    data = {
        "schema_version": 1,
        "content_policy_version": CONTENT_POLICY_VERSION,
        "artifact_status": ACTIVE_ARTIFACT_STATUS,
        "slug": package.slug,
        "short_slug": package.short_slug,
        "status": "queued",
        "queued_at": queued_at.isoformat(),
        "not_before": not_before.isoformat() if not_before else None,
        "priority": priority,
        "slides_count": len(package.slides),
        "caption_file": "legenda.txt",
        "package_sha256": package.package_sha256,
        "publication_key": package.publication_key,
        "attempts": 0,
        "publication": {
            "media_id": None,
            "published_at": None,
            "workflow_run_id": None,
            "asset_commit": None,
        },
        "failure": {
            "phase": None,
            "occurred_at": None,
            "requires_human_review": False,
        },
    }
    if slot is not None:
        if slot.status != "assigned" or slot.assigned_slug != package.slug:
            raise QueuePackageError("slot must be assigned to this package")
        data.update(
            {
                "slot_id": slot.slot_id,
                "planned_at": slot.planned_at,
                "slot_type": slot.slot_type,
                "explicit_override": slot.explicit_override,
                "override_reason": slot.override_reason,
            }
        )
    if include_story_state is None:
        include_story_state = package.visual_schema_version == "2.1" and slot is not None
    if package.visual_schema_version == "2.1" and slot is not None and not include_story_state:
        raise QueuePackageError("new planned packages require their Story child")
    if include_story_state:
        data["story"] = story_json(StoryPublication(
            StoryState.PENDING, build_story_publication_key(package.slug, package.package_sha256)))
        if slot is not None:
            from .schedule_integrity import story_planned_at
            data["story"]["not_before"] = story_planned_at(slot.planned_at).isoformat()
            data["story_plan_version"] = 1
    return parse_manifest(data)


def manifest_json(manifest: Manifest) -> bytes:
    data = {
        "schema_version": manifest.schema_version,
        "content_policy_version": manifest.content_policy_version,
        "artifact_status": manifest.artifact_status,
        "slug": manifest.slug,
        "short_slug": manifest.short_slug,
        "status": manifest.status.value,
        "queued_at": manifest.queued_at.isoformat(),
        "not_before": manifest.not_before.isoformat() if manifest.not_before else None,
        "priority": manifest.priority,
        "slides_count": manifest.slides_count,
        "caption_file": manifest.caption_file,
        "package_sha256": manifest.package_sha256,
        "publication_key": manifest.publication_key,
        "attempts": manifest.attempts,
        "publication": {
            "media_id": manifest.publication.media_id,
            "published_at": manifest.publication.published_at.isoformat() if manifest.publication.published_at else None,
            "workflow_run_id": manifest.publication.workflow_run_id,
            "asset_commit": manifest.publication.asset_commit,
        },
        "failure": {
            "phase": manifest.failure.phase,
            "occurred_at": manifest.failure.occurred_at.isoformat() if manifest.failure.occurred_at else None,
            "requires_human_review": manifest.failure.requires_human_review,
        },
    }
    if manifest.slot_id is not None:
        data.update(
            {
                "slot_id": manifest.slot_id,
                "planned_at": manifest.planned_at.isoformat(),
                "slot_type": manifest.slot_type,
                "explicit_override": manifest.explicit_override,
                "override_reason": manifest.override_reason,
            }
        )
    if manifest.story_plan_version is not None:
        data["story_plan_version"] = manifest.story_plan_version
    if manifest.story is not None:
        data["story"] = story_json(manifest.story)
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
