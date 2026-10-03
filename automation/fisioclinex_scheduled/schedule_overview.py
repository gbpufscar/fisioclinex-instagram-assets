"""Read-only projection of the canonical FisioClinEx publication queue."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from .editorial_calendar import EditorialSlot
from .feed_eligibility import evaluate_feed_eligibility
from .registry import read_registry

CANONICAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")
CANONICAL_HOUR = 12
CANONICAL_MINUTE = 0
CANONICAL_WEEKDAYS = frozenset({0, 1, 2, 3, 4})
LEGACY_HOUR = 11
LEGACY_MINUTE = 17
LEGACY_WEEKDAYS = frozenset({0, 2, 4})
PORTUGUESE_WEEKDAYS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


class ScheduleOverviewError(ValueError):
    """Raised when the queue cannot be projected safely."""


@dataclass(frozen=True, slots=True)
class QueueEntry:
    slug: str
    short_slug: str
    priority: int
    queued_at: datetime
    not_before: datetime | None
    planned_at: datetime | None = None
    slot_type: str | None = None
    explicit_override: bool = False
    override_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledPost:
    position: int
    slug: str
    short_slug: str
    scheduled_at: str
    date: str
    weekday: str
    time: str
    timezone: str
    priority: int
    status: str = "projected"
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _timestamp(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ScheduleOverviewError(f"{field} inválido")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleOverviewError(f"{field} inválido") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScheduleOverviewError(f"{field} sem fuso horário")
    return parsed


def _load_entry(path: Path) -> QueueEntry | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScheduleOverviewError(f"manifesto inválido: {path.parent.name}") from exc
    if not isinstance(data, dict):
        raise ScheduleOverviewError(f"manifesto inválido: {path.parent.name}")
    publication = data.get("publication")
    if not isinstance(publication, dict):
        raise ScheduleOverviewError(f"manifesto sem publicação: {path.parent.name}")
    if data.get("status") != "queued" or publication.get("media_id") is not None:
        return None
    slug = data.get("slug")
    short_slug = data.get("short_slug")
    priority = data.get("priority")
    if (
        not isinstance(slug, str)
        or not isinstance(short_slug, str)
        or not isinstance(priority, int)
        or isinstance(priority, bool)
    ):
        raise ScheduleOverviewError(f"identidade inválida: {path.parent.name}")
    queued_at = _timestamp(data.get("queued_at"), "queued_at")
    not_before = _timestamp(data.get("not_before"), "not_before", optional=True)
    assert queued_at is not None
    planned_at = _timestamp(data.get("planned_at"), "planned_at", optional=True)
    slot_type = data.get("slot_type")
    if planned_at is not None and slot_type not in {"scientific", "other"}:
        raise ScheduleOverviewError(f"slot inválido: {path.parent.name}")
    explicit_override = data.get("explicit_override", False)
    override_reason = data.get("override_reason")
    return QueueEntry(slug, short_slug, priority, queued_at, not_before, planned_at, slot_type, explicit_override, override_reason)


def load_queued_entries(workspace: str | Path) -> tuple[QueueEntry, ...]:
    root = Path(workspace).expanduser().resolve(strict=True)
    queue = root / "publication-state" / "queue"
    if not queue.is_dir() or queue.is_symlink():
        return ()
    entries = []
    for path in sorted(queue.glob("*/manifest.json")):
        if path.is_symlink() or not path.is_file():
            raise ScheduleOverviewError("manifesto de fila inseguro")
        entry = _load_entry(path)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def next_canonical_slot(after: datetime) -> datetime:
    if after.tzinfo is None or after.utcoffset() is None:
        raise ScheduleOverviewError("instante atual sem fuso horário")
    local = after.astimezone(CANONICAL_TIMEZONE)
    day = local.date()
    for offset in range(0, 8):
        candidate_day = day + timedelta(days=offset)
        if candidate_day.weekday() not in CANONICAL_WEEKDAYS:
            continue
        candidate = datetime.combine(
            candidate_day,
            time(CANONICAL_HOUR, CANONICAL_MINUTE),
            tzinfo=CANONICAL_TIMEZONE,
        )
        if candidate > local:
            return candidate
    raise ScheduleOverviewError("não foi possível localizar o próximo slot canônico")


def next_legacy_slot(after: datetime) -> datetime:
    local = after.astimezone(CANONICAL_TIMEZONE)
    day = local.date()
    for offset in range(0, 8):
        candidate_day = day + timedelta(days=offset)
        if candidate_day.weekday() not in LEGACY_WEEKDAYS:
            continue
        candidate = datetime.combine(candidate_day, time(LEGACY_HOUR, LEGACY_MINUTE), tzinfo=CANONICAL_TIMEZONE)
        if candidate > local:
            return candidate
    raise ScheduleOverviewError("não foi possível localizar o próximo slot legado")


def project_schedule(
    entries: tuple[QueueEntry, ...] | list[QueueEntry],
    *,
    now: datetime,
    published_at: tuple[datetime, ...] = (),
) -> tuple[ScheduledPost, ...]:
    """Project the current queue using the selector's priority/queued_at/slug order."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ScheduleOverviewError("instante atual sem fuso horário")
    remaining = list(entries)
    projected: list[ScheduledPost] = []
    simulated_history = list(published_at)
    legacy_slot = next_legacy_slot(now)
    def append(entry: QueueEntry, candidate: datetime, reason: str | None = None) -> None:
        local = candidate.astimezone(CANONICAL_TIMEZONE)
        projected.append(ScheduledPost(
            position=len(projected) + 1,
            slug=entry.slug,
            short_slug=entry.short_slug,
            scheduled_at=local.isoformat(),
            date=local.strftime("%d/%m/%Y"),
            weekday=PORTUGUESE_WEEKDAYS[local.weekday()],
            time=local.strftime("%Hh%M"),
            timezone=str(CANONICAL_TIMEZONE),
            priority=entry.priority,
            status="blocked" if reason else "projected",
            reason=reason,
        ))

    while remaining:
        selected = min(remaining, key=lambda entry: (
            entry.planned_at or legacy_slot,
            entry.priority, entry.queued_at, entry.slug,
        ))
        candidate = selected.planned_at or legacy_slot
        slot = None
        if selected.planned_at is not None:
            slot = EditorialSlot(
                slot_id=f"{candidate.astimezone(CANONICAL_TIMEZONE):%Y-%m-%dT%H:%M}-{selected.slot_type}",
                planned_at=candidate.isoformat(),
                timezone=str(CANONICAL_TIMEZONE),
                slot_type=selected.slot_type,
                status="queued",
                assigned_slug=selected.slug,
                explicit_override=selected.explicit_override,
                override_reason=selected.override_reason,
            )
        # A late runner can still publish today, but never catches up on past days.
        evaluation_time = max(candidate, now)
        decision = evaluate_feed_eligibility(
            now=evaluation_time, not_before=selected.not_before, slot=slot,
            published_at=tuple(simulated_history),
            legacy=selected.planned_at is None,
            explicit_override=selected.explicit_override,
            override_reason=selected.override_reason,
        )
        if selected.planned_at is None and not decision.eligible:
            # Only legacy entries may move to another legacy opportunity.
            legacy_slot = next_legacy_slot(max(legacy_slot, selected.not_before or legacy_slot))
            continue
        append(selected, candidate, None if decision.eligible else decision.reason)
        remaining.remove(selected)
        if decision.eligible:
            simulated_history.append(evaluation_time)
        if selected.planned_at is None:
            legacy_slot = next_legacy_slot(legacy_slot)
    return tuple(projected)


def build_schedule_overview(
    workspace: str | Path,
    *,
    now: datetime,
) -> tuple[ScheduledPost, ...]:
    root = Path(workspace).expanduser().resolve(strict=True)
    history = tuple(
        datetime.fromisoformat(record.published_at.replace("Z", "+00:00"))
        for record in read_registry(root / "publication-state" / "publications.jsonl")
    )
    return project_schedule(load_queued_entries(root), now=now, published_at=history)


def format_schedule_overview(posts: tuple[ScheduledPost, ...]) -> str:
    reasons = {
        "daily_feed_limit": "outro post ocupa o mesmo dia",
        "minimum_interval": "intervalo mínimo de 24 horas",
        "slot_missed_no_catch_up": "horário vencido; requer decisão humana",
        "not_before": "barreira not_before posterior ao horário",
    }
    lines = [
        "",
        "PRÓXIMAS PUBLICAÇÕES AGENDADAS — PROJEÇÃO ATUAL",
        "Calendário novo: segunda a sexta-feira às 12h00 (America/Sao_Paulo)",
    ]
    if not posts:
        lines.append("Nenhum post permanece na fila.")
    else:
        for post in posts:
            lines.append(
                f"{post.position}. {post.weekday}, {post.date}, às {post.time} — "
                f"{post.short_slug}"
                + (f" — BLOQUEADO: {reasons.get(post.reason, post.reason)}" if post.status == "blocked" else "")
            )
    lines.append(
        "Observação: itens com slot usam o horário planejado persistido; itens legados "
        "mantêm a projeção anterior de segunda, quarta e sexta às 11h17."
    )
    return "\n".join(lines)


def safe_format_schedule_overview(
    workspace: str | Path,
    *,
    now: datetime,
) -> str:
    """Render a non-fatal operational summary after an irreversible success."""
    try:
        posts = build_schedule_overview(workspace, now=now)
    except (OSError, ScheduleOverviewError) as exc:
        return (
            "\nPRÓXIMAS PUBLICAÇÕES AGENDADAS\n"
            "Resumo indisponível: a operação principal foi concluída, mas a fila "
            f"não pôde ser projetada com segurança. Motivo: {exc}"
        )
    return format_schedule_overview(posts)
