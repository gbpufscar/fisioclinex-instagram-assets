"""Local append-only JSONL publication registry."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .fingerprint import build_publication_key, build_story_publication_key
from .result import ResultCode, ScheduledResult

_FIELDS = frozenset({"publication_key", "slug", "media_id", "published_at"})
_WORKFLOW_FIELDS = frozenset(
    {
        "schema_version",
        "publication_key",
        "slug",
        "short_slug",
        "media_id",
        "published_at",
        "asset_commit",
        "package_sha256",
        "slides_count",
        "publication_run_id",
        "workflow_run_id",
        "mode",
    }
)
_STORY_WORKFLOW_FIELDS = _WORKFLOW_FIELDS | frozenset(
    {"story_media_id", "story_published_at"}
)
_SLOT_HISTORY_FIELDS = _STORY_WORKFLOW_FIELDS | frozenset(
    {"slot_id", "planned_at", "slot_type", "assigned_slug", "queued_at", "slot_status"}
)


class RegistryError(ValueError):
    """A sanitized registry validation or duplication failure."""


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    publication_key: str
    slug: str
    media_id: str
    published_at: str

    def validate(self) -> None:
        if not isinstance(self.media_id, str) or not self.media_id:
            raise RegistryError("media_id is invalid")
        if not isinstance(self.published_at, str):
            raise RegistryError("published_at is invalid")
        try:
            timestamp = datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RegistryError("published_at is invalid") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise RegistryError("published_at must include a timezone")

        if not isinstance(self.publication_key, str):
            raise RegistryError("publication_key is invalid")
        separator = self.publication_key.rfind(":")
        if separator <= 0:
            raise RegistryError("publication_key is invalid")
        digest = self.publication_key[separator + 1 :]
        try:
            expected = build_publication_key(self.slug, digest)
        except ValueError as exc:
            raise RegistryError("publication identity is invalid") from exc
        if self.publication_key != expected:
            raise RegistryError("publication_key does not match slug")



@dataclass(frozen=True, slots=True)
class StoryPublicationRecord:
    schema_version: int
    surface: str
    publication_key: str
    feed_publication_key: str
    slug: str
    media_id: str
    published_at: str

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2 or self.surface != "story":
            raise RegistryError("story registry version or surface is invalid")
        PublicationRecord(self.feed_publication_key, self.slug, self.media_id, self.published_at).validate()
        digest = self.feed_publication_key.rsplit(":", 1)[-1]
        if self.publication_key != build_story_publication_key(self.slug, digest):
            raise RegistryError("story registry identity differs from parent feed")


_STORY_ACTION_FIELDS = frozenset(StoryPublicationRecord.__dataclass_fields__)

def _read_records(path: str | Path) -> tuple[PublicationRecord | StoryPublicationRecord, ...]:
    registry_path = Path(path)
    if not registry_path.exists():
        return ()
    if registry_path.is_symlink() or not registry_path.is_file():
        raise RegistryError("registry path is invalid")

    records: list[PublicationRecord | StoryPublicationRecord] = []
    keys: set[str] = set()
    media_ids: set[str] = set()
    with registry_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise RegistryError(f"registry line {line_number} is empty")
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RegistryError(f"registry line {line_number} is invalid JSON") from exc
            if not isinstance(data, dict) or data.keys() not in (
                _FIELDS,
                _WORKFLOW_FIELDS,
                _STORY_WORKFLOW_FIELDS,
                _SLOT_HISTORY_FIELDS,
                _STORY_ACTION_FIELDS,
            ):
                raise RegistryError(f"registry line {line_number} has invalid fields")
            if data.keys() in (_WORKFLOW_FIELDS, _STORY_WORKFLOW_FIELDS, _SLOT_HISTORY_FIELDS):
                data = {
                    "publication_key": data["publication_key"],
                    "slug": data["slug"],
                    "media_id": data["media_id"],
                    "published_at": data["published_at"],
                }
            try:
                record = StoryPublicationRecord(**data) if data.keys() == _STORY_ACTION_FIELDS else PublicationRecord(**data)
            except TypeError as exc:
                raise RegistryError(f"registry line {line_number} is invalid") from exc
            record.validate()
            if record.publication_key in keys:
                raise RegistryError("duplicate publication_key in registry")
            if record.media_id in media_ids:
                raise RegistryError("duplicate media_id in registry")
            keys.add(record.publication_key)
            media_ids.add(record.media_id)
            records.append(record)
    feeds = {r.publication_key: r for r in records if isinstance(r, PublicationRecord)}
    for record in records:
        if isinstance(record, StoryPublicationRecord):
            parent = feeds.get(record.feed_publication_key)
            if parent is None or parent.slug != record.slug:
                raise RegistryError("story registry requires confirmed parent feed")
            if datetime.fromisoformat(record.published_at.replace("Z", "+00:00")) < datetime.fromisoformat(parent.published_at.replace("Z", "+00:00")):
                raise RegistryError("story registry publication precedes feed")
    return tuple(records)


def read_registry(path: str | Path) -> tuple[PublicationRecord, ...]:
    """Feed-only view: Story actions never count towards feed spacing or slots."""
    return tuple(r for r in _read_records(path) if isinstance(r, PublicationRecord))


def read_story_registry(path: str | Path) -> tuple[StoryPublicationRecord, ...]:
    return tuple(r for r in _read_records(path) if isinstance(r, StoryPublicationRecord))


def append_record(path: str | Path, record: PublicationRecord) -> ScheduledResult:
    if not isinstance(record, PublicationRecord):
        raise RegistryError("feed append requires a feed record")
    record.validate()
    registry_path = Path(path)
    existing = _read_records(registry_path)
    if any(item.publication_key == record.publication_key for item in existing):
        return ScheduledResult.failure(
            ResultCode.DUPLICATE,
            "publication_key already registered",
            slug=record.slug,
            publication_key=record.publication_key,
        )
    if any(item.media_id == record.media_id for item in existing):
        return ScheduledResult.failure(
            ResultCode.DUPLICATE,
            "media_id already registered",
            slug=record.slug,
            publication_key=record.publication_key,
        )
    if registry_path.exists() and (registry_path.is_symlink() or not registry_path.is_file()):
        raise RegistryError("registry path is invalid")

    payload = json.dumps(asdict(record), sort_keys=True, separators=(",", ":"))
    with registry_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return ScheduledResult.success(
        ResultCode.REGISTRY_APPENDED,
        "publication appended",
        slug=record.slug,
        publication_key=record.publication_key,
    )


def feed_selection_history(records):
    """Shared validated feed history for preparation and final precheck."""
    return {"registered_publication_keys":tuple(record.publication_key for record in records),
            "published_at":tuple(datetime.fromisoformat(record.published_at.replace("Z", "+00:00"))
                                 for record in records)}
