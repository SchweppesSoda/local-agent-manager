"""WorkBuddy's bounded nested diagnostics; never traverse native session stores.

Producer paths/envelopes were verified in installed 5.7.6 main/handlers.js,
main/index.js and main/process-cpu-sampler.js. Unknown members stay reviewable.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import stat
import struct

from . import frozen_files as ff

DAY = r'\d{4}-\d{2}-\d{2}'
STAMP = r'\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})'
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
CRASH_REPORT = re.compile(r'logs/Crash-Log/crash-report-[a-z-]+-\d+-\d{8}T\d{6}\.json\Z')
CRASH_CURSOR = 'logs/Crash-Log/.processed-crashes.json'
DIRECTORY = re.compile(r'logs/(?:' + DAY + r'(?:/sdk(?:/conversations)?)?'
    r'|(?:migration|update|database|editor_sdk|perf|Crash-Log|weixinpay)'
    r'|startup(?:/' + DAY + r')?|sandbox(?:/\d{8})?)\Z')
SDK_ACTIONS = {
    'list', 'fetchList', 'listByIds', 'create', 'get', 'create:ready', 'delete',
    'lifecycle:created', 'method:requests', 'method:requests:result', 'busy-ownership:reconcile',
    'artifact:query-started', 'artifact:query-completed', 'method:sendPrompt',
    '[QueueDiag] core.sendPrompt.direct', '[QueueDiag] prepareQueueDrain.skip',
    'state-machine:transition', 'runtime-status:persisted', 'connection:create',
    'runtime:session-load-fresh', 'connection:state', 'usage:session-scope', 'dispatch:mode-resolved',
    'event-machine:dispatch', 'ensureRuntime', 'runtime.applyStopReason', 'connection:runtime-refreshed',
    'cloud.http.detail.request_started', 'cloud.http.detail.request_completed',
    'cloud.http.detail.request_failed', 'lifecycle:dispose',
    *('LOCAL_HTTP_CONNECTION >> ' + action for action in (
        'acquire local http runtime', 'sse connected', 'connected', 'highfreq', 'session/prompt blocks',
        'runtime refresh requires worker restart', 'killWorker', 'disconnect')),
}
# A conversation diagnostic may describe prompt block types, never their text,
# images or tool payload. Validate every row, including nested and encoded JSON.
SDK_KEYS = set('''url total count ids requested returned filter page size transport view space kind
conversationOrigin state instanceId restoredOrphanPending byteLength itemCount hasOlder headIds states
trigger from decision reason ownerKinds conflicts truncated items resultScope blockCount mode modelId
queueOwner isBusyForQueue dispatching queueSizeBefore input valid to actions queueReserved status cwd
conversationId reconnecting generation sessionId streaming connectionId launchSpecMs httpReadyMs
connectMs emitted cached replaying totalTokens contextWindow hasByCategory byCategoryVersion line
latestRequestState dispatchGeneration optionMode requestedInteractionMode requestedRuntimePermissionMode
configManagerMode requestId output completeAssistantStream hasUsage hasTitle resourceEffectCount blocks
type missing stateEvent messageId event stopReason hasFatalError hasTurnError requestState
errorMessageMetaType queueSize finalState previous completed previousFingerprint nextFingerprint changed
envChangedKeys method route durationMs errorKind httpStatus level createdBy isBackgroundAutomation
projectScope projectTaskView ms'''.split())


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('workbuddy_duplicate_json_key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs)


def entries(root, names):
    """Descend only registered diagnostic directories, with a shared budget."""
    pending, count = ['logs'], 0
    while pending:
        parent = pending.pop()
        for name in names(root, parent):
            count += 1
            if count > ff.MAX_ENTRIES:
                raise ff.FrozenFilesError('maintenance_entry_budget')
            relative = parent + '/' + name
            info = ff.checked_path(root, relative).lstat()
            if (stat.S_ISDIR(info.st_mode) and DIRECTORY.fullmatch(relative)
                    and not getattr(info, 'st_file_attributes', 0) & 0x400):
                pending.append(relative)
            else:
                yield relative, info, parent != 'logs'


def nested(relative):
    return relative == 'logs/perf-metrics-cli-pids.json' or bool(
        DIRECTORY.fullmatch(str(Path(relative).parent).replace('\\', '/'))
        and relative.count('/') >= 2 and not re.fullmatch('logs/' + DAY + '/[^/]+', relative))


def _old_stamp(stamp, cutoff):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() * 10**9 < cutoff


def _dates(relative, cutoff):
    dates = re.findall(r'(?<!\d)(\d{4}-\d{2}-\d{2}|\d{8})(?!\d)', relative)
    return all(_old_stamp(datetime.strptime(day, '%Y-%m-%d' if '-' in day else '%Y%m%d')
                          .replace(tzinfo=timezone.utc).isoformat(), cutoff) for day in dates)


def _sdk_metadata(value, depth=0):
    if depth > 8:
        return False
    if isinstance(value, dict):
        return all(k in SDK_KEYS and _sdk_metadata(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return len(value) <= 2000 and all(_sdk_metadata(v, depth + 1) for v in value)
    return value is None or type(value) in {bool, int, float} or isinstance(value, str) and len(value) <= 4096


def _sdk(lines, relative, cutoff):
    name = Path(relative).name
    if not re.fullmatch(r'(?:operation|__cloud__|' + UUID + r')\.log', name):
        return False
    for line in lines:
        m = re.fullmatch('(' + STAMP + r') (.+?) (\{.*\})', line)
        if not m or m[2] not in SDK_ACTIONS or not _old_stamp(m[1], cutoff):
            return False
        obj = _json(m[3])
        if not isinstance(obj, dict) or not _sdk_metadata(obj):
            return False
        if name == 'operation.log' and m[2] not in {'list', 'create', 'get', 'create:ready', 'delete'}:
            return False
        if name == '__cloud__.log' and m[2] not in {'fetchList', 'listByIds'}:
            return False
        if 'line' in obj:
            encoded = re.fullmatch(r'\[LocalHttpConnection\] request ok (\{.*\})', obj['line']) if isinstance(obj['line'], str) else None
            if not encoded or not _sdk_metadata(_json(encoded[1])):
                return False
        if 'blocks' in obj and (not isinstance(obj['blocks'], list) or any(
                not isinstance(b, dict) or set(b) != {'type', 'missing'}
                or b['type'] not in {'text', 'image', 'resource_link'} or not isinstance(b['missing'], list)
                or any(x not in {'data', 'mimeType', 'name', 'uri'} for x in b['missing']) for b in obj['blocks'])):
            return False
    return bool(lines)


def _marks(lines, cutoff):
    for line in lines:
        m = re.fullmatch(r'\[(' + STAMP + r')\] (\{.*\})', line)
        if not m or not _old_stamp(m[1], cutoff):
            return False
        value = _json(m[2])
        if not isinstance(value, dict) or value.get('_type') not in {'mark', 'summary'}:
            return False
        if value['_type'] == 'mark' and not all(isinstance(value.get(k), str) for k in ('phase', 'source', 'proc')):
            return False
        if value['_type'] == 'summary' and value.get('flowType') not in {'startup', 'worker-startup'}:
            return False
        if set(value) - {'_type', 'timestamp', 'epochMs', 'phase', 'source', 'pid', 'proc', 'timeOrigin',
                         'data', 'elapsed', 'delta', 'flowType', 'totalDurationMs', 'totalMarks', 'phases'}:
            return False
        if _contains_payload(value):
            return False
    return bool(lines)


def _contains_payload(value):
    if isinstance(value, dict):
        return any(k in {'content', 'messages', 'prompt', 'contentBlocks', 'toolResult', 'text', 'image'}
                   or _contains_payload(v) for k, v in value.items())
    return isinstance(value, list) and any(_contains_payload(v) for v in value)


def _xlog(raw):
    # Tencent Mars LogCrypt: 73-byte header, little-endian payload length, zero tail.
    offset = 0
    while offset < len(raw):
        if len(raw) - offset < 74 or raw[offset] not in range(6, 14):
            return False
        if raw[offset + 3] > 23 or raw[offset + 4] > 23:
            return False
        size = struct.unpack_from('<I', raw, offset + 5)[0]
        end = offset + 73 + size
        if end >= len(raw) or raw[end] != 0:
            return False
        offset = end + 1
    return bool(raw)


def qualify(root, relative, cutoff, *, discard_pending=False):
    """Return exact content proof, retained dependencies and a semantic reason."""
    proof, raw = ff.read_file(root, relative, content=True, limit=ff.MAX_REWRITE_BYTES)
    if proof['mtime_ns'] >= cutoff or not _dates(relative, cutoff):
        return proof, [], 'recent_workbuddy_diagnostic'
    dependencies = []
    if relative == CRASH_CURSOR:
        return proof, [], 'crash_processing_cursor'
    if CRASH_REPORT.fullmatch(relative):
        obj = _json(raw)
        required = {'launchedAt', 'pid', 'processName', 'versions', 'platform', 'arch', 'appVersion', 'entries', 'droppedCount'}
        if not isinstance(obj, dict) or set(obj) != required or not isinstance(obj['entries'], list):
            return proof, [], 'workbuddy_diagnostic_format_unverified'
        if (type(obj['pid']) is not int or type(obj['droppedCount']) is not int
                or not _old_stamp(obj['launchedAt'], cutoff) or not isinstance(obj['versions'], dict)
                or not all(isinstance(obj[k], str) for k in ('processName', 'platform', 'arch', 'appVersion'))
                or any(not isinstance(e, dict) or not {'timestamp', 'type', 'errorName', 'errorMessage'} <= set(e)
                       or not _old_stamp(e['timestamp'], cutoff) for e in obj['entries'])):
            return proof, [], 'workbuddy_diagnostic_format_unverified'
        cursor, body = ff.read_file(root, 'logs/Crash-Log/.processed-crashes.json', content=True)
        state = _json(body)
        if (not isinstance(state, dict) or set(state) != {'files'} or not isinstance(state['files'], dict)
                or any(type(count) is not int or count < 0 for count in state['files'].values())):
            return proof, [], 'workbuddy_diagnostic_format_unverified'
        done = state['files'].get(Path(relative).name)
        if not discard_pending and (type(done) is not int or done < len(obj['entries'])):
            return proof, [], 'crash_entries_pending_native_processing'
        dependencies = [cursor]
        valid = True
    elif re.fullmatch(r'logs/sandbox/\d{8}/sandbox_(?:center|gc)_\d+\.mmap3', relative):
        valid = len(raw) == 150 * 1024 and not any(raw)
    elif re.fullmatch(r'logs/weixinpay/weixinpay_\d{8}\.xlog', relative):
        valid = _xlog(raw)
    elif re.fullmatch(r'logs/weixinpay/weixinpay_\d{8}\.seq', relative):
        _, paired = ff.read_file(root, relative[:-4] + '.xlog', content=True, limit=ff.MAX_REWRITE_BYTES)
        valid = bool(re.fullmatch(rb'\d{1,10}', raw)) and _xlog(paired)
    elif relative == 'logs/perf-metrics-cli-pids.json':
        obj = _json(raw)
        valid = (isinstance(obj, dict) and set(obj) == {'capturedAtMs', 'pids'}
                 and type(obj['capturedAtMs']) is int and obj['capturedAtMs'] * 10**6 < cutoff and obj['pids'] == [])
    else:
        text = raw.decode('utf-8')
        lines = text.splitlines()
        first = next((line for line in lines if line), '')
        valid = False
        if re.fullmatch('logs/' + DAY + r'/sdk/conversations/[^/]+\.log', relative):
            valid = _sdk(lines, relative, cutoff)
        elif re.fullmatch(r'logs/(migration|update|database)/(?:migration|update|database)-\d{8}\.log', relative):
            directory, name = relative.split('/')[1:]
            dates = re.findall(r'Session started at (' + STAMP + ')', text)
            valid = (name.startswith(directory + '-') and first == '=' * 80 and bool(dates)
                     and all(_old_stamp(s, cutoff) for s in dates)
                     and ('Logger initialized at ' in text)
                     and relative.replace('/', '\\') in text)
            if directory == 'migration':
                valid = valid and '=== Migration Completed ===' in text and 'Status: SUCCESS' in text
        elif relative == 'logs/update/qm-uninstall.log':
            valid = bool(lines) and all(re.match(r'\[\d{4}-\d{2}-\d{2}T[0-9:]+\] \[QmProtector\] ', line) for line in lines)
        elif relative == 'logs/editor_sdk/editor_sdk.log':
            valid = bool(lines) and all(re.match(r'\d{4}-\d{2}-\d{2} [0-9:.]+ \[\d+\] [IWEFD]/(?:SDK|editor_sdk):',
                re.sub(r'\x1b\[[0-9;]*m', '', line)) for line in lines if line)
        elif re.fullmatch('logs/startup/startup-' + DAY + r'\.log', relative):
            valid = bool(re.match(r'\[' + STAMP + r'\] \[bootstrap \+\d+ms\] bootstrapMainProcess entered', first))
        elif re.fullmatch('logs/startup/' + DAY + r'/\d+-\d{6}\.log', relative):
            valid = bool(re.match(r'\[' + STAMP + r'\] \[\+\d+ms\] === Process started === pid=\d+ argv=', first))
        elif re.fullmatch('logs/startup/' + DAY + r'/\d+-\d{6}\.jsonl', relative) or re.fullmatch(r'logs/perf/worker-startup-\d{8}-\d{6}\.jsonl', relative):
            valid = _marks(lines, cutoff)
        elif re.fullmatch(r'logs/sandbox/\d{8}/sandbox_(?:center|gc)_\d+_\d{3}\.log', relative):
            valid = bool(lines) and all(re.match(r'\[\d{4}/\d{2}/\d{2} [0-9:.]+\]\[\d+\]\[sandbox-(?:center|gc|core)\]\[[IWEFD]\] ', line) for line in lines if line)
    return proof, dependencies, 'qualified_workbuddy_nested_diagnostic' if valid else 'workbuddy_diagnostic_format_unverified'


def discarded_crash_group(root, paths, cutoff, names):
    """Discard a cursor only with every remaining report, freezing membership.

    Resetting a cursor while retaining reports would permit their re-upload.
    Unknown members, age, links and actual file use receive no exception.
    """
    current = {'logs/Crash-Log/' + name for name in names(root, 'logs/Crash-Log')}
    if current != set(paths) or CRASH_CURSOR not in current:
        raise ValueError('diagnostic_cursor_requires_all_reports_explicitly_discarded')
    cursor, body = ff.read_file(root, CRASH_CURSOR, content=True)
    state = _json(body)
    if (cursor['mtime_ns'] >= cutoff or not isinstance(state, dict) or set(state) != {'files'}
            or not isinstance(state['files'], dict)
            or any(type(count) is not int or count < 0 for count in state['files'].values())):
        raise ValueError('diagnostic_cursor_unqualified')
    proofs = [cursor]
    for relative in sorted(current - {CRASH_CURSOR}):
        if not CRASH_REPORT.fullmatch(relative):
            raise ValueError('diagnostic_discard_scope_unqualified')
        proof, _, reason = qualify(root, relative, cutoff, discard_pending=True)
        if reason != 'qualified_workbuddy_nested_diagnostic':
            raise ValueError('diagnostic_discard_scope_unqualified')
        proofs.append(proof)
    return proofs
