"""Complete the existing reschedule operation with fenced Git persistence."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .editorial_calendar import update_planning, load_policy
from .manifest import parse_manifest
from .publication_consistency import health
from .publication_writeback import verify_remote, WritebackError, GitWritebackError, classify_git_failure
from .schedule_integrity import schedule_lock
from .fingerprint import fingerprint_package


def git_executor(root):
    def run(args):
        result=subprocess.run(['git',*args],cwd=root,capture_output=True,text=True,timeout=60,check=False)
        if result.returncode:
            raise GitWritebackError(args[0],classify_git_failure(args[0],result.stderr))
        return result.stdout.strip()
    return run


def reschedule_remote(root, *, slug, target_slot_id, expected_head, audit, git_runner=None, now=None):
    """Success requires both exact files in the commit and verified remote blobs.

    Push failure preserves local evidence. Re-run only after reviewing and
    refreshing the remote; no blind rollback or retry after concurrent writes.
    """
    root=Path(root).resolve(strict=True)
    run=git_runner or git_executor(root)
    if not re.fullmatch(r'[0-9a-f]{40}',expected_head or ''):
        raise WritebackError('expected HEAD required')
    if not callable(audit):
        raise WritebackError('backup audit required')
    with schedule_lock(root/'.publication-execution'):
        if run(('status','--porcelain')):
            raise WritebackError('reschedule requires clean worktree')
        run(('fetch','origin','main'))
        if run(('rev-parse','HEAD'))!=expected_head or run(('rev-parse','origin/main'))!=expected_head:
            raise WritebackError('reschedule HEAD conflict; refresh/reconciliation required')
        if not health(root)['healthy']:
            raise WritebackError('reschedule requires healthy publication state')
        calendar=root/'publication-state/editorial-calendar.json'
        path=root/'publication-state/queue'/slug/'manifest.json'
        manifest=parse_manifest(path.read_bytes())
        if manifest.status.value!='queued' or manifest.attempts!=0 or manifest.publication.media_id or manifest.publication.published_at:
            raise WritebackError('reschedule requires untouched queued feed')
        files=['legenda.txt',*[f'{slug}-slide-{n:02d}.png' for n in range(1,manifest.slides_count+1)],f'{slug}-story.png']
        from tempfile import TemporaryDirectory
        import shutil
        with TemporaryDirectory() as temporary:
            folder=Path(temporary)
            for name in files:
                source=path.parent/name if name=='legenda.txt' else root/'posts'/slug/name
                if source.is_symlink():raise WritebackError('unsafe package')
                shutil.copyfile(source,folder/name)
            if fingerprint_package(folder,files)!=manifest.package_sha256:
                raise WritebackError('package integrity mismatch')
        before=path.read_bytes(),calendar.read_bytes()
        result=update_planning(calendar,policy=load_policy(root/'publication-state/editorial-calendar-policy.json'),
                slug=slug,target_slot_id=target_slot_id,workspace=root,now=now or datetime.now(timezone.utc))
        after=parse_manifest(path.read_bytes())
        preserved=('publication_key','package_sha256','queued_at','attempts','publication','slides_count')
        if any(getattr(after,k)!=getattr(manifest,k) for k in preserved) or not health(root)['healthy']:
            raise WritebackError('reschedule invariant failed; review local state')
        paths=(path,calendar)
        if result!='unchanged' and (before[0]==path.read_bytes() or before[1]==calendar.read_bytes()):
            raise WritebackError('reschedule partial mutation')
        if result!='unchanged':
            relatives=tuple(p.relative_to(root).as_posix() for p in paths)
            run(('add','--',*relatives))
            if set(run(('diff','--cached','--name-only')).splitlines())!=set(relatives):
                raise WritebackError('reschedule index must contain manifest and calendar only')
            run(('diff','--cached','--check'))
            run(('commit','-m',f'queue: reschedule {slug}'))
            audit(root)  # Every push is blocked by any finding.
            run(('push','origin','HEAD:main'))
        head=verify_remote(root,(*paths,root/'publication-state/publications.jsonl'),git_runner=run)
        return dict(status='completed',local_mutation=result,remote_verified=True,head=head,slug=slug,slot_id=after.slot_id)
