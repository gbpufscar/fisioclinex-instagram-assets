"""Write-side reservations against current persisted state; never repairs a queue."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Sao_Paulo")


class ScheduleConflictError(ValueError):
    def __init__(self, slug, planned_at, existing_slug, existing_at, reason):
        self.code = "schedule_conflict"
        self.reason = reason
        self.slug = slug
        self.planned_at = planned_at.isoformat()
        self.existing_slug = existing_slug
        self.existing_at = existing_at.isoformat()
        self.day = planned_at.astimezone(ZONE).date().isoformat()
        super().__init__(f"{reason}: {slug} em {self.planned_at}; ocupado por "
                         f"{existing_slug} em {self.existing_at}")

    def to_dict(self):
        return {key: getattr(self, key) for key in (
            "code", "reason", "slug", "planned_at", "existing_slug", "existing_at", "day"
        )}


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("schedule timestamp must include timezone")
    return result


@contextmanager
def schedule_lock(resource):
    """Serialize read/check/write across local CLI processes, without tracked files."""
    key = hashlib.sha256(str(Path(resource).resolve()).encode()).hexdigest()
    name = Path(tempfile.gettempdir()) / f"fisioclinex-schedule-{os.getuid()}-{key}.lock"
    fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def check_reservations(slug, planned_at, reservations, *, explicit_override=False, override_reason=None):
    planned = timestamp(planned_at)
    # An editorial override never authorizes two reservations on the same day.
    allow_interval = explicit_override and isinstance(override_reason, str) and bool(override_reason.strip())
    for other_slug, other_at in reservations:
        if other_slug == slug:
            continue
        other = timestamp(other_at)
        from .feed_eligibility import evaluate_feed_spacing
        earlier, later = sorted((planned, other))
        decision = evaluate_feed_spacing(now=later, published_at=(earlier,))
        if not decision.eligible and not (decision.reason == "minimum_interval" and allow_interval):
            raise ScheduleConflictError(slug, planned, other_slug, other, decision.reason)


def persisted_reservations(workspace):
    """Read freshly on every call, including in-flight and failed reserved feeds."""
    from .manifest import parse_manifest
    from .registry import read_registry
    root = Path(workspace).resolve(strict=True)
    queue = root / "publication-state" / "queue"
    if queue.is_symlink():
        raise ValueError("unsafe queue root")
    reservations = []
    for path in sorted(queue.glob("*/manifest.json")):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("unsafe queue manifest")
        manifest = parse_manifest(path.read_bytes())
        if manifest.status.value == "cancelled":
            continue
        if manifest.publication.published_at is not None:
            reservations.append((manifest.slug, manifest.publication.published_at))
        elif manifest.planned_at is not None:
            reservations.append((manifest.slug, manifest.planned_at))
        elif manifest.not_before is not None:
            reservations.append((manifest.slug, manifest.not_before))
    reservations.extend((r.slug, r.published_at) for r in read_registry(root / "publication-state/publications.jsonl"))
    # Editorial associations also reserve a day, even before queue staging.
    calendar = root / "publication-state/editorial-calendar.json"
    if calendar.exists():
        if calendar.is_symlink():
            raise ValueError("unsafe editorial calendar")
        data = json.loads(calendar.read_text(encoding="utf-8"))
        for item in data["slots"]:
            if item.get("assigned_slug") and item["status"] in {"assigned", "queued", "published"}:
                reservations.append((item["assigned_slug"], item.get("actual_published_at") or item["planned_at"]))
    return tuple(reservations)


def validate_schedule_write(workspace, *, slug, planned_at, explicit_override=False, override_reason=None):
    check_reservations(slug, planned_at, persisted_reservations(workspace),
                       explicit_override=explicit_override, override_reason=override_reason)


def story_planned_at(feed_planned_at):
    """S4 fixed planning barrier; never based on actual feed publication."""
    from datetime import time
    if not isinstance(feed_planned_at, (datetime, str)):
        raise ValueError("Story planning requires an aware feed timestamp")
    local = timestamp(feed_planned_at).astimezone(ZONE)
    if (local.hour, local.minute, local.second, local.microsecond) != (12, 0, 0, 0):
        raise ValueError("Story planning requires a feed at 12:00 America/Sao_Paulo")
    return datetime.combine(local.date(), time(18), ZONE)


def validate_story_plan(feed_planned_at, story_not_before):
    if timestamp(story_not_before) != story_planned_at(feed_planned_at):
        raise ValueError("Story planning must be 18:00 on its feed's local day")


def validate_occurrences(occurrences):
    """Validate derived projections without reserving another editorial slot."""
    items = tuple(occurrences)
    ids = [item['occurrence_id'] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate occurrence identity")
    feeds = {item['occurrence_id']: item for item in items if item['surface'] == 'feed'}
    if len({item["slug"] for item in feeds.values()}) != len(feeds):
        raise ValueError("one feed occurrence per topic is required")
    children = set()
    for item in items:
        if item['surface'] == 'feed':
            if item['parent_id'] is not None or not item['reserves_feed']:
                raise ValueError("invalid feed occurrence")
        elif item['surface'] == 'story':
            parent = feeds.get(item['parent_id'])
            if parent is None or parent['slug'] != item['slug'] or item['reserves_feed']:
                raise ValueError("Story must have a matching feed parent and no feed reservation")
            if item['parent_id'] in children:
                raise ValueError("duplicate Story child")
            children.add(item['parent_id'])
            validate_story_plan(parent['planned_at'], item['planned_at'])
        else:
            raise ValueError("unknown occurrence surface")
    reservations = tuple((item['slug'], timestamp(item['planned_at'])) for item in feeds.values())
    for slug, planned in reservations:
        check_reservations(slug, planned, reservations)
