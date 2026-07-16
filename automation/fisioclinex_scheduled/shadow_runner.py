"""Read-only queue inspection and deterministic shadow selection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from fisioclinex_scheduled.fingerprint import fingerprint_mapped_files
from fisioclinex_scheduled.manifest import Manifest, parse_manifest
from fisioclinex_scheduled.queue_pages import QueuePagesError, verify_slide_paths
from fisioclinex_scheduled.registry import read_registry
from fisioclinex_scheduled.result import ResultCode
from fisioclinex_scheduled.selector import select_next

from .queue_control import load_queue_control


class ShadowRunnerError(RuntimeError):
    pass


class ShadowVerificationError(ShadowRunnerError):
    def __init__(self, report: "VerifiedShadowReport"):
        super().__init__("GitHub Pages verification failed")
        self.report = report


@dataclass(frozen=True, slots=True)
class ShadowReport:
    mode: str
    queue_enabled: bool
    scanned_count: int
    valid_count: int
    eligible_count: int
    selected: bool
    reason: str
    slug: str | None = None
    short_slug: str | None = None
    status: str | None = None
    priority: int | None = None
    not_before: str | None = None
    slides_count: int | None = None
    package_sha256: str | None = None
    publication_key: str | None = None

    def sanitized(self) -> dict[str, object]:
        data = asdict(self)
        if self.package_sha256:
            data["package_sha256"] = f"{self.package_sha256[:12]}…"
        if self.publication_key:
            slug, digest = self.publication_key.rsplit(":", 1)
            data["publication_key"] = f"{slug}:{digest[:12]}…"
        return data


@dataclass(frozen=True, slots=True)
class VerifiedShadowReport:
    mode: str
    queue_enabled: bool
    scanned_count: int
    eligible_count: int
    selected: bool
    slug: str | None
    short_slug: str | None
    status_preserved: str | None
    slides_count: int
    verified_slides: int
    package_sha256: str | None
    publication_key: str | None
    pushed: bool
    verified: bool
    publication_performed: bool
    phase: str | None = None

    def sanitized(self) -> dict[str, object]:
        data = asdict(self)
        if self.package_sha256:
            data["package_sha256"] = f"{self.package_sha256[:12]}…"
        if self.publication_key:
            slug, digest = self.publication_key.rsplit(":", 1)
            data["publication_key"] = f"{slug}:{digest[:12]}…"
        return data


def _manifest_paths(root: Path) -> tuple[Path, ...]:
    queue_root = root / "publication-state" / "queue"
    if not queue_root.exists():
        return ()
    if queue_root.is_symlink() or not queue_root.is_dir():
        raise ShadowRunnerError("queue directory is invalid")
    paths = tuple(sorted(queue_root.glob("*/manifest.json")))
    for path in paths:
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise ShadowRunnerError("queue manifest path is invalid")
    return paths


def _validate_package(root: Path, manifest_path: Path, manifest: Manifest) -> None:
    if manifest_path.parent.name != manifest.slug:
        raise ShadowRunnerError("manifest directory does not match slug")
    caption = manifest_path.parent / manifest.caption_file
    posts = root / "posts" / manifest.slug
    if caption.is_symlink() or not caption.is_file():
        raise ShadowRunnerError("queue caption is missing")
    if posts.is_symlink() or not posts.is_dir():
        raise ShadowRunnerError("post slide directory is missing")
    expected_names = tuple(
        f"{manifest.slug}-slide-{number:02d}.png"
        for number in range(1, manifest.slides_count + 1)
    )
    entries = tuple(posts.iterdir())
    if any(entry.is_symlink() for entry in entries):
        raise ShadowRunnerError("post slide symlink is forbidden")
    if {entry.name for entry in entries if entry.is_file()} != set(expected_names):
        raise ShadowRunnerError("post slide set is invalid")
    if any(not entry.is_file() for entry in entries):
        raise ShadowRunnerError("post slide directory contains invalid entries")
    mapped = {"legenda.txt": caption}
    mapped.update({name: posts / name for name in expected_names})
    if fingerprint_mapped_files(mapped) != manifest.package_sha256:
        raise ShadowRunnerError("package fingerprint differs")


def run_shadow(
    repository_root: str | Path,
    *,
    now: datetime,
    step_summary_path: str | Path | None = None,
) -> ShadowReport:
    root = Path(repository_root)
    if root.is_symlink() or not root.is_dir():
        raise ShadowRunnerError("repository root is invalid")
    root = root.resolve(strict=True)
    summary_path = Path(step_summary_path).expanduser() if step_summary_path is not None else None
    if summary_path is not None:
        resolved_summary = summary_path.resolve(strict=False)
        if resolved_summary.is_relative_to(root) or summary_path.is_symlink():
            raise ShadowRunnerError("step summary path is unsafe")
    control = load_queue_control(root / "publication-state" / "queue-control.json")
    if not control.queue_enabled:
        report = ShadowReport(
            mode="shadow",
            queue_enabled=False,
            scanned_count=0,
            valid_count=0,
            eligible_count=0,
            selected=False,
            reason="queue is disabled",
        )
        if summary_path is not None:
            _write_summary(summary_path, report)
        return report
    paths = _manifest_paths(root)
    manifests: list[Manifest] = []
    for path in paths:
        try:
            manifest = parse_manifest(path.read_bytes())
            _validate_package(root, path, manifest)
        except (OSError, UnicodeError, ValueError, ShadowRunnerError) as exc:
            raise ShadowRunnerError("queue item validation failed") from exc
        manifests.append(manifest)
    try:
        registry = read_registry(root / "publication-state" / "publications.jsonl")
    except ValueError as exc:
        raise ShadowRunnerError("publication history is invalid") from exc
    selection = select_next(
        manifests,
        queue_enabled=control.queue_enabled,
        now=now,
        registered_publication_keys=(record.publication_key for record in registry),
    )
    if not selection.ok:
        raise ShadowRunnerError(selection.reason)
    selected_manifest = next(
        (item for item in manifests if item.publication_key == selection.publication_key),
        None,
    )
    report = ShadowReport(
        mode="shadow",
        queue_enabled=control.queue_enabled,
        scanned_count=len(paths),
        valid_count=len(manifests),
        eligible_count=selection.eligible_count,
        selected=selection.code is ResultCode.ITEM_SELECTED,
        reason=selection.reason,
        slug=selection.slug,
        short_slug=selection.short_slug,
        status=selection.status,
        priority=selected_manifest.priority if selected_manifest else None,
        not_before=(
            selected_manifest.not_before.isoformat()
            if selected_manifest and selected_manifest.not_before
            else None
        ),
        slides_count=selected_manifest.slides_count if selected_manifest else None,
        package_sha256=selected_manifest.package_sha256 if selected_manifest else None,
        publication_key=selected_manifest.publication_key if selected_manifest else None,
    )
    if summary_path is not None:
        _write_summary(summary_path, report)
    return report


def _write_summary(path: Path, report: ShadowReport) -> None:
    lines = [
        "# FisioClinEx — Fila sombra",
        "",
        "**MODO SOMBRA — NENHUMA PUBLICAÇÃO FOI REALIZADA**",
        "",
        f"- Itens examinados: {report.scanned_count}",
        f"- Itens elegíveis: {report.eligible_count}",
        f"- Seleção: {'sim' if report.selected else 'não'}",
    ]
    if report.selected:
        lines.extend(
            [
                f"- Slug: `{report.slug}`",
                f"- Status preservado: `{report.status}`",
                f"- Slides: {report.slides_count}",
                f"- Fingerprint: `{report.package_sha256[:12]}…`",
            ]
        )
    else:
        lines.append(f"- Motivo: {report.reason}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def report_json(report: ShadowReport) -> str:
    return json.dumps(report.sanitized(), ensure_ascii=False, sort_keys=True)


def run_shadow_verified(
    repository_root: str | Path,
    *,
    now: datetime,
    fetcher,
    step_summary_path: str | Path | None = None,
    max_wait_seconds: float = 120.0,
    retry_interval_seconds: float = 5.0,
    sleeper=None,
    monotonic=None,
) -> VerifiedShadowReport:
    selection = run_shadow(repository_root, now=now)
    base = {
        "mode": "shadow_verified",
        "queue_enabled": selection.queue_enabled,
        "scanned_count": selection.scanned_count,
        "eligible_count": selection.eligible_count,
        "selected": selection.selected,
        "slug": selection.slug,
        "short_slug": selection.short_slug,
        "status_preserved": selection.status,
        "slides_count": selection.slides_count or 0,
        "package_sha256": selection.package_sha256,
        "publication_key": selection.publication_key,
        "pushed": selection.selected,
        "publication_performed": False,
    }
    if not selection.selected:
        report = VerifiedShadowReport(
            **base,
            verified_slides=0,
            verified=False,
        )
        if step_summary_path is not None:
            _write_verified_summary(Path(step_summary_path), Path(repository_root), report)
        return report

    root = Path(repository_root).resolve(strict=True)
    slides = tuple(
        root / "posts" / selection.slug / f"{selection.slug}-slide-{number:02d}.png"
        for number in range(1, selection.slides_count + 1)
    )
    kwargs = {
        "fetcher": fetcher,
        "max_wait_seconds": max_wait_seconds,
        "retry_interval_seconds": retry_interval_seconds,
    }
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    try:
        verified_slides = verify_slide_paths(selection.slug, slides, **kwargs)
    except QueuePagesError:
        report = VerifiedShadowReport(
            **base,
            verified_slides=0,
            verified=False,
            phase="pages_verification",
        )
        if step_summary_path is not None:
            _write_verified_summary(Path(step_summary_path), root, report)
        raise ShadowVerificationError(report) from None
    report = VerifiedShadowReport(
        **base,
        verified_slides=verified_slides,
        verified=True,
    )
    if step_summary_path is not None:
        _write_verified_summary(Path(step_summary_path), root, report)
    return report


def _write_verified_summary(path: Path, root: Path, report: VerifiedShadowReport) -> None:
    resolved = path.expanduser().resolve(strict=False)
    if resolved.is_relative_to(root) or path.is_symlink():
        raise ShadowRunnerError("step summary path is unsafe")
    lines = [
        "# FisioClinEx — Fila sombra verificada",
        "",
        "**MODO SOMBRA — NENHUMA PUBLICAÇÃO FOI REALIZADA**",
        "",
        f"- Itens examinados: {report.scanned_count}",
        f"- Itens elegíveis: {report.eligible_count}",
        f"- Slug selecionada: `{report.slug or 'nenhuma'}`",
        f"- Status preservado: `{report.status_preserved or 'n/a'}`",
        f"- Slides esperados: {report.slides_count}",
        f"- Slides verificados: {report.verified_slides}",
        f"- GitHub Pages verificado: {'sim' if report.verified else 'não'}",
        f"- pushed: `{str(report.pushed).lower()}`",
        f"- verified: `{str(report.verified).lower()}`",
        f"- Fingerprint: `{report.package_sha256[:12]}…`"
        if report.package_sha256
        else "- Fingerprint: `n/a`",
        "- Publicação realizada: não",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verified_report_json(report: VerifiedShadowReport) -> str:
    return json.dumps(report.sanitized(), ensure_ascii=False, sort_keys=True)
