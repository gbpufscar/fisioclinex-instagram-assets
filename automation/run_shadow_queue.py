#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from fisioclinex_scheduled.shadow_runner import report_json, run_shadow


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    report = run_shadow(
        root,
        now=datetime.now(timezone.utc),
        step_summary_path=summary or None,
    )
    print(report_json(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
