"""Exact-ID batch automation using the existing per-thread operation journal."""
from pathlib import Path

from .operation_store import write_new_json
from .t3code_store import default_root


def run_server_batch(coordinator, *, record_ids, roots=(), authorized=False,
                     out=None, timeout=30, progress_callback=None):
    if not authorized:
        raise ValueError("t3code_run_requires_yes")
    if len(roots) > 1 or not record_ids or len(set(record_ids)) != len(record_ids):
        raise ValueError("t3code_run_requires_unique_exact_ids_and_one_profile")
    root = roots[0] if roots else default_root()
    result = {"schema_version": "lam.t3code-batch-result.v1", "command": "delete", "subcommand": "run",
              "scope": {"client": "t3code", "t3code_only": True, "record_ids": list(record_ids)},
              "goal_status": "blocked", "goal_satisfied": False, "modified": False,
              "mutation_started": False, "plans": [], "results": [], "blockers": []}
    # Plan every target before dispatching any deletion. Each exact journal
    # remains independently recoverable if a later target changes or fails.
    for index, thread_id in enumerate(record_ids):
        path = None
        if out is not None:
            output = Path(out)
            if output.exists():
                raise ValueError("t3code_batch_output_exists")
            path = output.with_name(f"{output.stem}.{index + 1}.plan.json")
        planned = coordinator.plan_operation(
            scope={"client": "t3code", "t3code_only": True, "record_ids": [thread_id]},
            t3code_roots=(root,), plan_path=path, progress_callback=progress_callback)
        result["plans"].append(planned)
        if planned.get("goal_status") != "ready":
            result["blockers"].extend(planned.get("blockers", ()))
            return result
    manifest = {"schema_version": "lam.t3code-batch.v1", "root": str(root),
                "plans": [{key: plan[key] for key in ("operation_id", "plan_path", "plan_sha256")}
                          for plan in result["plans"]]}
    if out is not None:
        write_new_json(Path(out), manifest)
    for plan in result["plans"]:
        try:
            applied = coordinator.apply_operation(operation_id=plan["operation_id"],
                plan_path=Path(plan["plan_path"]), plan_sha256=plan["plan_sha256"],
                scope={"t3code_only": True}, timeout=timeout, progress_callback=progress_callback)
        except Exception:
            result.update(goal_status="unknown", modified=result["modified"] or None,
                          mutation_started=result["mutation_started"] or None)
            result["blockers"].append({"blocker_code": "t3code_batch_outcome_unavailable",
                                       "operation_id": plan["operation_id"], "plan_path": plan["plan_path"]})
            return result
        result["results"].append(applied)
        result["modified"] |= bool(applied.get("modified"))
        result["mutation_started"] |= bool(applied.get("mutation_started"))
        if applied.get("goal_status") in {"unknown", "complete"}:
            try:
                # Resolve an ambiguous response through observation, never replay.
                if applied.get("goal_status") == "unknown":
                    coordinator.status_operation(operation_id=plan["operation_id"], plan_path=Path(plan["plan_path"]))
                verified = coordinator.verify_operation(operation_id=plan["operation_id"],
                    plan_path=Path(plan["plan_path"]), verify_timeout=0, progress_callback=progress_callback)
                result["results"].append(verified)
                result["modified"] |= bool(verified.get("modified"))
                result["mutation_started"] |= bool(verified.get("mutation_started"))
                applied = verified
            except Exception:
                result["goal_status"] = "unknown"
                result["blockers"].append({"blocker_code": "t3code_batch_verification_unavailable",
                                           "operation_id": plan["operation_id"], "plan_path": plan["plan_path"]})
                return result
        if applied.get("goal_status") != "complete":
            result["goal_status"] = applied.get("goal_status", "unknown")
            result["blockers"].extend(applied.get("blockers", ()))
            return result
    result.update(goal_status="complete", goal_satisfied=True)
    return result
