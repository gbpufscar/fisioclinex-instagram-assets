#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fisioclinex_scheduled.meta_client import MetaClient, MetaResponse
from fisioclinex_scheduled.publication_runner import (
    PublicationRunnerError,
    result_json,
    run_scheduled_publication,
)
from fisioclinex_scheduled.publication_writeback import (
    GitWritebackError,
    classify_git_failure,
)
from fisioclinex_scheduled.queue_pages import PageResponse
from fisioclinex_scheduled.schedule_control import load_schedule_control
from fisioclinex_scheduled.shadow_runner import run_shadow_verified


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request: Request, timeout: int):
    return build_opener(_NoRedirect).open(request, timeout=timeout)


def _pages_fetch(url: str) -> PageResponse:
    request = Request(url, headers={"User-Agent": "FisioClinEx-Scheduled-Pages/1.0"})
    try:
        with _open(request, 15) as response:
            return PageResponse(
                url, response.geturl(), response.status,
                response.headers.get("Content-Type", ""),
                response.read(50 * 1024 * 1024 + 1),
            )
    except HTTPError as exc:
        return PageResponse(
            url, exc.geturl(), exc.code,
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
            ["git", *args], cwd=root, check=False, capture_output=True, text=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "github-actions[bot]",
                "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
                "GIT_COMMITTER_NAME": "github-actions[bot]",
                "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            },
        )
        if completed.returncode != 0:
            raise GitWritebackError(args[0], classify_git_failure(args[0], completed.stderr))
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
        or _required("GITHUB_EVENT_NAME") != "schedule"
        or _required("GITHUB_REF") != "refs/heads/main"
        or not re.fullmatch(r"[0-9a-f]{40}", _required("GITHUB_SHA"))
        or not _required("GITHUB_RUN_ID").isdigit()
    ):
        raise RuntimeError("GitHub execution context is invalid")


def _write(path_name: str, lines: list[str]) -> None:
    target = os.environ.get(path_name)
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _prepare(root: Path) -> int:
    control = load_schedule_control(root / "publication-state" / "schedule-control.json")
    if not control.scheduled_publication_enabled:
        _write("GITHUB_OUTPUT", ["selected=false"])
        _write("GITHUB_STEP_SUMMARY", [
            "# FisioClinEx — Publicação agendada da fila", "",
            "**AGENDAMENTO DESABILITADO**", "",
            "- Publicação realizada: não", "- Meta iniciada: não",
        ])
        print(json.dumps({"reason": "schedule_disabled", "selected": False}))
        return 0
    report = run_shadow_verified(root, now=datetime.now(timezone.utc), fetcher=_pages_fetch)
    _write("GITHUB_OUTPUT", [f"selected={'true' if report.selected else 'false'}"])
    if not report.selected:
        _write("GITHUB_STEP_SUMMARY", [
            "# FisioClinEx — Publicação agendada da fila", "",
            "**NENHUM POST ELEGÍVEL**", "",
            f"- Itens examinados: {report.scanned_count}",
            "- Itens elegíveis: 0", "- Publicação realizada: não",
        ])
    else:
        _write("GITHUB_STEP_SUMMARY", [
            "# FisioClinEx — Publicação agendada da fila", "",
            "**POST AGENDADO VALIDADO**", "", f"- Slug: `{report.slug}`",
            "- Status preservado: `queued`", f"- Slides: {report.slides_count}",
            "- Pages verificado: sim", "- pushed: `true`", "- verified: `true`",
            "- Publicação realizada: não",
        ])
    print(json.dumps({"selected": report.selected, "verified": report.verified}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--repository-root")
    args = parser.parse_args(argv)
    root = Path(args.repository_root or Path(__file__).resolve().parents[1]).resolve()
    if args.prepare:
        return _prepare(root)
    _validate_github_context()
    control = load_schedule_control(root / "publication-state" / "schedule-control.json")
    if not control.scheduled_publication_enabled:
        raise RuntimeError("scheduled publication is disabled")
    client = MetaClient(
        _required("INSTAGRAM_ACCESS_TOKEN"), _required("INSTAGRAM_BUSINESS_ID"),
        _required("META_API_VERSION"), transport=_meta_transport,
    )
    try:
        result = run_scheduled_publication(
            root, asset_commit=_required("GITHUB_SHA"),
            workflow_run_id=_required("GITHUB_RUN_ID"), fetcher=_pages_fetch,
            meta_client=client, git_runner=_git_runner(root),
        )
    except PublicationRunnerError as exc:
        performed = exc.publication_performed
        label = "sim" if performed is True else "não" if performed is False else "desconhecida"
        _write("GITHUB_STEP_SUMMARY", [
            "# FisioClinEx — Publicação agendada da fila", "",
            "**PUBLICAÇÃO AGENDADA INTERROMPIDA — NÃO REPETIR AUTOMATICAMENTE**", "",
            f"- Fase: `{exc.phase}`", f"- Run ID: `{exc.run_id or 'indisponível'}`",
            "- Revisão humana: necessária", f"- Publicação realizada: {label}",
        ])
        print(json.dumps({"phase": exc.phase, "publication_performed": performed, "status": "interrupted"}))
        return 1
    _write("GITHUB_STEP_SUMMARY", [
        "# FisioClinEx — Publicação agendada da fila", "",
        "**PUBLICAÇÃO AGENDADA CONCLUÍDA**", "", f"- Slug: `{result.slug}`",
        f"- Slides: {result.slides_count}", "- Pages verificado: sim",
        "- Bloqueio persistido: sim", "- Meta concluída: sim",
        f"- Media ID: `{result.media_id}`", "- Status: `published`",
        "- Histórico registrado: sim", "- Modo: `workflow_scheduled`",
        "- Publicação realizada: sim",
    ])
    print(result_json(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
