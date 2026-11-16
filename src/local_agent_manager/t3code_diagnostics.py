"""Known T3 diagnostic sinks; provider events and terminal records stay excluded."""
import math
import re

from . import frozen_files as ff


_TRACE = re.compile(r'userdata/logs/(?:server|desktop)\.trace\.ndjson(?:\.[1-9][0-9]*)?')
_RENDERER = re.compile(r'userdata/logs/renderer-history\.ndjson(?:\.[1-9][0-9]*)?')
_OTHER = {'userdata/logs/event-loop-stalls.ndjson', 'userdata/logs/relay-client.ndjson',
          'userdata/logs/server-child.log'}


def recognized(relative):
    return bool(_TRACE.fullmatch(relative) or _RENDERER.fullmatch(relative) or relative in _OTHER)


def _number(value):
    return (type(value) is int and 0 <= value < 2**64
            or type(value) is float and math.isfinite(value) and value >= 0)


def _member(value, choices):
    return isinstance(value, str) and value in choices


def _row(relative, value):
    if not isinstance(value, dict):
        return False
    if _TRACE.fullmatch(relative):
        required = {'type', 'kind', 'name', 'traceId', 'spanId', 'startTimeUnixNano',
                    'endTimeUnixNano', 'sampled', 'attributes', 'events', 'links'}
        allowed = required | {'parentSpanId', 'durationMs', 'exit', 'scope', 'resourceAttributes', 'status'}
        return (required <= value.keys() <= allowed
                and _member(value['type'], {'effect-span', 'otlp-span'})
                and _member(value['kind'], {'internal', 'server', 'client', 'producer', 'consumer'})
                and isinstance(value['name'], str)
                and all(isinstance(value[k], str) and re.fullmatch(pattern, value[k]) for k, pattern in
                        [('traceId', r'[0-9a-f]{32}'), ('spanId', r'[0-9a-f]{16}'),
                         ('startTimeUnixNano', r'[0-9]{1,24}'), ('endTimeUnixNano', r'[0-9]{1,24}')])
                and type(value['sampled']) is bool and isinstance(value['attributes'], dict)
                and isinstance(value['events'], list) and isinstance(value['links'], list)
                and (isinstance(value.get('exit'), dict) if value['type'] == 'effect-span' else
                     all(isinstance(value.get(k), dict) for k in ('scope', 'resourceAttributes', 'status'))))
    if _RENDERER.fullmatch(relative):
        required = {'version', 'mainPid', 'mainSessionStartedAtUnixMs', 'timestampUnixMs', 'event',
                    'webContentsId', 'surface', 'rendererPid', 'rendererPidSource', 'rendererCreationTimeMs', 'memory'}
        memory = value.get('memory')
        return (required <= value.keys() <= required | {'tabId'} and value['version'] == 1
                and _member(value['event'], {'created', 'destroyed', 'dom-ready', 'sample', 'identified'})
                and _member(value['surface'], {'main', 'preview'})
                and _member(value['rendererPidSource'], {'current', 'last-known', 'unavailable'})
                and all(_number(value[k]) for k in ('mainPid', 'mainSessionStartedAtUnixMs', 'timestampUnixMs', 'webContentsId'))
                and all(value[k] is None or _number(value[k]) for k in ('rendererPid', 'rendererCreationTimeMs'))
                and ('tabId' not in value or value['tabId'] is None or isinstance(value['tabId'], str))
                and (memory is None or isinstance(memory, dict) and
                     set(memory) == {'kind', 'sampledAtUnixMs', 'workingSetBytes', 'peakWorkingSetBytes'}
                     and isinstance(memory['kind'], str) and all(_number(memory[k]) for k in
                         ('sampledAtUnixMs', 'workingSetBytes', 'peakWorkingSetBytes'))))
    if relative.endswith('event-loop-stalls.ndjson'):
        return (set(value) == {'time', 'delayMaxMs', 'utilization', 'cpuUserMs', 'cpuSystemMs',
                              'majorPageFaults', 'minorPageFaults', 'involuntaryContextSwitches', 'rssMb'}
                and isinstance(value['time'], str) and all(_number(v) for k, v in value.items() if k != 'time'))
    return (set(value) == {'message', 'level', 'timestamp', 'annotations', 'spans', 'fiberId'}
            and _member(value['level'], {'TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'})
            and isinstance(value['message'], (str, list)) and isinstance(value['timestamp'], str)
            and isinstance(value['annotations'], dict) and isinstance(value['spans'], dict)
            and isinstance(value['fiberId'], str))


def qualify(root, relative, cutoff_ns, parse):
    if not recognized(relative):
        return None
    proof, raw = ff.read_file(root, relative, content=True, limit=ff.MAX_REWRITE_BYTES)
    if proof['mtime_ns'] >= cutoff_ns:
        return None
    rows = raw.splitlines()
    if not rows or not all(line and _row(relative, parse(line)) for line in rows):
        return None
    return proof


def require_closed(root, *, census=None):
    # Reuse the existing T3 desktop/server/runtime census without inspecting or
    # changing its native records. Exact file leases remain mandatory afterward.
    from .t3code_lifecycle import require_closed as check
    check({'root': str(root), 'lifecycle': {'binaries': []},
           'binding_manifest': {'desktop_profiles': []}}, census=census)
