"""Sanitized result types shared by the local queue components."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResultCode(StrEnum):
    QUEUE_DISABLED = "queue_disabled"
    QUEUE_EMPTY = "queue_empty"
    ITEM_SELECTED = "item_selected"
    INVALID_MANIFEST = "invalid_manifest"
    DUPLICATE = "duplicate"
    INVALID_TRANSITION = "invalid_transition"
    REGISTRY_APPENDED = "registry_appended"


@dataclass(frozen=True, slots=True)
class ScheduledResult:
    code: ResultCode
    ok: bool
    reason: str
    slug: str | None = None
    short_slug: str | None = None
    status: str | None = None
    publication_key: str | None = None
    candidate_count: int = 0
    eligible_count: int = 0

    @classmethod
    def success(cls, code: ResultCode, reason: str, **details: object) -> "ScheduledResult":
        return cls(code=code, ok=True, reason=reason, **details)

    @classmethod
    def failure(cls, code: ResultCode, reason: str, **details: object) -> "ScheduledResult":
        return cls(code=code, ok=False, reason=reason, **details)
