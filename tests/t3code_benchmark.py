"""Repeat the real isolated 17-thread / 18-native-record handoff rehearsal.

Only synthetic stores and copied runtimes are touched. Each round builds an
independent fixture, runs the same public commands, and verifies preservation.
Timing instrumentation does not alter production scanner/writer behavior.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import time
import unittest

from tests.test_t3code_cli_rehearsal import ColdCliRehearsal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if not os.environ.get('LAM_REHEARSAL_CODEX_BINARY'):
        parser.error('LAM_REHEARSAL_CODEX_BINARY must select the copied real runtime')
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
