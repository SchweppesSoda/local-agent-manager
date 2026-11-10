"""Batch the existing exact-ID frontend operations without replaying mutations."""
from pathlib import Path

from .operation_store import write_new_json


def run_frontend_batch(coordinator, *, client, record_ids, roots=(), authorized=False,
                       out=None, timeout=30, operation_home=None, progress_callback=None):
    if client not in {"t3code", "paseo"}:
        raise ValueError("frontend_batch_client_unsupported")
    if not authorized:
        raise ValueError(f"{client}_run_requires_yes")
    if len(roots) > 1 or not record_ids or len(set(record_ids)) != len(record_ids):
        raise ValueError(f"{client}_run_requires_unique_exact_ids_and_one_profile")
    if client == "t3code":
        from .t3code_store import default_root
    else:
        from .paseo_store import default_root
    root = roots[0] if roots else default_root()
    flag = client + "_only"
    scope = {"client": client, flag: True, "record_ids": list(record_ids)}
    result = {"schema_version": f"lam.{client}-batch-result.v1", "command": "delete", "subcommand": "run",
              "scope": scope, "goal_status": "blocked", "goal_satisfied": False,
              "modified": False, "mutation_started": False, "plans": [], "results": [], "blockers": []}
    output = Path(out) if out is not None else None
    if output is not None and output.exists():
        raise ValueError(f"{client}_batch_output_exists")
    # Every target gets a persisted, independently recoverable plan before
    # the first dispatch. Approval remains bound to each immutable plan.
    for index, record_id in enumerate(record_ids):
        path = output.with_name(f"{output.stem}.{index + 1}.plan.json") if output is not None else None
        planned = coordinator.plan_operation(scope={**scope, "record_ids": [record_id]},
            **{client + "_roots": (root,)}, plan_path=path, operation_home=operation_home,
            timeout=timeout, progress_callback=progress_callback)
        result["plans"].append(planned)
        if planned.get("goal_status") != "ready":
            result["blockers"].extend(planned.get("blockers", ()))
            return result
    if output is not None:
        write_new_json(output, {"schema_version": f"lam.{client}-batch.v1", "root": str(root),
            "plans": [{key: plan[key] for key in ("operation_id", "plan_path", "plan_sha256")}
                      for plan in result["plans"]]})
    for plan in result["plans"]:
        identity = {"operation_id": plan["operation_id"], "plan_path": Path(plan["plan_path"]),
                    "operation_home": operation_home}
        try:
            applied = coordinator.apply_operation(**identity, plan_sha256=plan["plan_sha256"],
                scope={flag: True}, timeout=timeout, progress_callback=progress_callback)
        except Exception:
            applied = {"goal_status": "unknown", "modified": None, "mutation_started": None}
        result["results"].append(applied)
        for field in ("modified", "mutation_started"):
            result[field] = True if result[field] or applied.get(field) else applied.get(field)
        if applied.get("goal_status") in {"unknown", "complete"}:
            try:
                if applied.get("goal_status") == "unknown":
                    coordinator.status_operation(**identity)
                applied = coordinator.verify_operation(**identity, verify_timeout=0,
                    progress_callback=progress_callback)
                result["results"].append(applied)
                for field in ("modified", "mutation_started"):
                    result[field] = True if result[field] or applied.get(field) else applied.get(field)
            except Exception:
                result["goal_status"] = "unknown"
                result["blockers"].append({"blocker_code": f"{client}_batch_verification_unavailable",
                    "operation_id": plan["operation_id"], "plan_path": plan["plan_path"]})
                return result
        if applied.get("goal_status") != "complete" or applied.get("goal_satisfied") is not True:
            result["goal_status"] = applied.get("goal_status", "unknown")
            result["blockers"].extend(applied.get("blockers", ()))
            return result
    result.update(goal_status="complete", goal_satisfied=True)
    return result
