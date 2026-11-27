"""Time an isolated T3 mixed-history cleanup or the 17-thread handoff rehearsal.

Each run creates new stores and checks the retained records after cold verification.
"""
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import time
import unittest

from tests.test_t3code_cli_rehearsal import ColdCliRehearsal, KEEP, SELECTED
from tests.t3code_support import STAMP, V2
from local_agent_manager.t3code_database import THREAD_TABLES
from local_agent_manager.t3code_store import RELATIVE


def seed_history(db, total):
    families = [table for table in (*THREAD_TABLES, 'orchestration_events')
                if any(col[1] == 'payload_json' for col in db.execute(f'PRAGMA table_info("{table}")'))]
    counts = {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in families}
    extra = total - sum(counts.values())
    if extra < 4:
        raise ValueError(f'history sample needs at least {sum(counts.values()) + 4} rows')
    groups, extra_messages = divmod(extra, 4)
    versions = dict(db.execute("SELECT stream_id, max(stream_version) FROM orchestration_events GROUP BY stream_id"))
    for index in range(groups + extra_messages):
        owner = SELECTED[(index // 12) % len(SELECTED)] if index % 12 == 0 else KEEP
        message = f'bench-message-{index}'
        body = f'Synthetic history entry {index}'
        payload = json.dumps(dict(id=message, threadId=owner, role='assistant', text=body))
        db.execute(f'INSERT INTO {V2}messages(message_id,thread_id,role,streaming,created_at,updated_at,payload_json) '
                   'VALUES(?,?,?,0,?,?,?)', (message, owner, 'assistant', STAMP, STAMP, payload))
        if index >= groups:
            continue
        item = f'bench-item-{index}'
        event = f'bench-event-{index}'
        payload = json.dumps(dict(id=item, threadId=owner, type='assistant_message', status='completed', text=body))
        db.execute(f'INSERT INTO {V2}turn_items(turn_item_id,thread_id,ordinal,type,status,updated_at,payload_json) '
                   "VALUES(?,?,?,'assistant_message','completed',?,?)", (item, owner, index, STAMP, payload))
        db.execute('INSERT INTO orchestration_v2_events(event_id,thread_id,event_type,occurred_at,payload_json) '
                   "VALUES(?,?,'turn-item.updated',?,?)", (event, owner, STAMP, payload))
        versions[owner] = versions.get(owner, 0) + 1
        db.execute('INSERT INTO orchestration_events(event_id,aggregate_kind,stream_id,stream_version,event_type,'
                   "occurred_at,actor_kind,payload_json,metadata_json) VALUES(?,'thread',?,?,'turn-item.updated',?,'system',?,'{}')",
                   (event, owner, versions[owner], STAMP, payload))
    db.commit()
    return {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in families}


def remaining_history(db, families):
    """Compare all columns of every retained history row, including the event mirrors."""
    digest = hashlib.sha256()
    count = 0
    for table in families:
        owner = 'stream_id' if table == 'orchestration_events' else 'thread_id'
        digest.update(table.encode('ascii') + b'\n')
        for row in db.execute(f'SELECT * FROM "{table}" WHERE {owner}=? ORDER BY rowid', (KEEP,)):
            digest.update(json.dumps(row, separators=(',', ':')).encode('utf-8') + b'\n')
            count += 1
    return count, digest.hexdigest()


def history_sample(total):
    case = ColdCliRehearsal('test_cold_mixed_cli_chain')
    try:
        case.setUp()
        path = case.root / RELATIVE
        with closing(sqlite3.connect(path)) as db:
            families = seed_history(db, total)
            case.assertEqual(sum(families.values()), total)
            preserved = remaining_history(db, families)
        plan = case.plan()
        code, applied = case.apply(plan)
        case.assertEqual((code, applied['goal_status']), (0, 'complete'), applied)
        code, verified = case.call('verify', ['operation', 'verify', *case.common])
        case.assertEqual((code, verified['goal_satisfied']), (0, True), verified)
        started = time.perf_counter()
        case.assert_mixed_preserved()
        with closing(sqlite3.connect(path)) as db:
            case.assertEqual(remaining_history(db, families), preserved)
            remaining = sum(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in families)
            # This synthetic store contains only the selected threads and one keeper.
            case.assertEqual(remaining, preserved[0])
        timings = [json.loads(line) for line in (case.evidence/'timings.jsonl').read_text().splitlines()]
        return {'history_rows': total, 'rows_by_family': families, 'selected_threads': len(SELECTED),
                'retained_history_rows': remaining, 'retained_sha256': preserved[1],
                'real_codex_app_server': bool(os.environ.get('LAM_REHEARSAL_CODEX_BINARY')),
                'seconds': {**{row['label']: row['seconds'] for row in timings},
                            'preservation': time.perf_counter() - started}}
    finally:
        case.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--history-rows', type=int, help='benchmark mixed cold cleanup with this total number of history rows')
    args = parser.parse_args()
    if args.runs < 1:
        parser.error('--runs must be positive')
    if args.history_rows is not None and args.history_rows < 1:
        parser.error('--history-rows must be positive')
    if args.history_rows is None and not os.environ.get('LAM_REHEARSAL_CODEX_BINARY'):
        parser.error('LAM_REHEARSAL_CODEX_BINARY must select the copied real runtime for the handoff')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if args.history_rows is not None:
        samples = []
        for index in range(args.runs):
            os.environ['LAM_REHEARSAL_EVIDENCE'] = str(output / f'run-{index + 1}')
            sample = history_sample(args.history_rows)
            samples.append(sample)
            (output/'samples.json').write_text(json.dumps(samples, indent=2), encoding='utf-8')
        summary = {'runs': args.runs, 'sample': samples[-1], 'median_seconds': {
            phase: statistics.median(s['seconds'][phase] for s in samples) for phase in samples[-1]['seconds']}}
        (output/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(summary, indent=2))
        return
    method = 'test_handoff_17_frontend_18_native_records_with_retained_diagnostics'
    samples = []
    for index in range(args.runs):
        folder = output/f'run-{index+1}'
        os.environ['LAM_REHEARSAL_EVIDENCE'] = str(folder)
        started = time.perf_counter()
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([ColdCliRehearsal(method)]))
        if not result.wasSuccessful():
            raise SystemExit(1)
        timings = [json.loads(line) for line in (folder/method/'timings.jsonl').read_text().splitlines()]
        sample = {'total_seconds': time.perf_counter()-started, 'commands': timings}
        samples.append(sample)
        (output/'samples.json').write_text(json.dumps(samples, indent=2), encoding='utf-8')
    summary = {'runs': args.runs, 'targets': {'t3': 17, 'native': 18}, 'retained_threads': 3,
               'real_codex_app_server': True, 'real_chromium_indexeddb': True,
               'production_writes': 0, 'median_total_seconds': statistics.median(s['total_seconds'] for s in samples),
               'median_commands': {row['label']: statistics.median(next(r['seconds'] for r in s['commands']
                   if r['label'] == row['label']) for s in samples) for row in samples[0]['commands']}}
    (output/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
