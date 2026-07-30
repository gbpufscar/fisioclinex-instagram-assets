#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from run_manual_publication import _git_runner, _meta_transport, _required, _validate_github_context
from fisioclinex_scheduled.meta_client import MetaClient
from fisioclinex_scheduled.story_recovery import StoryRecoveryError, recover_story


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--short-slug", required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--repository-root")
    args = parser.parse_args(argv)
    root = Path(args.repository_root or Path(__file__).resolve().parents[1]).resolve()
    _validate_github_context()
    client = MetaClient(
        _required("INSTAGRAM_ACCESS_TOKEN"),
        _required("INSTAGRAM_BUSINESS_ID"),
        _required("META_API_VERSION"),
        transport=_meta_transport,
    )
    try:
        result = recover_story(
            root,
            short_slug=args.short_slug,
            confirmation=args.confirmation,
            meta_client=client,
            git_runner=_git_runner(root),
        )
    except StoryRecoveryError as exc:
        print(json.dumps({"status": "interrupted", "phase": exc.phase}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
