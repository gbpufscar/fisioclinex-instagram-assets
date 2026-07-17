"""Manual, durable and non-retrying publication orchestration."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .publication_state import authorize, begin_publishing, mark_failed, mark_published
from .publication_writeback import append_registry, persist, write_manifest
from .queue_pages import official_slide_url
from .registry import read_registry


class PublicationRunnerError(RuntimeError):
    def __init__(
        self,
        phase: str,
        *,
        run_id: str | None = None,
        publication_performed=None,
        git_operation: str | None = None,
        git_category: str | None = None,
    ):
        super().__init__(f"manual publication interrupted in phase {phase}")
        self.phase = phase
        self.run_id = run_id
        self.publication_performed = publication_performed
        self.git_operation = git_operation
        self.git_category = git_category


@dataclass(frozen=True, slots=True)
class PublicationResult:
    mode: str
    slug: str
    short_slug: str
    status: str
    slides_count: int
    asset_commit: str
    package_sha256: str
    publication_key: str
    pushed: bool
    verified: bool
    publication_performed: bool
    media_id: str
    published_at: str
    state_writeback: bool
    registry_writeback: bool

    def sanitized(self) -> dict:
        data = asdict(self)
        data["asset_commit"] = f"{self.asset_commit[:12]}…"
        data["package_sha256"] = f"{self.package_sha256[:12]}…"
        slug, digest = self.publication_key.rsplit(":", 1)
        data["publication_key"] = f"{slug}:{digest[:12]}…"
        return data


def _run_publication(
    repository_root: str | Path,
    *,
    mode: str,
    short_slug: str | None,
    confirmation: str | None,
    asset_commit: str,
    workflow_run_id: str,
    fetcher,
    meta_client,
    git_runner,
    now_fn=lambda: datetime.now(timezone.utc),
    run_id_factory=lambda: str(uuid.uuid4()),
    verifier=None,
) -> PublicationResult:
    root = Path(repository_root).resolve(strict=True)
    if not isinstance(workflow_run_id, str) or not workflow_run_id.isdigit():
        raise PublicationRunnerError("prepare", publication_performed=False)
    if verifier is None:
        try:
            from .shadow_runner import run_shadow_verified as verifier
        except ImportError:
            from github_actions.shadow_runner import run_shadow_verified as verifier
    verified = verifier(root, now=now_fn(), fetcher=fetcher)
    if not verified.selected or not verified.verified:
        raise PublicationRunnerError("prepare", publication_performed=False)
    if mode == "workflow_manual":
        try:
            authorize(short_slug, confirmation, verified.short_slug)
        except Exception:
            raise PublicationRunnerError(
                "authorization", publication_performed=False
            ) from None
    elif mode != "workflow_scheduled":
        raise PublicationRunnerError("authorization", publication_performed=False)
    manifest_path = (
        root / "publication-state" / "queue" / verified.slug / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    registry_path = root / "publication-state" / "publications.jsonl"
    if any(r.publication_key == verified.publication_key for r in read_registry(registry_path)):
        raise PublicationRunnerError("idempotency", publication_performed=False)

    run_id = run_id_factory()
    locked = begin_publishing(
        manifest,
        run_id=run_id,
        workflow_run_id=workflow_run_id,
        started_at=now_fn(),
        asset_commit=asset_commit,
    )
    write_manifest(manifest_path, locked)
    try:
        persist(
            root,
            paths=(manifest_path,),
            message=f"queue: iniciar publicação {verified.slug}",
            git_runner=git_runner,
        )
    except Exception as exc:
        write_manifest(manifest_path, manifest)
        raise PublicationRunnerError(
            "lock_push",
            run_id=run_id,
            publication_performed=False,
            git_operation=getattr(exc, "operation", None),
            git_category=getattr(exc, "category", None),
        ) from None

    children: list[str] = []
    carousel_id = None
    media_id = None
    urls = tuple(
        official_slide_url(
            verified.slug, f"{verified.slug}-slide-{number:02d}.png"
        )
        for number in range(1, verified.slides_count + 1)
    )
    caption = (
        root
        / "publication-state"
        / "queue"
        / verified.slug
        / "legenda.txt"
    ).read_text(encoding="utf-8")
    try:
        for url in urls:
            child = meta_client.create_child(url)
            children.append(child)
            meta_client.wait_finished(child)
        carousel_id = meta_client.create_carousel(tuple(children), caption)
        meta_client.wait_finished(carousel_id)
        media_id = meta_client.publish(carousel_id)
    except Exception as exc:
        phase = getattr(exc, "phase", "meta")
        failed = mark_failed(
            locked,
            phase=phase,
            failed_at=now_fn(),
            children=tuple(children),
            carousel_id=carousel_id,
            media_id=media_id,
        )
        write_manifest(manifest_path, failed)
        try:
            persist(
                root,
                paths=(manifest_path,),
                message=f"queue: registrar falha Meta {verified.slug}",
                git_runner=git_runner,
            )
        except Exception:
            pass
        raise PublicationRunnerError(phase, run_id=run_id) from None

    published_at = now_fn()
    completed = mark_published(locked, media_id=media_id, published_at=published_at)
    write_manifest(manifest_path, completed)
    record = {
        "schema_version": 1,
        "publication_key": verified.publication_key,
        "slug": verified.slug,
        "short_slug": verified.short_slug,
        "media_id": media_id,
        "published_at": published_at.isoformat(),
        "asset_commit": asset_commit,
        "package_sha256": verified.package_sha256,
        "slides_count": verified.slides_count,
        "publication_run_id": run_id,
        "workflow_run_id": workflow_run_id,
        "mode": mode,
    }
    append_registry(registry_path, record)
    try:
        persist(
            root,
            paths=(manifest_path, registry_path),
            message=f"queue: registrar publicação {verified.slug}",
            git_runner=git_runner,
        )
    except Exception:
        raise PublicationRunnerError(
            "writeback_after_publish", run_id=run_id, publication_performed=True
        ) from None
    return PublicationResult(
        mode=mode,
        slug=verified.slug,
        short_slug=verified.short_slug,
        status="published",
        slides_count=verified.slides_count,
        asset_commit=asset_commit,
        package_sha256=verified.package_sha256,
        publication_key=verified.publication_key,
        pushed=True,
        verified=True,
        publication_performed=True,
        media_id=media_id,
        published_at=published_at.isoformat(),
        state_writeback=True,
        registry_writeback=True,
    )


def result_json(result: PublicationResult) -> str:
    return json.dumps(result.sanitized(), ensure_ascii=False, sort_keys=True)


def run_manual_publication(repository_root: str | Path, **kwargs) -> PublicationResult:
    return _run_publication(
        repository_root,
        mode="workflow_manual",
        **kwargs,
    )


def run_scheduled_publication(repository_root: str | Path, **kwargs) -> PublicationResult:
    return _run_publication(
        repository_root,
        mode="workflow_scheduled",
        short_slug=None,
        confirmation=None,
        **kwargs,
    )
