"""Pure, deterministic selection of at most one queued manifest."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .manifest import Manifest, ManifestValidationError, parse_manifest
from .result import ResultCode, ScheduledResult
from .states import QueueState


def select_next(
    manifests: Iterable[Manifest | Mapping[str, Any]],
    *,
    queue_enabled: bool,
    now: datetime,
    registered_publication_keys: Iterable[str] = (),
    consumed_slot_ids: Iterable[str] = (),
    slot_id: str | None = None,
) -> ScheduledResult:
    """Select at most one item without reading time, files, network, or state."""
    if now.tzinfo is None or now.utcoffset() is None:
        return ScheduledResult.failure(ResultCode.INVALID_MANIFEST, "now must include a timezone")

    candidates = list(manifests)
    candidate_count = len(candidates)
    if not queue_enabled:
        return ScheduledResult.success(
            ResultCode.QUEUE_DISABLED,
            "queue is disabled",
            candidate_count=candidate_count,
        )

    if slot_id is not None and slot_id in frozenset(consumed_slot_ids):
        return ScheduledResult.failure(
            ResultCode.DUPLICATE,
            "slot is already consumed",
            candidate_count=candidate_count,
        )

    parsed: list[Manifest] = []
    for candidate in candidates:
        if isinstance(candidate, Manifest):
            parsed.append(candidate)
            continue
        try:
            parsed.append(parse_manifest(candidate))
        except ManifestValidationError:
            return ScheduledResult.failure(
                ResultCode.INVALID_MANIFEST,
                "manifest validation failed",
                candidate_count=candidate_count,
            )

    registered = frozenset(registered_publication_keys)
    eligible = [
        manifest
        for manifest in parsed
        if manifest.status is QueueState.QUEUED
        and (manifest.not_before is None or manifest.not_before <= now)
        and manifest.publication.media_id is None
        and manifest.publication_key not in registered
    ]
    eligible.sort(key=lambda item: (item.priority, item.queued_at, item.slug))

    if not eligible:
        return ScheduledResult.success(
            ResultCode.QUEUE_EMPTY,
            "no eligible queued item",
            candidate_count=candidate_count,
            eligible_count=0,
        )

    selected = eligible[0]
    return ScheduledResult.success(
        ResultCode.ITEM_SELECTED,
        "one queued item selected",
        slug=selected.slug,
        short_slug=selected.short_slug,
        status=selected.status.value,
        publication_key=selected.publication_key,
        candidate_count=candidate_count,
        eligible_count=len(eligible),
    )
