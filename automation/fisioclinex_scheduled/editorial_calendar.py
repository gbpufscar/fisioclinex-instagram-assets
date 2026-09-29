"""Persistent editorial slots kept deliberately separate from the publication queue."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

SLOT_TYPES = frozenset({"scientific", "other"})
SLOT_STATES = frozenset({"available", "assigned", "queued", "published", "skipped"})
_SLUG_RE = re.compile(r"^fisioclinex-[a-z0-9]+(?:-[a-z0-9]+)*$")
_SLOT_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}-(?:scientific|other)$")


class EditorialCalendarError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CalendarPolicy:
    schema_version: int
    timezone: str
    hour: int
    minute: int
    generation_weeks: int
    weekdays: dict[int, str]


@dataclass(frozen=True, slots=True)
class EditorialSlot:
    slot_id: str
    planned_at: str
    timezone: str
    slot_type: str
    status: str = "available"
    assigned_slug: str | None = None
    queued_at: str | None = None
    actual_published_at: str | None = None
    explicit_override: bool = False
    override_reason: str | None = None

    @property
    def planned_datetime(self) -> datetime:
        return _aware_timestamp(self.planned_at, "planned_at")


def _aware_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise EditorialCalendarError(f"{field} inválido")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EditorialCalendarError(f"{field} inválido") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise EditorialCalendarError(f"{field} sem timezone")
    return result


def _optional_timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _aware_timestamp(value, field).isoformat()


def load_policy(path: str | Path) -> CalendarPolicy:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise EditorialCalendarError("política de calendário indisponível")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EditorialCalendarError("política de calendário inválida") from exc
    if not isinstance(data, dict) or data.keys() != {
        "schema_version", "timezone", "hour", "minute", "generation_weeks", "weekdays"
    }:
        raise EditorialCalendarError("campos da política inválidos")
    if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
        raise EditorialCalendarError("versão da política inválida")
    try:
        ZoneInfo(data["timezone"])
    except (KeyError, TypeError) as exc:
        raise EditorialCalendarError("timezone inválido") from exc
    if not isinstance(data["hour"], int) or not 0 <= data["hour"] <= 23:
        raise EditorialCalendarError("hora inválida")
    if not isinstance(data["minute"], int) or not 0 <= data["minute"] <= 59:
        raise EditorialCalendarError("minuto inválido")
    if not isinstance(data["generation_weeks"], int) or not 1 <= data["generation_weeks"] <= 8:
        raise EditorialCalendarError("janela de geração inválida")
    weekdays: dict[int, str] = {}
    if not isinstance(data["weekdays"], dict):
        raise EditorialCalendarError("weekdays inválido")
    for raw_day, slot_type in data["weekdays"].items():
        try:
            day = int(raw_day)
        except (TypeError, ValueError) as exc:
            raise EditorialCalendarError("weekday inválido") from exc
        if day not in range(7) or slot_type not in SLOT_TYPES:
            raise EditorialCalendarError("regra de weekday inválida")
        weekdays[day] = slot_type
    if weekdays != {0: "scientific", 1: "other", 2: "scientific", 3: "other", 4: "scientific"}:
        raise EditorialCalendarError("política semanal não canônica")
    return CalendarPolicy(1, data["timezone"], data["hour"], data["minute"], data["generation_weeks"], weekdays)


def make_slot(day: date, policy: CalendarPolicy) -> EditorialSlot | None:
    slot_type = policy.weekdays.get(day.weekday())
    if slot_type is None:
        return None
    planned = datetime.combine(day, time(policy.hour, policy.minute), ZoneInfo(policy.timezone))
    slot_id = f"{planned.strftime('%Y-%m-%dT%H:%M')}-{slot_type}"
    return EditorialSlot(slot_id, planned.isoformat(), policy.timezone, slot_type)


def generate_slots(start: date, *, policy: CalendarPolicy, weeks: int | None = None) -> tuple[EditorialSlot, ...]:
    count = policy.generation_weeks if weeks is None else weeks
    if not isinstance(count, int) or not 1 <= count <= 8:
        raise EditorialCalendarError("janela de geração deve ficar entre 1 e 8 semanas")
    result = []
    for offset in range(count * 7):
        slot = make_slot(start + timedelta(days=offset), policy)
        if slot is not None:
            result.append(slot)
    return tuple(result)


def _validate_slot(slot: EditorialSlot, policy: CalendarPolicy) -> EditorialSlot:
    if not _SLOT_ID_RE.fullmatch(slot.slot_id):
        raise EditorialCalendarError("slot_id inválido")
    planned = slot.planned_datetime
    if slot.timezone != policy.timezone or str(ZoneInfo(slot.timezone)) != policy.timezone:
        raise EditorialCalendarError("timezone do slot inválido")
    local = planned.astimezone(ZoneInfo(policy.timezone))
    if local.hour != policy.hour or local.minute != policy.minute:
        raise EditorialCalendarError("horário do slot diverge da política")
    if policy.weekdays.get(local.weekday()) != slot.slot_type or slot.slot_type not in SLOT_TYPES:
        raise EditorialCalendarError("tipo do slot diverge da política")
    if slot.slot_id != f"{local.strftime('%Y-%m-%dT%H:%M')}-{slot.slot_type}":
        raise EditorialCalendarError("slot_id diverge do horário planejado")
    if slot.status not in SLOT_STATES:
        raise EditorialCalendarError("status de slot inválido")
    if slot.assigned_slug is not None and not _SLUG_RE.fullmatch(slot.assigned_slug):
        raise EditorialCalendarError("assigned_slug inválido")
    if slot.status == "available" and slot.assigned_slug is not None:
        raise EditorialCalendarError("slot disponível não pode estar associado")
    if slot.status in {"assigned", "queued", "published"} and slot.assigned_slug is None:
        raise EditorialCalendarError("status exige publicação associada")
    if slot.status == "published" and slot.actual_published_at is None:
        raise EditorialCalendarError("slot publicado exige horário real")
    if slot.actual_published_at is not None and slot.status != "published":
        raise EditorialCalendarError("horário real exige status published")
    if slot.queued_at is not None and slot.status not in {"queued", "published"}:
        raise EditorialCalendarError("queued_at incompatível com status")
    _optional_timestamp(slot.queued_at, "queued_at")
    _optional_timestamp(slot.actual_published_at, "actual_published_at")
    if slot.explicit_override:
        if not isinstance(slot.override_reason, str) or not slot.override_reason.strip():
            raise EditorialCalendarError("override explícito exige justificativa")
    elif slot.override_reason is not None:
        raise EditorialCalendarError("justificativa exige override explícito")
    return slot


def load_calendar(path: str | Path, *, policy: CalendarPolicy) -> tuple[EditorialSlot, ...]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise EditorialCalendarError("calendário indisponível")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EditorialCalendarError("calendário inválido") from exc
    if not isinstance(data, dict) or data.keys() != {"schema_version", "policy_version", "slots"}:
        raise EditorialCalendarError("campos do calendário inválidos")
    if data["schema_version"] != 1 or data["policy_version"] != policy.schema_version or not isinstance(data["slots"], list):
        raise EditorialCalendarError("versão do calendário inválida")
    slots = tuple(_validate_slot(EditorialSlot(**raw), policy) for raw in data["slots"] if isinstance(raw, dict))
    if len(slots) != len(data["slots"]):
        raise EditorialCalendarError("slot inválido")
    if len({slot.slot_id for slot in slots}) != len(slots):
        raise EditorialCalendarError("slot_id duplicado")
    assigned = [slot.assigned_slug for slot in slots if slot.assigned_slug and slot.status in {"assigned", "queued"}]
    if len(set(assigned)) != len(assigned):
        raise EditorialCalendarError("publicação ocupa mais de um slot ativo")
    return tuple(sorted(slots, key=lambda item: item.planned_datetime))


def save_calendar(path: str | Path, slots: Iterable[EditorialSlot], *, policy: CalendarPolicy) -> None:
    target = Path(path)
    if target.is_symlink():
        raise EditorialCalendarError("destino de calendário inseguro")
    checked = tuple(_validate_slot(slot, policy) for slot in slots)
    payload = {"schema_version": 1, "policy_version": policy.schema_version, "slots": [asdict(slot) for slot in sorted(checked, key=lambda item: item.planned_datetime)]}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def merge_generated(existing: Iterable[EditorialSlot], generated: Iterable[EditorialSlot]) -> tuple[EditorialSlot, ...]:
    by_id = {slot.slot_id: slot for slot in existing}
    for slot in generated:
        by_id.setdefault(slot.slot_id, slot)
    return tuple(sorted(by_id.values(), key=lambda item: item.planned_datetime))


def assign_slot(slots: Iterable[EditorialSlot], *, slot_id: str, slug: str, editorial_type: str, now: datetime, explicit_override: bool = False, override_reason: str | None = None) -> tuple[EditorialSlot, ...]:
    if editorial_type not in SLOT_TYPES or not _SLUG_RE.fullmatch(slug):
        raise EditorialCalendarError("classificação ou slug inválido")
    if now.tzinfo is None or now.utcoffset() is None:
        raise EditorialCalendarError("now sem timezone")
    items = list(slots)
    if any(item.assigned_slug == slug and item.status in {"assigned", "queued"} for item in items):
        raise EditorialCalendarError("publicação já ocupa outro slot")
    for index, slot in enumerate(items):
        if slot.slot_id != slot_id:
            continue
        if slot.status != "available" or slot.assigned_slug is not None:
            raise EditorialCalendarError("slot não está disponível")
        if slot.planned_datetime < now and not explicit_override:
            raise EditorialCalendarError("slot passado não pode ser associado")
        if slot.slot_type != editorial_type:
            raise EditorialCalendarError("tipo editorial incompatível com o slot")
        items[index] = replace(slot, status="assigned", assigned_slug=slug, explicit_override=explicit_override, override_reason=override_reason)
        return tuple(items)
    raise EditorialCalendarError("slot não encontrado")


def transition_slot(slots: Iterable[EditorialSlot], *, slot_id: str, status: str, occurred_at: datetime | None = None) -> tuple[EditorialSlot, ...]:
    if status not in {"queued", "published", "skipped"}:
        raise EditorialCalendarError("transição de slot inválida")
    items = list(slots)
    for index, slot in enumerate(items):
        if slot.slot_id != slot_id:
            continue
        if status == "queued" and slot.status != "assigned":
            raise EditorialCalendarError("somente slot assigned pode entrar na fila")
        if status == "published" and slot.status != "queued":
            raise EditorialCalendarError("somente slot queued pode ser publicado")
        if status == "skipped" and slot.status not in {"available", "assigned"}:
            raise EditorialCalendarError("slot não pode ser marcado como skipped")
        timestamp = occurred_at.isoformat() if occurred_at else None
        items[index] = replace(
            slot,
            status=status,
            assigned_slug=None if status == "skipped" else slot.assigned_slug,
            queued_at=timestamp if status == "queued" else slot.queued_at,
            actual_published_at=timestamp if status == "published" else None,
        )
        return tuple(items)
    raise EditorialCalendarError("slot não encontrado")


def next_available_slot(slots: Iterable[EditorialSlot], *, now: datetime) -> EditorialSlot | None:
    candidates = [slot for slot in slots if slot.status == "available" and slot.planned_datetime >= now]
    return min(candidates, key=lambda item: item.planned_datetime) if candidates else None


def render_calendar(slots: Iterable[EditorialSlot], *, limit: int = 20) -> str:
    lines = ["DATA | HORA | TIPO | POST | STATUS"]
    for slot in sorted(slots, key=lambda item: item.planned_datetime)[:limit]:
        planned = slot.planned_datetime.astimezone(ZoneInfo(slot.timezone))
        lines.append(f"{planned:%d/%m/%Y} | {planned:%H:%M} | {slot.slot_type} | {slot.assigned_slug or '—'} | {slot.status}")
    return "\n".join(lines)
