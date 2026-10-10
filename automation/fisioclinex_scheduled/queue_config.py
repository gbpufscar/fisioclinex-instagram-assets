"""Strict public configuration with no secret or .env dependency."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_FIELDS = frozenset({"repository", "branch", "pages_base_url", "workspace_path"})
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SECRET_MARKERS = ("token", "secret", "password", "credential")


class QueueConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QueueConfig:
    repository: str
    branch: str
    pages_base_url: str
    workspace_path: Path


def load_queue_config(path: str | Path) -> QueueConfig:
    config_path = Path(path)
    if config_path.is_symlink() or not config_path.is_file():
        raise QueueConfigError("public configuration is unavailable")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QueueConfigError("public configuration is invalid") from exc
    if not isinstance(data, dict) or data.keys() != _FIELDS:
        raise QueueConfigError("public configuration fields are invalid")
    if any(marker in key.casefold() for key in data for marker in _SECRET_MARKERS):
        raise QueueConfigError("secret fields are forbidden")
    if not isinstance(data["repository"], str) or not _REPOSITORY.fullmatch(data["repository"]):
        raise QueueConfigError("repository is invalid")
    if (
        not isinstance(data["branch"], str)
        or not _BRANCH.fullmatch(data["branch"])
        or ".." in data["branch"]
        or data["branch"].endswith("/")
    ):
        raise QueueConfigError("branch is invalid")
    url = data["pages_base_url"]
    parsed = urlsplit(url) if isinstance(url, str) else None
    owner, repository = data["repository"].split("/", 1)
    expected_host = f"{owner.casefold()}.github.io"
    expected_path = f"/{repository}/"
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise QueueConfigError("pages_base_url is invalid")
    workspace = data["workspace_path"]
    if (
        not isinstance(workspace, str)
        or not workspace.startswith("~/")
        or ".." in Path(workspace).parts
        or any(marker in workspace.casefold() for marker in _SECRET_MARKERS)
    ):
        raise QueueConfigError("workspace_path is invalid")
    return QueueConfig(
        repository=data["repository"],
        branch=data["branch"],
        pages_base_url=url,
        workspace_path=Path(workspace).expanduser().resolve(strict=False),
    )
