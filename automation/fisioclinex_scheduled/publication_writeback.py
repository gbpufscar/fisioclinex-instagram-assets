"""Exact-path Git writeback for durable publication state."""

from __future__ import annotations

import json
import os
import tempfile
from functools import wraps
from datetime import datetime
from pathlib import Path


class WritebackError(RuntimeError):
    pass


class GitWritebackError(WritebackError):
    def __init__(self, operation: str, category: str):
        super().__init__(f"git_writeback_failed operation={operation} category={category}")
        self.operation = operation
        self.category = category


def classify_git_failure(operation: str, stderr: str) -> str:
    value = stderr.casefold()
    if any(
        marker in value
        for marker in (
            "author identity unknown",
            "please tell me who you are",
            "unable to auto-detect email address",
        )
    ):
        return "identity_missing"
    if "nothing to commit" in value:
        return "nothing_to_commit"
    if operation == "push" and any(
        marker in value for marker in ("non-fast-forward", "fetch first")
    ):
        return "non_fast_forward"
    if any(
        marker in value
        for marker in (
            "authentication failed",
            "could not read username",
            "permission denied",
            "http 401",
            "http 403",
        )
    ):
        return "authentication_failed"
    if operation == "push" and any(
        marker in value for marker in ("remote rejected", "failed to push some refs")
    ):
        return "push_rejected"
    return "git_operation_failed"


def write_manifest(path: Path, data: dict) -> None:
    if path.name != "manifest.json" or path.is_symlink() or path.parent.is_symlink():
        raise WritebackError("manifest path is invalid")
    from .schedule_integrity import schedule_lock, validate_schedule_write
    from .manifest import parse_manifest
    with schedule_lock(path.parents[3]):
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if previous and previous.get("story_plan_version") == 1 and data.get("story_plan_version") != 1:
            raise WritebackError("Story planning cannot be removed independently from its feed")
        if "story_plan_version" in data:
            parse_manifest(data)  # Validate child-only writes too, not just feed dates.
        schedule_fields = ("planned_at", "not_before", "slot_id", "slug")
        changed = previous is None or any(previous.get(key) != data.get(key) for key in schedule_fields)
        if changed:
            manifest = parse_manifest(data)
            planned = manifest.planned_at or manifest.not_before
            if manifest.status.value == "queued" and (manifest.planned_at is None or manifest.slot_id is None):
                raise WritebackError("new or changed queued items require an explicit editorial slot")
            if planned is not None:
                validate_schedule_write(path.parents[3], slug=manifest.slug, planned_at=planned,
                                        explicit_override=manifest.explicit_override,
                                        override_reason=manifest.override_reason)
        _write_manifest(path, data)


def _write_manifest(path: Path, data: dict) -> None:
    if (path.name != "manifest.json" or path.is_symlink()
            or any(parent.is_symlink() for parent in path.parents[:3])):
        raise WritebackError("manifest path is invalid")
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def atomic_write(path: Path, text: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise WritebackError("unsafe state path")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".publication-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def execution_locked(function):
    """One local publication worker per repository; Actions also uses Git fencing.

    A separate resource avoids nesting the write-side schedule lock.
    """
    @wraps(function)
    def locked(repository_root, *args, **kwargs):
        from .schedule_integrity import schedule_lock
        with schedule_lock(Path(repository_root) / ".publication-execution"):
            return function(repository_root, *args, **kwargs)
    return locked


def append_registry(path: Path, record: dict) -> None:
    from .schedule_integrity import schedule_lock
    from .registry import _read_records, RegistryError
    if path.name != "publications.jsonl" or path.is_symlink():
        raise WritebackError("registry path is invalid")
    with schedule_lock(path):
        existing = _read_records(path)
        same = next((r for r in existing if r.publication_key == record["publication_key"]), None)
        if same is not None:
            if same.media_id != record["media_id"] or datetime.fromisoformat(same.published_at.replace("Z", "+00:00")) != datetime.fromisoformat(record["published_at"].replace("Z", "+00:00")):
                raise RegistryError("confirmed registry result cannot be replaced")
            return
        if any(r.media_id == record["media_id"] for r in existing):
            raise RegistryError("duplicate media_id")
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        payload = previous + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        # Validate the complete replacement before exposing it to readers.
        fd, name = tempfile.mkstemp(dir=path.parent if path.parent.exists() else None)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            _read_records(Path(name))
        finally:
            os.unlink(name)
        atomic_write(path, payload)


def persist_feed(root: Path, manifest_path: Path, data: dict, *, git_runner) -> None:
    """Write manifest first, then registry; interruptions keep feed non-repostable.

    Files are individually atomic. They are not a two-file transaction: a crash
    between them requires operator writeback repair, never a new Meta call.
    """
    from .manifest import parse_manifest
    manifest = parse_manifest(data)
    if not manifest.publication.media_id or not manifest.publication.published_at:
        raise WritebackError("feed confirmation is missing")
    record = {
        "publication_key": manifest.publication_key, "slug": manifest.slug,
        "media_id": manifest.publication.media_id,
        "published_at": manifest.publication.published_at.isoformat(),
    }
    write_manifest(manifest_path, data)
    registry_path = root / "publication-state/publications.jsonl"
    append_registry(registry_path, record)
    persist(root, paths=(manifest_path, registry_path),
            message=f"queue: registrar feed {manifest.slug}", git_runner=git_runner)


def persist(
    repository_root: Path,
    *,
    paths: tuple[Path, ...],
    message: str,
    git_runner,
) -> str:
    root = repository_root.resolve(strict=True)
    relatives = []
    for path in paths:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise WritebackError("writeback path escapes repository")
        relative = resolved.relative_to(root).as_posix()
        if not (
            relative.startswith("publication-state/queue/")
            or relative == "publication-state/publications.jsonl"
        ):
            raise WritebackError("writeback path is not allowlisted")
        relatives.append(relative)
    if not (
        message.startswith("queue: iniciar publicação fisioclinex-")
        or message.startswith("queue: registrar feed fisioclinex-")
        or message.startswith("queue: registrar falha Meta fisioclinex-")
        or message.startswith("queue: registrar publicação fisioclinex-")
        or message.startswith("queue: registrar Story fisioclinex-")
        or message.startswith("queue: registrar falha Story fisioclinex-")
    ):
        raise WritebackError("commit message is invalid")
    for args in (
        ("add", "--", *relatives),
        ("commit", "-m", message),
        ("push", "origin", "HEAD:main"),
    ):
        result = git_runner(args)
        if not isinstance(result, str):
            raise WritebackError("Git writeback failed")
    return result
