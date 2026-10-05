#!/usr/bin/env python3
"""Two explicit rollout gates; both default off before loading credentials."""
import argparse
import json
import os
import sys
from pathlib import Path



def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root")
    args = parser.parse_args(argv)
    if os.environ.get("STORY_SCHEDULER_ENABLED") != "true":
        print(json.dumps({"status": "disabled", "selected": False}))
        return 0
    root = Path(args.repository_root or Path(__file__).resolve().parents[1]).resolve()
    path = root / "automation/story-scheduler-control.json"
    if path.is_symlink():
        raise ValueError("unsafe scheduler control")
    control = json.loads(path.read_bytes())
    if control != {"schema_version": 1, "story_scheduler_enabled": True}:
        print(json.dumps({"status": "disabled", "selected": False}))
        return 0
    from run_manual_publication import _pages_fetch, _git_runner, _meta_transport, _required
    from fisioclinex_scheduled.meta_client import MetaClient
    from fisioclinex_scheduled.story_scheduler import run_story_scheduler
    from fisioclinex_scheduled.story_publisher import StoryPublicationError
    # Validate schedule and manual dispatch with the same repository/main guard.
    import re
    if (os.environ.get("GITHUB_REPOSITORY") != "gbpufscar/fisioclinex-instagram-assets"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch"}
            or not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("GITHUB_SHA", ""))
            or not os.environ.get("GITHUB_RUN_ID", "").isdigit()):
        raise ValueError("invalid Story scheduler context")
    client = MetaClient(_required("INSTAGRAM_ACCESS_TOKEN"), _required("INSTAGRAM_BUSINESS_ID"),
                        _required("META_API_VERSION"), transport=_meta_transport)
    try:
        result = run_story_scheduler(root, enabled=True, fetcher=_pages_fetch,
                                     meta_client=client, git_runner=_git_runner(root))
    except StoryPublicationError as exc:
        print(json.dumps({"status": "interrupted", "phase": exc.phase}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
