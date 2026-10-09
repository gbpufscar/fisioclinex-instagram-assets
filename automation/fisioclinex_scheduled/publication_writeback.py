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
            lines = path.read_text(encoding="utf-8").splitlines()
            updated = False
            for index, line in enumerate(lines):
                old = json.loads(line)
                if old["publication_key"] != record["publication_key"]:
                    continue
                for field in ("story_media_id", "story_published_at"):
                    if record.get(field) is not None and field in old:
                        if old[field] is not None and old[field] != record[field]:
                            raise RegistryError("confirmed Story result cannot be replaced")
                        if old[field] is None:
                            old[field] = record[field]; updated = True
                lines[index] = json.dumps(old, sort_keys=True, separators=(",", ":"))
            if updated:
                atomic_write(path, "\n".join(lines) + "\n")
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


def persist_feed(root: Path, manifest_path: Path, data: dict, *, git_runner, record=None, mode="workflow_scheduled") -> None:
    """One logical post-feed writeback; durable evidence precedes projection.

    A failure never rolls back a confirmed feed. Reconciliation repairs only
    documentation, never retries Meta. Individual files are crash-safe.
    """
    from .manifest import parse_manifest
    from .schedule_integrity import schedule_lock
    from .publication_consistency import project_confirmed_feed
    manifest = parse_manifest(data)
    if not manifest.publication.media_id or not manifest.publication.published_at:
        raise WritebackError("feed confirmation is missing")
    if record is None:
        record = {
            "publication_key": manifest.publication_key, "slug": manifest.slug,
            "media_id": manifest.publication.media_id,
            "published_at": manifest.publication.published_at.isoformat(),
        }
    if record.keys() == {"publication_key", "slug", "media_id", "published_at"} and data.get("publication_run_id"):
        record.update(schema_version=1, short_slug=manifest.short_slug,
                      asset_commit=manifest.publication.asset_commit,
                      package_sha256=manifest.package_sha256, slides_count=manifest.slides_count,
                      publication_run_id=data["publication_run_id"],
                      workflow_run_id=manifest.publication.workflow_run_id, mode=mode)
        if manifest.slot_id is not None:
            record.update(story_media_id=data.get("story_media_id"), story_published_at=data.get("story_published_at"),
                          slot_id=manifest.slot_id, planned_at=manifest.planned_at.isoformat(),
                          slot_type=manifest.slot_type, assigned_slug=manifest.slug,
                          queued_at=manifest.queued_at.isoformat(), slot_status="published")
    write_manifest(manifest_path, data)
    registry_path = root / "publication-state/publications.jsonl"
    # The root lock protects the editorial projection against reschedule writes.
    # Avoid nesting write_manifest's root flock.
    with schedule_lock(root):
        append_registry(registry_path, record)
        calendar_path = project_confirmed_feed(root, manifest)
    paths = (manifest_path, registry_path) + ((calendar_path,) if calendar_path else ())
    persist(root, paths=paths,
            message=f"queue: registrar feed {manifest.slug}", git_runner=git_runner)


def verify_remote(root, paths, *, git_runner, branch="main"):
    """Fetch remote, require commit ancestry and equal exact-path Git blobs.

    A concurrent unrelated descendant is allowed. Any changed operational path
    fails; no overwrite/rebase/automatic retry is attempted.
    """
    import re
    head = git_runner(("rev-parse", "HEAD"))
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise WritebackError("remote_verification_failed: invalid_head")
    git_runner(("fetch", "origin", branch))
    git_runner(("merge-base", "--is-ancestor", head, f"origin/{branch}"))
    for path in paths:
        relative = Path(path).resolve(strict=True).relative_to(Path(root).resolve(strict=True)).as_posix()
        local = git_runner(("rev-parse", f"{head}:{relative}"))
        remote = git_runner(("rev-parse", f"origin/{branch}:{relative}"))
        if not re.fullmatch(r"[0-9a-f]{40}", local or "") or remote != local:
            raise WritebackError(f"remote_verification_failed: {relative}")
    return head


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
            or relative in {"publication-state/publications.jsonl", "publication-state/editorial-calendar.json"}
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
    verify_remote(root, paths, git_runner=git_runner)
    return result
