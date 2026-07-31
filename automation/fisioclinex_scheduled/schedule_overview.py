"""Read-only projection of the canonical FisioClinEx publication queue."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CANONICAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")
CANONICAL_HOUR = 11
CANONICAL_MINUTE = 17
CANONICAL_WEEKDAYS = frozenset({0, 2, 4})  # segunda, quarta e sexta
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
    return QueueEntry(slug, short_slug, priority, queued_at, not_before)


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


def project_schedule(
    entries: tuple[QueueEntry, ...] | list[QueueEntry],
    *,
    now: datetime,
) -> tuple[ScheduledPost, ...]:
    """Project the current queue using the selector's priority/queued_at/slug order."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ScheduleOverviewError("instante atual sem fuso horário")
    remaining = list(entries)
    projected: list[ScheduledPost] = []
    slot = next_canonical_slot(now)
    safety_limit = max(3660, len(remaining) * 14)
    attempts = 0
    while remaining:
        attempts += 1
        if attempts > safety_limit:
            raise ScheduleOverviewError("projeção excedeu o limite seguro")
        eligible = [
            entry
            for entry in remaining
            if entry.not_before is None or entry.not_before <= slot
        ]
        if not eligible:
            slot = next_canonical_slot(slot)
            continue
        selected = min(
            eligible,
            key=lambda entry: (entry.priority, entry.queued_at, entry.slug),
        )
        projected.append(
            ScheduledPost(
                position=len(projected) + 1,
                slug=selected.slug,
                short_slug=selected.short_slug,
                scheduled_at=slot.isoformat(),
                date=slot.strftime("%d/%m/%Y"),
                weekday=PORTUGUESE_WEEKDAYS[slot.weekday()],
                time=slot.strftime("%Hh%M"),
                timezone=str(CANONICAL_TIMEZONE),
                priority=selected.priority,
            )
        )
        remaining.remove(selected)
        slot = next_canonical_slot(slot)
    return tuple(projected)


def build_schedule_overview(
    workspace: str | Path,
    *,
    now: datetime,
) -> tuple[ScheduledPost, ...]:
    return project_schedule(load_queued_entries(workspace), now=now)


def format_schedule_overview(posts: tuple[ScheduledPost, ...]) -> str:
    lines = [
        "",
        "PRÓXIMAS PUBLICAÇÕES AGENDADAS — PROJEÇÃO ATUAL",
        "Calendário: segunda, quarta e sexta-feira às 11h17 (America/Sao_Paulo)",
    ]
    if not posts:
        lines.append("Nenhum post permanece na fila.")
    else:
        for post in posts:
            lines.append(
                f"{post.position}. {post.weekday}, {post.date}, às {post.time} — "
                f"{post.short_slug}"
            )
    lines.append(
        "Observação: as datas são projetadas pela fila atual e podem mudar com "
        "prioridade, not_before ou novas inclusões."
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
    except (OSError, ScheduleOverviewError):
        return (
            "\nPRÓXIMAS PUBLICAÇÕES AGENDADAS\n"
            "Resumo indisponível: a operação principal foi concluída, mas a fila "
            "não pôde ser projetada com segurança."
        )
    return format_schedule_overview(posts)
