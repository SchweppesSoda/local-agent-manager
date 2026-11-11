"""The plan/apply/verify pipeline shared by online preflight and offline handoff.

This module has no desktop lifecycle actions. A ready online plan proves the
whole static cleanup path; it never grants a write or substitutes for a fresh
offline plan. Only the exact confirmed frontend/native scope may proceed.
"""
from dataclasses import dataclass
from pathlib import Path

from .operation_store import plan_sha256, strict_json_load
from .record_identity import canonical_path
from .t3code_cleanup import evidence_from_document


class CleanupFlowError(RuntimeError):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code, self.details, self.retryable = code, details, True


def forwarded_arguments(args, root):
    result = ['--t3code-root', str(root)]
    for flag, attr in (
        ('--t3code-bindings', 't3code_bindings'), ('--codex-home', 'codex_home'),
        ('--codex-bin', 'codex_bin'), ('--antigravity-root', 'antigravity_root'),
        ('--antigravity-bin', 'antigravity_bin'), ('--operation-home', 'operation_home'),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            result.extend((flag, str(Path(value).expanduser().resolve())))
    for flag, attr in (('--orca-root', 'orca_root'), ('--herdr-root', 'herdr_root'),
                       ('--workbuddy-root', 'workbuddy_root'), ('--paseo-root', 'paseo_root')):
        for value in getattr(args, attr, ()) or ():
            result.extend((flag, str(Path(value).expanduser().resolve())))
    return result


def cleanup_scope(evidence):
    """Ownership only: changing logs/timestamps cannot widen confirmation."""
    native = [dict(engine=t['engine'], root=canonical_path(t['root']),
                   ids=sorted(t['ids']), paths=sorted(t['paths']))
              for t in evidence['native_targets']]
    return dict(root=canonical_path(evidence['root']),
                thread_ids=sorted(evidence['selected_thread_ids']),
                native_targets=sorted(native, key=lambda t: (t['engine'], t['root'])))


@dataclass(frozen=True)
class PreparedCleanup:
    document: dict
    scope: dict

    @property
    def summary(self):
        return {key: self.document.get(key) for key in
                ('operation_id', 'plan_path', 'plan_sha256', 'goal_status', 'blockers')}


def prepare_cleanup(record_ids, forwards, path, *, run_cli, coordinator=None,
                    expected_scope=None, stage='preflight'):
    code, result = run_cli(['delete', 'plan', '--client', 't3code',
        *(arg for rid in record_ids for arg in ('--record-id', str(rid))),
        '--out', str(path), *forwards, '--json'], coordinator=coordinator)
    if code != 0 or result.get('goal_status') != 'ready':
        raise CleanupFlowError(stage + '_plan_blocked',
            '完整清理计划未通过：' + str(result.get('blockers', [])), details=result)
    document = strict_json_load(path)
    if (document.get('plan_sha256') != result.get('plan_sha256')
            or plan_sha256(document) != document.get('plan_sha256')):
        raise CleanupFlowError('cleanup_plan_binding_invalid', '计划文件与返回摘要不一致。')
    evidence = evidence_from_document(document)
    if evidence is None:
        raise CleanupFlowError('cleanup_scope_missing', '计划缺少完整 T3 清理范围。')
    scope = cleanup_scope(evidence)
    if expected_scope is not None and scope != expected_scope:
        raise CleanupFlowError('offline_scope_drift', '前端或原生清理范围与已确认范围不一致。')
    return PreparedCleanup(document, scope)


def execute_cleanup(prepared, forwards, *, timeout, run_cli, coordinator, receipt, event):
    """Execute once. Unknown responses go through status/verify, never replay."""
    plan = prepared.document
    common = ['--operation-id', plan['operation_id'], '--plan', plan['plan_path']]
    receipt['plan'] = prepared.summary
    event('plan_ready', operation_id=plan['operation_id'])
    # Durable worker error reporting must remain unknown if dispatch raises.
    receipt['mutation_started'] = True
    _, applied = run_cli(['delete', 'apply', '--client', 't3code', *common,
        '--authorized-plan-sha256', plan['plan_sha256'], '--clients-closed',
        '--timeout', str(timeout), *forwards, '--json'], coordinator=coordinator)
    receipt['apply'] = applied
    receipt['mutation_started'] = bool(applied.get('mutation_started', True))
    event('apply_finished', operation_id=plan['operation_id'], goal_status=applied.get('goal_status'))
    if applied.get('goal_status') == 'unknown':
        _, receipt['status'] = run_cli(['operation', 'status', *common, '--json'], coordinator=None)
    _, verified = run_cli(['operation', 'verify', *common, '--json'], coordinator=None)
    receipt['cold_verify'] = verified
    event('cold_verify_finished', operation_id=plan['operation_id'], goal_status=verified.get('goal_status'))
    receipt['goal_status'] = str(verified.get('goal_status') or 'unknown')
    receipt['goal_satisfied'] = verified.get('goal_status') == 'complete' and verified.get('goal_satisfied') is True
