"""Offline reconciliation of durable feed evidence and its editorial projection.

No transport or Meta dependency. Registry is immutable evidence; repair requires
matching manifest confirmation and never invents a publication result.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from .editorial_calendar import load_calendar, load_policy, _slot_json
from .manifest import parse_manifest
from .registry import read_registry, RegistryError
from .schedule_integrity import schedule_lock, timestamp, ZONE
from .publication_writeback import atomic_write, WritebackError


def calendar_state(root):
    root = Path(root)
    path = root / 'publication-state/editorial-calendar.json'
    policy = load_policy(root / 'publication-state/editorial-calendar-policy.json')
    return path, policy, load_calendar(path, policy=policy)


def matching_confirmation(manifest, record):
    return (record is not None and record.slug == manifest.slug
            and record.publication_key == manifest.publication_key
            and record.media_id == manifest.publication.media_id
            and manifest.publication.published_at is not None
            and timestamp(record.published_at) == manifest.publication.published_at)


def registry_schedule_matches(root, manifest):
    """Extended historical fields, when present, must match queue identity."""
    path = Path(root) / 'publication-state/publications.jsonl'
    for line in path.read_text(encoding='utf-8').splitlines():
        record = json.loads(line)
        if record.get('publication_key') != manifest.publication_key:
            continue
        for field, expected in (('package_sha256', manifest.package_sha256),
                                ('slot_id', manifest.slot_id), ('slot_type', manifest.slot_type),
                                ('assigned_slug', manifest.slug)):
            if field in record and record[field] != expected:
                return False
        for field, expected in (('planned_at', manifest.planned_at), ('queued_at', manifest.queued_at)):
            if field in record and timestamp(record[field]) != expected:
                return False
        return True
    return False


def slot_matches(slot, manifest):
    return (slot is not None and slot.assigned_slug == manifest.slug
            and slot.planned_datetime == manifest.planned_at
            and slot.slot_type == manifest.slot_type
            and (timestamp(slot.queued_at) if slot.queued_at else None) == manifest.queued_at)


def project_confirmed_feed(root, manifest):
    """Update only status/time after matching registry evidence, under root lock."""
    if manifest.slot_id is None:
        return None  # Immediate/legacy feeds have no editorial slot to fabricate.
    records = {r.publication_key: r for r in read_registry(Path(root)/'publication-state/publications.jsonl')}
    record = records.get(manifest.publication_key)
    if not matching_confirmation(manifest, record) or not registry_schedule_matches(root, manifest):
        raise WritebackError('state_reconciliation_required: manifest_registry_mismatch')
    path, policy, slots = calendar_state(root)
    slot = next((s for s in slots if s.slot_id == manifest.slot_id), None)
    if not slot_matches(slot, manifest) or slot.status not in {'queued', 'published'}:
        raise WritebackError('state_reconciliation_required: queue_calendar_mismatch')
    if slot.status == 'published':
        if timestamp(slot.actual_published_at) != timestamp(record.published_at):
            raise WritebackError('state_reconciliation_required: calendar_time_mismatch')
        return path
    # Preserve all editorial metadata and timestamp spelling of durable evidence.
    updated = replace(slot, status='published', actual_published_at=record.published_at)
    values = [updated if s.slot_id == slot.slot_id else s for s in slots]
    payload = {'schema_version':1, 'policy_version':policy.schema_version,
               'slots':[_slot_json(s) for s in values]}
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2)+'\n')
    return path


def health(root):
    """Read-only complete state audit; historical daily violations are warnings."""
    root = Path(root).resolve(strict=True)
    issues, warnings = [], []
    def issue(code, slug=None, **details):
        issues.append(dict(code=code, slug=slug, **details))
    try:
        records = read_registry(root/'publication-state/publications.jsonl')
    except (RegistryError, OSError, ValueError) as exc:
        return dict(healthy=False, issues=[dict(code='registry_invalid', diagnostic=str(exc))], warnings=[])
    by_key = {r.publication_key:r for r in records}
    manifests = {}
    for path in sorted((root/'publication-state/queue').glob('*/manifest.json')):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError('unsafe manifest')
            m = parse_manifest(path.read_bytes())
            if m.slug != path.parent.name or m.publication_key in manifests:
                raise ValueError('duplicate or mismatched manifest identity')
            manifests[m.publication_key] = m
        except (OSError, ValueError) as exc:
            issue('manifest_invalid', path.parent.name, diagnostic=str(exc))
    calendar = root/'publication-state/editorial-calendar.json'
    slots = ()
    if calendar.exists() or any(m.slot_id for m in manifests.values()):
        try:
            _, _, slots = calendar_state(root)
        except (OSError, ValueError) as exc:
            issue('calendar_invalid', diagnostic=str(exc))
    by_slot = {s.slot_id:s for s in slots}
    reservations = {}
    for m in manifests.values():
        r = by_key.get(m.publication_key)
        confirmed = matching_confirmation(m, r) and registry_schedule_matches(root, m)
        if r is not None and not confirmed:
            issue('manifest_registry_mismatch', m.slug)
        if m.publication.media_id or m.publication.published_at or m.status.value=='published':
            if not confirmed:
                issue('needs_review', m.slug, reason='missing_or_conflicting_durable_confirmation')
        if m.slot_id is not None and m.status.value!='cancelled':
            s = by_slot.get(m.slot_id)
            if not slot_matches(s, m):
                issue('queue_calendar_mismatch', m.slug)
            elif confirmed:
                if s.status!='published' or timestamp(s.actual_published_at)!=timestamp(r.published_at):
                    issue('stale_queued_published_item' if s.status=='queued' else 'calendar_registry_mismatch', m.slug)
            elif s.status=='published':
                issue('needs_review', m.slug, reason='calendar_published_without_confirmation')
            elif s.status!='queued':
                issue('queue_calendar_mismatch', m.slug)
            if not confirmed and m.planned_at:
                day=m.planned_at.astimezone(ZONE).date().isoformat()
                reservations.setdefault(day,set()).add(m.slug)
    for r in records:
        if r.publication_key not in manifests:
            issue('orphan_record', r.slug)
    for s in slots:
        if s.status in {'queued','published'}:
            candidates=[m for m in manifests.values() if m.slug==s.assigned_slug and m.slot_id==s.slot_id and m.status.value!='cancelled']
            if len(candidates)!=1:
                issue('orphan_calendar_slot',s.assigned_slug,slot_id=s.slot_id)
    historical={}
    for r in records:
        day=timestamp(r.published_at).astimezone(ZONE).date().isoformat()
        historical.setdefault(day,set()).add(r.slug)
        if day in reservations:
            reservations[day].add(r.slug)
    for day, slugs in reservations.items():
        if len(slugs)>1: issue('conflicting_daily_slots',day=day,slugs=sorted(slugs))
    for day, slugs in historical.items():
        if len(slugs)>1: warnings.append(dict(code='historical_daily_limit',day=day,slugs=sorted(slugs)))
    return dict(healthy=not issues,issues=issues,warnings=warnings,manifests=len(manifests),feed_records=len(records),slots=len(slots))


def reconcile(root):
    """Repair stale projection only; every other divergence remains needs review."""
    root=Path(root).resolve(strict=True)
    with schedule_lock(root):
        before=health(root)
        repairable={i['slug'] for i in before['issues'] if i['code']=='stale_queued_published_item'}
        changed=[]
        for path in sorted((root/'publication-state/queue').glob('*/manifest.json')):
            m=parse_manifest(path.read_bytes())
            if m.slug in repairable:
                project_confirmed_feed(root,m);changed.append(m.slug)
        return dict(status='reconciled' if changed else 'unchanged',changed=changed,**health(root))


def main(argv=None):
    parser=argparse.ArgumentParser(prog='publication-state')
    parser.add_argument('--workspace',required=True)
    parser.add_argument('command',choices=('health','reconcile'))
    args=parser.parse_args(argv)
    result=health(args.workspace) if args.command=='health' else reconcile(args.workspace)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0 if result['healthy'] else 1


if __name__=='__main__':
    raise SystemExit(main())
