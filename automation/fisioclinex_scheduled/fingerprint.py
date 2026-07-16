"""Cross-platform package fingerprinting for explicitly listed approved files."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SLUG_RE = re.compile(r"^fisioclinex-[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAGIC = b"FISIOCLINEX-PACKAGE-SHA256\x00v1\x00"


class FingerprintError(ValueError):
    """Raised when an approved package cannot be fingerprinted safely."""


def _length_prefix(value: int) -> bytes:
    return value.to_bytes(8, byteorder="big", signed=False)


def _normalize_relative_path(value: str | Path) -> PurePosixPath:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw:
        raise FingerprintError("relative path is invalid")
    if "\\" in raw:
        raise FingerprintError("relative path must use POSIX separators")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise FingerprintError("relative path escapes or is not normalized")
    return path


def fingerprint_mapped_files(files: Mapping[str | Path, str | Path]) -> str:
    """Hash canonical relative names mapped to explicit regular files."""
    if not isinstance(files, Mapping) or not files:
        raise FingerprintError("approved file mapping cannot be empty")
    normalized = [(_normalize_relative_path(name), Path(path)) for name, path in files.items()]
    names = [name.as_posix() for name, _ in normalized]
    if len(names) != len(set(names)):
        raise FingerprintError("duplicate relative path")
    if names.count("legenda.txt") != 1:
        raise FingerprintError("approved file list must contain legenda.txt exactly once")
    slide_names = [name for name in names if name != "legenda.txt"]
    if not slide_names or any(PurePosixPath(name).suffix.lower() != ".png" for name in slide_names):
        raise FingerprintError("approved file list must contain PNG slides")

    digest = hashlib.sha256()
    digest.update(_MAGIC)
    for relative, candidate in sorted(
        normalized, key=lambda item: item[0].as_posix().encode("utf-8")
    ):
        if candidate.is_symlink():
            raise FingerprintError("symlinks are not allowed")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise FingerprintError("approved file is missing") from exc
        if not resolved.is_file():
            raise FingerprintError("approved path is not a regular file")
        name_bytes = relative.as_posix().encode("utf-8")
        size = resolved.stat().st_size
        digest.update(_length_prefix(len(name_bytes)))
        digest.update(name_bytes)
        digest.update(_length_prefix(size))
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def fingerprint_package(root: str | Path, relative_paths: Iterable[str | Path]) -> str:
    """Hash approved files using a documented, platform-independent binary stream.

    Stream format:
      magic bytes;
      for each normalized POSIX path in UTF-8 byte-order:
        8-byte unsigned big-endian path length;
        path UTF-8 bytes;
        8-byte unsigned big-endian file size;
        file content bytes.

    Only the explicitly supplied files are opened. Input order does not affect the
    digest because normalized paths are sorted before hashing.
    """
    root_path = Path(root)
    if root_path.is_symlink() or not root_path.is_dir():
        raise FingerprintError("package root must be an existing non-symlink directory")
    root_resolved = root_path.resolve(strict=True)

    normalized = [_normalize_relative_path(path) for path in relative_paths]
    names = [path.as_posix() for path in normalized]
    if len(names) != len(set(names)):
        raise FingerprintError("duplicate relative path")
    if not names:
        raise FingerprintError("approved file list cannot be empty")
    if names.count("legenda.txt") != 1:
        raise FingerprintError("approved file list must contain legenda.txt exactly once")
    slide_names = [name for name in names if name != "legenda.txt"]
    if not slide_names or any(PurePosixPath(name).suffix.lower() != ".png" for name in slide_names):
        raise FingerprintError("approved file list must contain PNG slides")

    mapped: dict[PurePosixPath, Path] = {}
    for relative in normalized:
        candidate = root_resolved.joinpath(*relative.parts)
        if candidate.is_symlink():
            raise FingerprintError("symlinks are not allowed")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise FingerprintError("approved file is missing") from exc
        if not resolved.is_relative_to(root_resolved):
            raise FingerprintError("approved file escapes package root")
        mapped[relative] = resolved
    return fingerprint_mapped_files(mapped)


def build_publication_key(slug: str, package_sha256: str) -> str:
    """Return the unambiguous canonical key ``<slug>:<lowercase sha256>``."""
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise FingerprintError("slug is invalid")
    if not isinstance(package_sha256, str) or not _SHA256_RE.fullmatch(package_sha256):
        raise FingerprintError("package_sha256 is invalid")
    return f"{slug}:{package_sha256}"
