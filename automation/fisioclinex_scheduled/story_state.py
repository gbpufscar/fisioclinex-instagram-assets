"""Pure contract for a Story child; no runner, clock, network or scheduler."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Mapping

from .fingerprint import build_story_publication_key


class StoryStateError(ValueError):
    pass


class FeedState(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class StoryState(StrEnum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


MAX_STORY_ATTEMPTS = 2
STORY_RETRY_DELAY = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class StoryError:
    phase: str
    classification: str  # confirmed_not_published or ambiguous
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class StoryPublication:
    status: StoryState
    publication_key: str
    not_before: datetime | None = None
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    container_id: str | None = None
    media_id: str | None = None
    published_at: datetime | None = None
    last_error: StoryError | None = None


def _timestamp(value, field, *, optional=True):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise StoryStateError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StoryStateError(f"{field} must be a timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StoryStateError(f"{field} must include a timezone")
    return parsed


def _string(value, field):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise StoryStateError(f"{field} must be a nonempty string or null")
    return value


def parse_story(data: Mapping, *, slug: str, package_sha256: str,
                feed_published_at: datetime | None) -> StoryPublication:
    fields = set(StoryPublication.__dataclass_fields__)
    if not isinstance(data, Mapping) or set(data) != fields:
        raise StoryStateError("story fields are incomplete or unknown")
    try:
        status = StoryState(data["status"])
    except (ValueError, TypeError):
        raise StoryStateError("story status is unknown") from None
    if data["publication_key"] != build_story_publication_key(slug, package_sha256):
        raise StoryStateError("story identity differs from its feed/package")
    count = data["attempt_count"]
    if type(count) is not int or not 0 <= count <= MAX_STORY_ATTEMPTS:
        raise StoryStateError("story attempt_count must be between 0 and 2")
    last = _timestamp(data["last_attempt_at"], "story.last_attempt_at")
    published = _timestamp(data["published_at"], "story.published_at")
    not_before = _timestamp(data["not_before"], "story.not_before")
    container = _string(data["container_id"], "story.container_id")
    media = _string(data["media_id"], "story.media_id")
    error = data["last_error"]
    if error is not None:
        if not isinstance(error, Mapping) or set(error) != set(StoryError.__dataclass_fields__):
            raise StoryStateError("story.last_error fields are invalid")
        phase = _string(error["phase"], "story.last_error.phase")
        if phase is None or not isinstance(error["classification"], str) or error["classification"] not in {"confirmed_not_published", "ambiguous"}:
            raise StoryStateError("story failure classification is invalid")
        error = StoryError(phase, error["classification"],
                           _timestamp(error["occurred_at"], "story.last_error.occurred_at", optional=False))
    if (count == 0) != (last is None):
        raise StoryStateError("story attempt count and timestamp disagree")
    if status != StoryState.PENDING and feed_published_at is None:
        raise StoryStateError("story action requires a confirmed published feed")
    if last is not None and (feed_published_at is None or last < feed_published_at):
        raise StoryStateError("story attempt precedes the feed")
    if published is not None and status != StoryState.PUBLISHED:
        raise StoryStateError("story published_at requires published status")
    if status == StoryState.PUBLISHED:
        if not media or not published or not container or count == 0:
            raise StoryStateError("published story requires IDs, attempt and timestamp")
        if published < last:
            raise StoryStateError("story publication precedes its attempt")
    elif status != StoryState.AMBIGUOUS and media is not None:
        raise StoryStateError("story media_id requires published or ambiguous status")
    if status == StoryState.PENDING and (count or error or container):
        raise StoryStateError("pending story must be unattempted")
    if status in {StoryState.PUBLISHING, StoryState.FAILED, StoryState.AMBIGUOUS} and count == 0:
        raise StoryStateError("story state requires an attempt")
    if status in {StoryState.FAILED, StoryState.AMBIGUOUS}:
        expected = "ambiguous" if status == StoryState.AMBIGUOUS else "confirmed_not_published"
        if error is None or error.classification != expected:
            raise StoryStateError("story state and failure classification disagree")
    if error is not None and (last is None or error.occurred_at < last):
        raise StoryStateError("story error precedes its attempt")
    if status == StoryState.ELIGIBLE:
        if not_before is None or count >= MAX_STORY_ATTEMPTS:
            raise StoryStateError("eligible story requires a barrier and remaining attempt")
        if count and (error is None or error.classification != "confirmed_not_published"
                      or not_before < error.occurred_at + STORY_RETRY_DELAY):
            raise StoryStateError("story retry is not safe or its barrier is too early")
    if status in {StoryState.PUBLISHING, StoryState.PUBLISHED} and (not_before is None or last < not_before):
        raise StoryStateError("story attempt precedes its eligibility barrier")
    if status == StoryState.PUBLISHING and error is not None:
        raise StoryStateError("publishing story cannot retain a previous failure")
    if status == StoryState.PUBLISHED and error is not None:
        if error.classification != "ambiguous" or published < error.occurred_at:
            raise StoryStateError("published story requires coherent reconciliation evidence")
    return StoryPublication(status, data["publication_key"], not_before, count, last,
                            container, media, published, error)


def story_json(story: StoryPublication) -> dict:
    value = asdict(story)
    value["status"] = story.status.value
    for key in ("not_before", "last_attempt_at", "published_at"):
        value[key] = value[key].isoformat() if value[key] is not None else None
    if value["last_error"] is not None:
        value["last_error"]["occurred_at"] = story.last_error.occurred_at.isoformat()
    return value


def transition_story(story: StoryPublication, target: StoryState, *, now: datetime,
                     slug: str, package_sha256: str, feed_published_at: datetime | None,
                     not_before: datetime | None = None, container_id: str | None = None,
                     media_id: str | None = None, phase: str | None = None,
                     reconciliation: str | None = None) -> StoryPublication:
    """Validate an explicit state change; never execute a retry or calculate a schedule.

    Reconciliation evidence is supplied by a future caller, never inferred here.
    """
    story = parse_story(story_json(story), slug=slug, package_sha256=package_sha256,
                        feed_published_at=feed_published_at)
    _timestamp(now.isoformat(), "now", optional=False)
    target = StoryState(target)
    allowed = {
        StoryState.PENDING: {StoryState.ELIGIBLE},
        StoryState.ELIGIBLE: {StoryState.PUBLISHING},
        StoryState.PUBLISHING: {StoryState.PUBLISHED, StoryState.FAILED, StoryState.AMBIGUOUS},
        StoryState.FAILED: {StoryState.ELIGIBLE},
        StoryState.AMBIGUOUS: {StoryState.PUBLISHED, StoryState.FAILED},
        StoryState.PUBLISHED: set(),
    }
    if target not in allowed[story.status]:
        raise StoryStateError("story transition is not allowed")
    changes = {"status": target}
    if target == StoryState.ELIGIBLE:
        barrier = not_before if not_before is not None else story.not_before
        if barrier is None or now < barrier or feed_published_at is None or now < feed_published_at:
            raise StoryStateError("story is not yet eligible")
        changes["not_before"] = barrier
    elif target == StoryState.PUBLISHING:
        if story.not_before is None or now < story.not_before:
            raise StoryStateError("story barrier has not passed")
        changes.update(attempt_count=story.attempt_count + 1, last_attempt_at=now,
                       container_id=None, last_error=None)
    elif target == StoryState.PUBLISHED:
        if story.status == StoryState.AMBIGUOUS and reconciliation != "published":
            raise StoryStateError("ambiguous story requires reconciliation")
        if story.media_id and media_id != story.media_id:
            raise StoryStateError("known story media_id cannot be replaced")
        changes.update(media_id=media_id, published_at=now,
                       container_id=container_id or story.container_id)
    else:
        if story.status == StoryState.AMBIGUOUS:
            if reconciliation != "confirmed_not_published" or story.media_id:
                raise StoryStateError("ambiguous story requires conclusive reconciliation")
        classification = "ambiguous" if target == StoryState.AMBIGUOUS else "confirmed_not_published"
        if not phase:
            raise StoryStateError("story failure requires a phase")
        changes.update(last_error=StoryError(phase, classification, now),
                       container_id=container_id or story.container_id,
                       media_id=media_id or story.media_id)
    candidate = replace(story, **changes)
    return parse_story(story_json(candidate), slug=slug, package_sha256=package_sha256,
                       feed_published_at=feed_published_at)
