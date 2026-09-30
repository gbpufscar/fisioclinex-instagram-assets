"""Queue states and the exact transition table frozen by the master plan."""

from __future__ import annotations

from enum import StrEnum

from .result import ResultCode, ScheduledResult


class QueueState(StrEnum):
    QUEUED = "queued"
    SELECTED = "selected"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    FAILED_BEFORE_META = "failed_before_meta"
    FAILED_AFTER_META = "failed_after_meta"
    NEEDS_REVIEW = "needs_review"


ALLOWED_TRANSITIONS: frozenset[tuple[QueueState | None, QueueState]] = frozenset(
    {
        (None, QueueState.QUEUED),
        (QueueState.QUEUED, QueueState.SELECTED),
        (QueueState.QUEUED, QueueState.PAUSED),
        (QueueState.QUEUED, QueueState.CANCELLED),
        (QueueState.SELECTED, QueueState.VERIFYING),
        (QueueState.SELECTED, QueueState.FAILED_BEFORE_META),
        (QueueState.SELECTED, QueueState.CANCELLED),
        (QueueState.VERIFYING, QueueState.VERIFIED),
        (QueueState.VERIFYING, QueueState.FAILED_BEFORE_META),
        (QueueState.VERIFYING, QueueState.CANCELLED),
        (QueueState.VERIFIED, QueueState.PUBLISHING),
        (QueueState.VERIFIED, QueueState.FAILED_BEFORE_META),
        (QueueState.VERIFIED, QueueState.CANCELLED),
        (QueueState.PUBLISHING, QueueState.PUBLISHED),
        (QueueState.PUBLISHING, QueueState.FAILED_AFTER_META),
        (QueueState.PUBLISHING, QueueState.NEEDS_REVIEW),
        (QueueState.FAILED_BEFORE_META, QueueState.SELECTED),
        (QueueState.FAILED_BEFORE_META, QueueState.CANCELLED),
        (QueueState.FAILED_AFTER_META, QueueState.NEEDS_REVIEW),
        (QueueState.PAUSED, QueueState.QUEUED),
        (QueueState.PAUSED, QueueState.CANCELLED),
    }
)

HUMAN_REVIEW_STATES = frozenset({QueueState.FAILED_AFTER_META, QueueState.NEEDS_REVIEW})

# Physical persistence below publication-state/queue is historical storage, not
# evidence that an item still participates in queue execution.  Keep this
# classification centralized so selection, validation and projection cannot
# silently diverge.
TERMINAL_OR_HISTORICAL_STATES = frozenset(
    {QueueState.PUBLISHED, QueueState.CANCELLED}
)
ACTIVE_QUEUE_STATES = frozenset(set(QueueState) - TERMINAL_OR_HISTORICAL_STATES)
SELECTABLE_QUEUE_STATES = frozenset({QueueState.QUEUED})


def is_active_queue_state(state: QueueState) -> bool:
    return state in ACTIVE_QUEUE_STATES


def is_terminal_state(state: QueueState) -> bool:
    return state in TERMINAL_OR_HISTORICAL_STATES


def is_selectable_queue_state(state: QueueState) -> bool:
    return state in SELECTABLE_QUEUE_STATES


class TransitionError(ValueError):
    """Raised when a queue transition is not part of the frozen state machine."""


def transition_allowed(source: QueueState | None, target: QueueState) -> bool:
    return (source, target) in ALLOWED_TRANSITIONS


def require_transition(source: QueueState | None, target: QueueState) -> None:
    if not transition_allowed(source, target):
        source_name = "new" if source is None else source.value
        raise TransitionError(f"transition not allowed: {source_name} -> {target.value}")


def transition_result(source: QueueState | None, target: QueueState) -> ScheduledResult:
    if transition_allowed(source, target):
        return ScheduledResult.success(
            ResultCode.ITEM_SELECTED,
            "transition allowed",
            status=target.value,
        )
    source_name = "new" if source is None else source.value
    return ScheduledResult.failure(
        ResultCode.INVALID_TRANSITION,
        f"transition not allowed: {source_name} -> {target.value}",
        status=source_name,
    )
