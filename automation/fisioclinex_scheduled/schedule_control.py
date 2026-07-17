"""Strict, read-only activation control for scheduled publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class ScheduleControlError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScheduleControl:
    schema_version: int
    scheduled_publication_enabled: bool


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScheduleControlError("schedule control contains duplicate fields")
        result[key] = value
    return result


def load_schedule_control(path: str | Path) -> ScheduleControl:
    control = Path(path)
    if control.is_symlink() or not control.is_file():
        raise ScheduleControlError("schedule control is unavailable")
    try:
        data = json.loads(
            control.read_text(encoding="utf-8"), object_pairs_hook=_strict_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScheduleControlError("schedule control is invalid JSON") from exc
    if not isinstance(data, dict) or data.keys() != {
        "schema_version",
        "scheduled_publication_enabled",
    }:
        raise ScheduleControlError("schedule control fields are invalid")
    if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
        raise ScheduleControlError("schedule control schema is invalid")
    enabled = data["scheduled_publication_enabled"]
    if not isinstance(enabled, bool):
        raise ScheduleControlError("scheduled publication flag must be boolean")
    return ScheduleControl(1, enabled)
