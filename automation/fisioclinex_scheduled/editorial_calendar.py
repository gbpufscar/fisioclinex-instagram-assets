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
    derived_story: bool = False

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
    if type(slot.derived_story) is not bool or (slot.derived_story and not slot.assigned_slug):
        raise EditorialCalendarError("Story derivado exige pauta associada")
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


def calendar_workspace(path: str | Path) -> Path | None:
    """Resolve the configured source of truth for the normal project calendar."""
    from .queue_config import load_queue_config
    root = Path(path).resolve().parent.parent
    config = root / "publicacao-agendada/config.json"
    if config.is_file():
        return load_queue_config(config).workspace_path
    if (root / "publication-state").is_dir() and Path(path).parent.name == "publication-state":
        return root
    return None


def save_calendar(path: str | Path, slots: Iterable[EditorialSlot], *, policy: CalendarPolicy, workspace: str | Path | None = None) -> None:
    from contextlib import nullcontext
    from .schedule_integrity import schedule_lock
    target = Path(path)
    source = workspace if workspace is not None else calendar_workspace(target)
    # Keep a consistent lock order with queue staging and direct writeback.
    with schedule_lock(source) if source is not None else nullcontext():
        with schedule_lock(target):
            _save_calendar(target, slots, policy=policy, workspace=source)


def _save_calendar(target: Path, slots: Iterable[EditorialSlot], *, policy: CalendarPolicy, workspace, release_slug=None) -> None:
    from .schedule_integrity import check_reservations, validate_schedule_write

    if target.is_symlink():
        raise EditorialCalendarError("destino de calendário inseguro")
    checked = tuple(_validate_slot(slot, policy) for slot in slots)
    if len({slot.slot_id for slot in checked}) != len(checked):
        raise EditorialCalendarError("slot_id duplicado")
    active = {"assigned", "queued", "published"}
    previous = load_calendar(target, policy=policy) if target.exists() else ()
    by_id = {slot.slot_id: slot for slot in checked}
    old_by_id = {slot.slot_id: slot for slot in previous}
    assigned = [s.assigned_slug for s in checked if s.assigned_slug and s.status in {"assigned", "queued"}]
    if len(set(assigned)) != len(assigned):
        raise EditorialCalendarError("publicação ocupa mais de um slot ativo")
    for old in previous:
        new = by_id.get(old.slot_id)
        if old.status in active and (new is None or new.status == "available" or
                (new.status in active and new.assigned_slug != old.assigned_slug)):
            if old.assigned_slug == release_slug and old.status in {"assigned", "queued"}:
                continue
            raise EditorialCalendarError(f"slot persistido ocupado: {old.slot_id} por {old.assigned_slug}")
    reservations = tuple((s.assigned_slug, s.planned_datetime) for s in checked
                         if s.status in active and s.assigned_slug)
    for slot in checked:
        old = old_by_id.get(slot.slot_id)
        if slot.status not in active or (old is not None and old.assigned_slug == slot.assigned_slug
                and old.planned_datetime == slot.planned_datetime and old.status in active):
            continue
        check_reservations(slot.assigned_slug, slot.planned_datetime, reservations,
                           explicit_override=slot.explicit_override, override_reason=slot.override_reason)
        if workspace is not None:
            validate_schedule_write(workspace, slug=slot.assigned_slug, planned_at=slot.planned_datetime,
                                    explicit_override=slot.explicit_override, override_reason=slot.override_reason)
    payload = {"schema_version": 1, "policy_version": policy.schema_version, "slots": [_slot_json(slot) for slot in sorted(checked, key=lambda item: item.planned_datetime)]}
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


def assign_slot(slots: Iterable[EditorialSlot], *, slot_id: str, slug: str, editorial_type: str, now: datetime, explicit_override: bool = False, override_reason: str | None = None, derived_story: bool = False) -> tuple[EditorialSlot, ...]:
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
        items[index] = replace(slot, status="assigned", assigned_slug=slug, explicit_override=explicit_override, override_reason=override_reason, derived_story=derived_story)
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
            derived_story=False if status == "skipped" else slot.derived_story,
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
        lines.append(f"{planned:%d/%m/%Y} | {planned:%H:%M} | {slot.slot_type} Feed | {slot.assigned_slug or '—'} | {slot.status}")
        if slot.derived_story and slot.status in {"assigned", "queued"}:
            from .schedule_integrity import story_planned_at
            story = story_planned_at(slot.planned_at)
            lines.append(f"{story:%d/%m/%Y} | {story:%H:%M} | ↳ Story derivado | {slot.assigned_slug} | planejado; execução desabilitada")
    return "\n".join(lines)


def _slot_json(slot):
    data = asdict(slot)
    if not slot.derived_story:
        data.pop('derived_story')  # Do not add Story metadata to historical slots.
    return data


def project_calendar_occurrences(slots):
    from .schedule_integrity import story_planned_at, validate_occurrences
    result = []
    for slot in slots:
        if not slot.assigned_slug or slot.status not in {'assigned', 'queued'}:
            continue
        feed_id = slot.assigned_slug + ':feed'
        result.append(dict(occurrence_id=feed_id, parent_id=None, slug=slot.assigned_slug,
                           surface='feed', planned_at=slot.planned_at, reserves_feed=True))
        if slot.derived_story:
            result.append(dict(occurrence_id=slot.assigned_slug+':story', parent_id=feed_id,
                               slug=slot.assigned_slug, surface='story',
                               planned_at=story_planned_at(slot.planned_at).isoformat(), reserves_feed=False))
    validate_occurrences(result)
    return tuple(result)


def _move_association(slots, *, slug, target_slot_id, cancel=False):
    active = [s for s in slots if s.assigned_slug == slug and s.status in {'assigned', 'queued', 'published'}]
    if len(active) != 1 or active[0].status == 'published':
        raise EditorialCalendarError('reagendamento exige uma pauta ainda não publicada')
    old = active[0]
    if cancel:
        return tuple(replace(s, status='skipped', assigned_slug=None, queued_at=None,
                             derived_story=False, explicit_override=False, override_reason=None)
                     if s.slot_id == old.slot_id else s for s in slots), old, None
    targets = [s for s in slots if s.slot_id == target_slot_id]
    if len(targets) != 1:
        raise EditorialCalendarError('slot de destino não encontrado')
    target = targets[0]
    if target.slot_id == old.slot_id:
        return tuple(slots), old, old
    if target.status != 'available' or target.slot_type != old.slot_type:
        raise EditorialCalendarError('destino indisponível ou tipo editorial incompatível')
    moved = replace(target, status=old.status, assigned_slug=slug, queued_at=old.queued_at,
                    derived_story=old.derived_story, explicit_override=old.explicit_override,
                    override_reason=old.override_reason)
    freed = replace(old, status='available', assigned_slug=None, queued_at=None,
                    derived_story=False, explicit_override=False, override_reason=None)
    return tuple(moved if s.slot_id == target.slot_id else freed if s.slot_id == old.slot_id
                 else s for s in slots), old, moved


def _atomic_bytes(path, payload):
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_planning(calendar, *, policy, slug, target_slot_id=None, workspace=None, cancel=False):
    """Explicit local operation. Locks + atomic files + byte rollback, no Git/Meta.

    Cross-file crash atomicity is not claimed. An interrupted process needs review.
    """
    from contextlib import ExitStack
    from .schedule_integrity import schedule_lock, validate_schedule_write, story_planned_at
    from .manifest import parse_manifest
    from .queue_package import manifest_json
    from .story_state import StoryState
    from .states import QueueState
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise EditorialCalendarError('slug inválida')
    path = Path(calendar)
    if path.is_symlink():
        raise EditorialCalendarError('calendário inseguro')
    root = Path(workspace).resolve(strict=True) if workspace is not None else calendar_workspace(path)
    with ExitStack() as locks:
        if root is not None:
            locks.enter_context(schedule_lock(root))
        locks.enter_context(schedule_lock(path))
        queue_path = root / 'publication-state/queue' / slug / 'manifest.json' if root else None
        if queue_path and (queue_path.is_symlink() or queue_path.parent.is_symlink()):
            raise EditorialCalendarError('manifest inseguro')
        stored = parse_manifest(queue_path.read_bytes()) if queue_path and queue_path.exists() else None
        if stored is not None and stored.slug != slug:
            raise EditorialCalendarError("identidade do manifest diverge da pauta")
        slots = load_calendar(path, policy=policy)
        if root is not None:
            from .registry import read_registry
            if any(record.slug == slug for record in read_registry(root/'publication-state/publications.jsonl')):
                raise EditorialCalendarError('feed confirmado no registry não pode ser reagendado/cancelado')
        if stored is None and cancel and not any(s.assigned_slug == slug for s in slots):
            return 'unchanged'
        if stored is not None:
            if stored.status is QueueState.CANCELLED and cancel:
                if any(s.assigned_slug == slug and s.status in {"assigned", "queued", "published"} for s in slots):
                    raise EditorialCalendarError("cancelamento divergente exige revisão")
                mirror = root / 'publication-state/editorial-calendar.json'
                if mirror.resolve() != path.resolve() and mirror.exists():
                    locks.enter_context(schedule_lock(mirror))
                    if any(s.assigned_slug == slug and s.status in {'assigned', 'queued', 'published'}
                           for s in load_calendar(mirror, policy=policy)):
                        raise EditorialCalendarError('espelho cancelado divergente exige revisão')
                return 'cancelled'
            if stored.status not in {QueueState.QUEUED, QueueState.PAUSED, QueueState.FAILED_BEFORE_META} or stored.publication.media_id or stored.publication.published_at:
                raise EditorialCalendarError('feed publicado, em execução ou ambíguo não pode ser reagendado')
            if stored.story and stored.story.status is not StoryState.PENDING:
                raise EditorialCalendarError('Story iniciado/publicado não pode ser reagendado')
        moved, old, new = _move_association(slots, slug=slug, target_slot_id=target_slot_id, cancel=cancel)
        if stored is None and old.status == 'queued':
            raise EditorialCalendarError('slot queued exige manifest operacional')
        if stored is not None and (stored.slot_id != old.slot_id or stored.planned_at != old.planned_datetime):
            raise EditorialCalendarError('calendário e fila divergentes; revisão necessária')
        if stored is not None and old.derived_story != (stored.story_plan_version == 1):
            raise EditorialCalendarError('vínculo Story do calendário diverge do manifest')
        plans = [(path, moved)]
        # Update an existing operational mirror too, never invent one from local state.
        mirror = root / 'publication-state/editorial-calendar.json' if root else None
        if mirror and mirror.resolve() != path.resolve() and mirror.exists():
            locks.enter_context(schedule_lock(mirror))
            mirror_slots = load_calendar(mirror, policy=policy)
            mirrors, mirror_old, _ = _move_association(mirror_slots, slug=slug, target_slot_id=target_slot_id, cancel=cancel)
            if (mirror_old.slot_id, mirror_old.planned_at, mirror_old.derived_story) != (old.slot_id, old.planned_at, old.derived_story):
                raise EditorialCalendarError('espelho operacional divergente')
            plans.append((mirror, mirrors))
        if new == old:
            return 'unchanged'
        if new is not None and root is not None:
            validate_schedule_write(root, slug=slug, planned_at=new.planned_datetime,
                                    explicit_override=new.explicit_override, override_reason=new.override_reason)
        updated = stored
        if stored is not None:
            if cancel:
                child = replace(stored.story, not_before=None) if stored.story else None
                updated = replace(stored, status=QueueState.CANCELLED, story=child)
            else:
                from datetime import timedelta
                delay = max(timedelta(), (stored.not_before or stored.planned_at)-stored.planned_at)
                child = replace(stored.story, not_before=story_planned_at(new.planned_at)) if stored.story_plan_version else stored.story
                updated = replace(stored, slot_id=new.slot_id, planned_at=new.planned_datetime,
                                  not_before=new.planned_datetime+delay, slot_type=new.slot_type,
                                  explicit_override=new.explicit_override, override_reason=new.override_reason, story=child)
            updated = parse_manifest(manifest_json(updated))
        backups = {p: p.read_bytes() for p, _ in plans}
        if stored is not None:
            backups[queue_path] = queue_path.read_bytes()
        try:
            for target, values in plans:
                _save_calendar(target, values, policy=policy, workspace=root, release_slug=slug)
            if updated is not None:
                _atomic_bytes(queue_path, manifest_json(updated))
            for target, _ in plans:
                project_calendar_occurrences(load_calendar(target, policy=policy))
            if updated is not None:
                parse_manifest(queue_path.read_bytes())
        except BaseException as failure:
            rollback_errors = []
            for target, payload in backups.items():
                try:
                    if target.read_bytes() != payload:
                        _atomic_bytes(target, payload)
                except OSError:
                    rollback_errors.append(str(target))
            if rollback_errors:
                raise EditorialCalendarError("rollback incompleto; revisão necessária: "
                                             + ", ".join(rollback_errors)) from failure
            raise
    return 'cancelled' if cancel else 'rescheduled'
