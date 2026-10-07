"""Single feed eligibility policy shared by selection and projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .editorial_calendar import EditorialSlot


@dataclass(frozen=True, slots=True)
class FeedEligibility:
    eligible: bool
    reason: str


def evaluate_feed_spacing(
    *,
    now: datetime,
    published_at: tuple[datetime, ...],
    timezone_name: str = "America/Sao_Paulo",
    explicit_override: bool = False,
    override_reason: str | None = None,
) -> FeedEligibility:
    override_valid = explicit_override and isinstance(override_reason, str) and bool(override_reason.strip())
    aware_history = tuple(value for value in published_at if value.tzinfo is not None and value.utcoffset() is not None)
    if aware_history:
        zone = ZoneInfo(timezone_name)
        same_day = any(value.astimezone(zone).date() == now.astimezone(zone).date() for value in aware_history)
        if same_day and not override_valid:
            return FeedEligibility(False, "daily_feed_limit")
    if explicit_override and not override_valid:
        return FeedEligibility(False, "override_requires_reason")
    return FeedEligibility(True, "explicit_override" if override_valid else "eligible")


def manifest_slot(manifest) -> EditorialSlot | None:
    if manifest.slot_id is None:
        return None
    return EditorialSlot(
        slot_id=manifest.slot_id,
        planned_at=manifest.planned_at.isoformat(),
        timezone="America/Sao_Paulo",
        slot_type=manifest.slot_type,
        status="queued",
        assigned_slug=manifest.slug,
        explicit_override=manifest.explicit_override,
        override_reason=manifest.override_reason,
    )


def evaluate_feed_eligibility(
    *,
    now: datetime,
    not_before: datetime | None,
    slot: EditorialSlot | None,
    published_at: tuple[datetime, ...] = (),
    legacy: bool = False,
    explicit_override: bool = False,
    override_reason: str | None = None,
) -> FeedEligibility:
    if now.tzinfo is None or now.utcoffset() is None:
        return FeedEligibility(False, "now_without_timezone")
    if not_before is not None and not_before > now:
        return FeedEligibility(False, "not_before")
    if legacy:
        return evaluate_feed_spacing(now=now, published_at=published_at,
            explicit_override=explicit_override, override_reason=override_reason)
    if slot is None or slot.status != "queued" or slot.assigned_slug is None:
        return FeedEligibility(False, "slot_not_queued")
    planned = slot.planned_datetime
    local_now = now.astimezone(ZoneInfo(slot.timezone))
    local_planned = planned.astimezone(ZoneInfo(slot.timezone))
    if local_now < local_planned:
        return FeedEligibility(False, "slot_not_started")
    if local_now.date() != local_planned.date():
        return FeedEligibility(False, "slot_missed_no_catch_up")
    return evaluate_feed_spacing(
        now=now,
        published_at=published_at,
        timezone_name=slot.timezone,
        explicit_override=explicit_override,
        override_reason=override_reason,
    )
