"""Exact-path Git writeback for durable publication state."""

from __future__ import annotations

import json
from pathlib import Path


class WritebackError(RuntimeError):
    pass


def write_manifest(path: Path, data: dict) -> None:
    if path.name != "manifest.json" or path.is_symlink():
        raise WritebackError("manifest path is invalid")
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_registry(path: Path, record: dict) -> None:
    if path.name != "publications.jsonl" or path.is_symlink():
        raise WritebackError("registry path is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


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
        or message.startswith("queue: registrar falha Meta fisioclinex-")
        or message.startswith("queue: registrar publicação fisioclinex-")
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
