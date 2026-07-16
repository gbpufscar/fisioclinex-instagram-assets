"""Local append-only JSONL publication registry."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .fingerprint import build_publication_key
from .result import ResultCode, ScheduledResult

_FIELDS = frozenset({"publication_key", "slug", "media_id", "published_at"})


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


def read_registry(path: str | Path) -> tuple[PublicationRecord, ...]:
    registry_path = Path(path)
    if not registry_path.exists():
        return ()
    if registry_path.is_symlink() or not registry_path.is_file():
        raise RegistryError("registry path is invalid")

    records: list[PublicationRecord] = []
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
            if not isinstance(data, dict) or data.keys() != _FIELDS:
                raise RegistryError(f"registry line {line_number} has invalid fields")
            try:
                record = PublicationRecord(**data)
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
    return tuple(records)


def append_record(path: str | Path, record: PublicationRecord) -> ScheduledResult:
    record.validate()
    registry_path = Path(path)
    existing = read_registry(registry_path)
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
