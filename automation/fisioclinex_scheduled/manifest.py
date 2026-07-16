"""Strict manifest.json parsing with no external validation dependency."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Mapping

from .fingerprint import build_publication_key
from .states import HUMAN_REVIEW_STATES, QueueState

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SLUG_RE = re.compile(r"^fisioclinex-[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHORT_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "slug",
        "short_slug",
        "status",
        "queued_at",
        "not_before",
        "priority",
        "slides_count",
        "caption_file",
        "package_sha256",
        "publication_key",
        "attempts",
        "publication",
        "failure",
    }
)
_PUBLICATION_FIELDS = frozenset(
    {"media_id", "published_at", "workflow_run_id", "asset_commit"}
)
_FAILURE_FIELDS = frozenset({"phase", "occurred_at", "requires_human_review"})


class ManifestValidationError(ValueError):
    """A sanitized manifest validation failure."""


@dataclass(frozen=True, slots=True)
class Publication:
    media_id: str | None
    published_at: datetime | None
    workflow_run_id: str | None
    asset_commit: str | None


@dataclass(frozen=True, slots=True)
class Failure:
    phase: str | None
    occurred_at: datetime | None
    requires_human_review: bool


@dataclass(frozen=True, slots=True)
class Manifest:
    schema_version: int
    slug: str
    short_slug: str
    status: QueueState
    queued_at: datetime
    not_before: datetime | None
    priority: int
    slides_count: int
    caption_file: str
    package_sha256: str
    publication_key: str
    attempts: int
    publication: Publication
    failure: Failure


def _require_exact_fields(data: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    missing = expected - data.keys()
    unknown = data.keys() - expected
    if missing:
        raise ManifestValidationError(f"{label} missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ManifestValidationError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")


def _require_type(value: Any, expected: type, field: str) -> None:
    if expected is int:
        valid = isinstance(value, int) and not isinstance(value, bool)
    else:
        valid = isinstance(value, expected)
    if not valid:
        raise ManifestValidationError(f"{field} has invalid type")


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    _require_type(value, str, field)
    if not value:
        raise ManifestValidationError(f"{field} cannot be empty")
    return value


def _parse_timestamp(value: Any, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    _require_type(value, str, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestValidationError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestValidationError(f"{field} must include a timezone")
    return parsed


def _parse_mapping(data: Mapping[str, Any]) -> Manifest:
    _require_exact_fields(data, _TOP_LEVEL_FIELDS, "manifest")

    _require_type(data["schema_version"], int, "schema_version")
    if data["schema_version"] != 1:
        raise ManifestValidationError("unsupported schema_version")

    _require_type(data["slug"], str, "slug")
    if not _SLUG_RE.fullmatch(data["slug"]):
        raise ManifestValidationError("slug must use the fisioclinex- prefix")

    _require_type(data["short_slug"], str, "short_slug")
    if not _SHORT_SLUG_RE.fullmatch(data["short_slug"]) or data["short_slug"].startswith(
        "fisioclinex-"
    ):
        raise ManifestValidationError("short_slug is invalid")
    if data["slug"] != f"fisioclinex-{data['short_slug']}":
        raise ManifestValidationError("slug and short_slug do not match")

    try:
        status = QueueState(data["status"])
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError("status is unknown") from exc

    queued_at = _parse_timestamp(data["queued_at"], "queued_at")
    not_before = _parse_timestamp(data["not_before"], "not_before", optional=True)

    _require_type(data["priority"], int, "priority")
    _require_type(data["slides_count"], int, "slides_count")
    if not 1 <= data["slides_count"] <= 10:
        raise ManifestValidationError("slides_count must be between 1 and 10")

    _require_type(data["caption_file"], str, "caption_file")
    caption_path = PurePosixPath(data["caption_file"])
    if (
        data["caption_file"] != caption_path.as_posix()
        or caption_path.is_absolute()
        or ".." in caption_path.parts
        or data["caption_file"] != "legenda.txt"
    ):
        raise ManifestValidationError("caption_file is invalid")

    _require_type(data["package_sha256"], str, "package_sha256")
    if not _SHA256_RE.fullmatch(data["package_sha256"]):
        raise ManifestValidationError("package_sha256 must be lowercase SHA-256")

    _require_type(data["publication_key"], str, "publication_key")
    expected_key = build_publication_key(data["slug"], data["package_sha256"])
    if data["publication_key"] != expected_key:
        raise ManifestValidationError("publication_key does not match slug and package_sha256")

    _require_type(data["attempts"], int, "attempts")
    if data["attempts"] < 0:
        raise ManifestValidationError("attempts cannot be negative")

    _require_type(data["publication"], dict, "publication")
    _require_exact_fields(data["publication"], _PUBLICATION_FIELDS, "publication")
    publication = Publication(
        media_id=_optional_string(data["publication"]["media_id"], "publication.media_id"),
        published_at=_parse_timestamp(
            data["publication"]["published_at"], "publication.published_at", optional=True
        ),
        workflow_run_id=_optional_string(
            data["publication"]["workflow_run_id"], "publication.workflow_run_id"
        ),
        asset_commit=_optional_string(
            data["publication"]["asset_commit"], "publication.asset_commit"
        ),
    )

    _require_type(data["failure"], dict, "failure")
    _require_exact_fields(data["failure"], _FAILURE_FIELDS, "failure")
    _require_type(
        data["failure"]["requires_human_review"], bool, "failure.requires_human_review"
    )
    failure = Failure(
        phase=_optional_string(data["failure"]["phase"], "failure.phase"),
        occurred_at=_parse_timestamp(
            data["failure"]["occurred_at"], "failure.occurred_at", optional=True
        ),
        requires_human_review=data["failure"]["requires_human_review"],
    )

    if status is QueueState.PUBLISHED:
        if publication.media_id is None or publication.published_at is None:
            raise ManifestValidationError("published status requires media_id and published_at")
    elif publication.published_at is not None:
        raise ManifestValidationError("published_at is only valid for published status")

    if publication.media_id is not None and status not in {
        QueueState.PUBLISHING,
        QueueState.PUBLISHED,
        QueueState.FAILED_AFTER_META,
        QueueState.NEEDS_REVIEW,
    }:
        raise ManifestValidationError("media_id is inconsistent with status")

    if status in HUMAN_REVIEW_STATES and not failure.requires_human_review:
        raise ManifestValidationError("status requires human review")
    if failure.requires_human_review and status not in HUMAN_REVIEW_STATES:
        raise ManifestValidationError("human review flag is inconsistent with status")

    return Manifest(
        schema_version=data["schema_version"],
        slug=data["slug"],
        short_slug=data["short_slug"],
        status=status,
        queued_at=queued_at,
        not_before=not_before,
        priority=data["priority"],
        slides_count=data["slides_count"],
        caption_file=data["caption_file"],
        package_sha256=data["package_sha256"],
        publication_key=data["publication_key"],
        attempts=data["attempts"],
        publication=publication,
        failure=failure,
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError("manifest contains a duplicate field")
        result[key] = value
    return result


def parse_manifest(source: str | bytes | bytearray | Mapping[str, Any]) -> Manifest:
    """Parse strict JSON text or an already decoded mapping."""
    if isinstance(source, Mapping):
        return _parse_mapping(source)
    if not isinstance(source, (str, bytes, bytearray)):
        raise ManifestValidationError("manifest input type is invalid")
    try:
        data = json.loads(source, object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestValidationError("manifest is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ManifestValidationError("manifest root must be an object")
    return _parse_mapping(data)
