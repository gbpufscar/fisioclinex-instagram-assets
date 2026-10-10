"""Small CLI for generation, inspection, assignment and skipping of editorial slots."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

from .editorial_calendar import (
    assign_slot,
    generate_slots,
    merge_generated,
    next_available_slot,
    render_calendar,
    update_planning,
)
from .queue_package import validate_queue_package


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calendario-editorial")
    parser.add_argument("--calendar")
    parser.add_argument("--workspace")
    parser.add_argument("--policy", help="compatibility argument; Assets policy is authoritative")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--start", type=date.fromisoformat, required=True)
    generate.add_argument("--weeks", type=int)
    show = sub.add_parser("show")
    show.add_argument("--limit", type=int, default=20)
    sub.add_parser("next")
    assign = sub.add_parser("assign")
    assign.add_argument("--slot-id", required=True)
    assign.add_argument("--package", required=True)
    assign.add_argument("--editorial-type", choices=("scientific", "other"), required=True)
    assign.add_argument("--override-reason")
    skip = sub.add_parser("skip")
    skip.add_argument("--slot-id", required=True)
    reschedule = sub.add_parser("reschedule")
    reschedule.add_argument("--slug", required=True)
    reschedule.add_argument("--slot-id", required=True)
    reschedule.add_argument("--workspace", default=argparse.SUPPRESS)
    reschedule.add_argument("--persist-remote", action="store_true")
    reschedule.add_argument("--expected-head")
    reschedule.add_argument("--audit-script")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--slug", required=True)
    cancel.add_argument("--workspace", default=argparse.SUPPRESS)
    return parser


def main(argv=None, *, snapshot_reader=None, now_fn=lambda: datetime.now(timezone.utc), output_fn=print) -> int:
    args = build_parser().parse_args(argv)
    from .assets_calendar import read_current, CALENDAR
    from .queue_config import load_queue_config
    root = Path(__file__).resolve().parents[2]
    configured = root / "publicacao-agendada/config.json"
    workspace = Path(args.workspace or load_queue_config(configured).workspace_path)
    head, policy, slots = (snapshot_reader or read_current)(workspace)
    canonical = workspace / CALENDAR
    if args.calendar and Path(args.calendar).resolve() != canonical.resolve():
        raise ValueError("Studio calendar is not an operational source")
    args.calendar = str(canonical)
    now = now_fn()
    generated_view = slots
    if args.command in {"reschedule", "cancel"}:
        if args.command == "reschedule" and args.persist_remote:
            from .planning_writeback import reschedule_remote
            import importlib.util
            import json
            if not args.audit_script:
                raise ValueError("--audit-script obrigatório para persistência remota")
            spec = importlib.util.spec_from_file_location("backup_audit", args.audit_script)
            scanner = importlib.util.module_from_spec(spec); spec.loader.exec_module(scanner)
            def audit(root):
                if scanner.inspect(scanner.candidates(root), root):
                    raise ValueError("backup audit blocked push")
            result = reschedule_remote(args.workspace or workspace, slug=args.slug,
                target_slot_id=args.slot_id, expected_head=args.expected_head, audit=audit, now=now)
            output_fn(json.dumps(result, sort_keys=True))
        else:
            result = update_planning(args.calendar, policy=policy, slug=args.slug,
                                     target_slot_id=getattr(args, "slot_id", None),
                                     workspace=args.workspace or workspace, cancel=args.command == "cancel", now=now)
            output_fn(f"local_mutation_successful: {result}: {args.slug}; remote_not_verified")
    elif args.command == "generate":
        output_fn("proposal only; persistence requires authorized Assets maintenance")
        output_fn(render_calendar(merge_generated(slots, generate_slots(args.start, policy=policy, weeks=args.weeks)), limit=20))
    elif args.command == "skip":
        raise ValueError("mutação persistente exige fluxo operacional autorizado no Assets")
    elif args.command == "show":
        output_fn(f"Assets HEAD: {head}")
        output_fn(render_calendar(generated_view, limit=args.limit))
    elif args.command == "next":
        from .schedule_integrity import ScheduleConflictError, check_reservations, persisted_reservations
        available = generated_view
        if workspace is not None:
            reservations = persisted_reservations(workspace)
            available = []
            for candidate in generated_view:
                if candidate.status != "available":
                    continue
                try:
                    check_reservations("fisioclinex-next-candidate", candidate.planned_datetime, reservations)
                except ScheduleConflictError:
                    continue
                available.append(candidate)
        slot = next_available_slot(available, now=now)
        output_fn(f"Assets HEAD: {head}")
        output_fn("nenhum slot disponível" if slot is None else render_calendar((slot,), limit=1))
    elif args.command == "assign":
        package = validate_queue_package(args.package)
        slots = assign_slot(
            slots,
            slot_id=args.slot_id,
            slug=package.slug,
            editorial_type=args.editorial_type,
            now=now,
            explicit_override=args.override_reason is not None,
            override_reason=args.override_reason,
            derived_story=package.visual_schema_version == "2.1",
        )
        from .schedule_integrity import validate_schedule_write
        slot = next(s for s in slots if s.slot_id == args.slot_id)
        validate_schedule_write(workspace, slug=package.slug, planned_at=slot.planned_datetime,
                                explicit_override=slot.explicit_override, override_reason=slot.override_reason)
        output_fn(f"proposed: {package.slug} -> {args.slot_id}; planning_head={head}; not persisted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
