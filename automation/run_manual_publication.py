#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fisioclinex_scheduled.meta_client import MetaClient, MetaResponse
from fisioclinex_scheduled.publication_runner import (
    PublicationRunnerError,
    result_json,
    run_manual_publication,
)
from fisioclinex_scheduled.publication_state import authorize
from fisioclinex_scheduled.queue_pages import PageResponse
from fisioclinex_scheduled.shadow_runner import run_shadow_verified, verified_report_json


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request: Request, timeout: int):
    return build_opener(_NoRedirect).open(request, timeout=timeout)


def _pages_fetch(url: str) -> PageResponse:
    request = Request(url, headers={"User-Agent": "FisioClinEx-Manual-Pages/1.0"})
    try:
        with _open(request, 15) as response:
            return PageResponse(
                url,
                response.geturl(),
                response.status,
                response.headers.get("Content-Type", ""),
                response.read(50 * 1024 * 1024 + 1),
            )
    except HTTPError as exc:
        return PageResponse(
            url,
            exc.geturl(),
            exc.code,
            exc.headers.get("Content-Type", "") if exc.headers else "",
            exc.read(50 * 1024 * 1024 + 1),
        )


def _meta_transport(method: str, url: str, headers: dict, body: bytes, timeout: int):
    request = Request(url, data=body if method == "POST" else None, headers=headers, method=method)
    try:
        with _open(request, timeout) as response:
            return MetaResponse(response.status, response.geturl(), response.read(1024 * 1024 + 1))
    except HTTPError as exc:
        return MetaResponse(exc.code, exc.geturl(), exc.read(1024 * 1024 + 1))


def _git_runner(root: Path):
    allowed = {"add", "commit", "push"}

    def run(args):
        if not args or args[0] not in allowed:
            raise RuntimeError("Git command is not allowlisted")
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        if completed.returncode != 0:
            raise RuntimeError("Git writeback failed")
        return completed.stdout.strip()

    return run


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required configuration is missing: {name}")
    return value


def _validate_github_context() -> None:
    if (
        _required("GITHUB_REPOSITORY") != "gbpufscar/fisioclinex-instagram-assets"
        or _required("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or _required("GITHUB_REF") != "refs/heads/main"
        or not re.fullmatch(r"[0-9a-f]{40}", _required("GITHUB_SHA"))
        or not _required("GITHUB_RUN_ID").isdigit()
    ):
        raise RuntimeError("GitHub execution context is invalid")


def _summary(lines: list[str]) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--simulate", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repository-root")
    parser.add_argument("--short-slug", required=True)
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args(argv)
    root = Path(args.repository_root or Path(__file__).resolve().parents[1]).resolve()
    if args.prepare:
        report = run_shadow_verified(root, now=datetime.now(timezone.utc), fetcher=_pages_fetch)
        authorize(args.short_slug, args.confirmation, report.short_slug)
        print(verified_report_json(report))
        return 0
    if args.simulate:
        raise RuntimeError("simulation is available only through injected test collaborators")
    _validate_github_context()
    client = MetaClient(
        _required("INSTAGRAM_ACCESS_TOKEN"),
        _required("INSTAGRAM_BUSINESS_ID"),
        _required("META_API_VERSION"),
        transport=_meta_transport,
    )
    try:
        result = run_manual_publication(
            root,
            short_slug=args.short_slug,
            confirmation=args.confirmation,
            asset_commit=_required("GITHUB_SHA"),
            workflow_run_id=_required("GITHUB_RUN_ID"),
            fetcher=_pages_fetch,
            meta_client=client,
            git_runner=_git_runner(root),
        )
    except PublicationRunnerError as exc:
        _summary(
            [
                "# FisioClinEx — Publicação manual da fila",
                "",
                "**PUBLICAÇÃO INTERROMPIDA — NÃO REPETIR AUTOMATICAMENTE**",
                "",
                f"- Fase: `{exc.phase}`",
                f"- Run ID: `{exc.run_id or 'indisponível'}`",
                "- Revisão humana: necessária",
                "- Publicação realizada: desconhecida",
            ]
        )
        print(json.dumps({"phase": exc.phase, "run_id": exc.run_id, "status": "interrupted"}))
        return 1
    _summary(
        [
            "# FisioClinEx — Publicação manual da fila",
            "",
            "**PUBLICAÇÃO REAL CONCLUÍDA**",
            "",
            f"- Slug: `{result.slug}`",
            f"- Slides: {result.slides_count}/{result.slides_count}",
            "- Pages verificado: sim",
            "- Bloqueio persistido: sim",
            "- Meta concluída: sim",
            f"- Media ID: `{result.media_id}`",
            "- Status: `published`",
            "- Histórico registrado: sim",
            "- Publicação realizada: sim",
        ]
    )
    print(result_json(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
