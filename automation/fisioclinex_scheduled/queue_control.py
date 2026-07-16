"""Strict read-only queue-control parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIELDS = frozenset({"schema_version", "queue_enabled", "max_posts_per_run"})


class QueueControlError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QueueControl:
    schema_version: int
    queue_enabled: bool
    max_posts_per_run: int


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QueueControlError("queue control contains a duplicate field")
        result[key] = value
    return result


def load_queue_control(path: str | Path) -> QueueControl:
    control_path = Path(path)
    if control_path.is_symlink() or not control_path.is_file():
        raise QueueControlError("queue control is unavailable")
    try:
        data = json.loads(
            control_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QueueControlError("queue control is invalid JSON") from exc
    if not isinstance(data, dict) or data.keys() != _FIELDS:
        raise QueueControlError("queue control fields are invalid")
    if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
        raise QueueControlError("queue control schema_version is invalid")
    if not isinstance(data["queue_enabled"], bool):
        raise QueueControlError("queue_enabled must be boolean")
    if data["max_posts_per_run"] != 1 or isinstance(data["max_posts_per_run"], bool):
        raise QueueControlError("max_posts_per_run must be exactly 1")
    return QueueControl(1, data["queue_enabled"], 1)
