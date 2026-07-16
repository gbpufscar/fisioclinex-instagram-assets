#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fisioclinex_scheduled.queue_pages import PageResponse, validate_official_slide_url
from fisioclinex_scheduled.shadow_runner import (
    ShadowVerificationError,
    run_shadow_verified,
    verified_report_json,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url: str) -> PageResponse:
    filename = url.rsplit("/", 1)[-1]
    slug = url.rsplit("/", 2)[-2]
    validate_official_slide_url(url, slug, filename)
    request = Request(url, headers={"User-Agent": "FisioClinEx-Shadow-Verifier/1.0"})
    opener = build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=15) as response:
            return PageResponse(
                requested_url=url,
                final_url=response.geturl(),
                status=response.status,
                content_type=response.headers.get("Content-Type", ""),
                body=response.read(50 * 1024 * 1024 + 1),
            )
    except HTTPError as exc:
        return PageResponse(
            requested_url=url,
            final_url=exc.geturl(),
            status=exc.code,
            content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
            body=exc.read(50 * 1024 * 1024 + 1),
        )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    try:
        report = run_shadow_verified(
            root,
            now=datetime.now(timezone.utc),
            fetcher=_fetch,
            step_summary_path=summary or None,
        )
    except ShadowVerificationError as exc:
        print(verified_report_json(exc.report))
        return 1
    print(verified_report_json(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
