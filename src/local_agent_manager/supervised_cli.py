"""Wait for a CLI without killing an ambiguous writer on a wall-clock limit.

Idle observations are diagnostic, not success/failure or permission to replay.
A separate child entry emits periodic Python stacks even when its main thread
is blocked. RPC/lock-specific safety deadlines remain owned by their adapters.
"""
import faulthandler
from pathlib import Path
import subprocess
import sys
import time


class WriterStillRunning(RuntimeError):
    """The waiter stopped without proving child exit; recovery is read-only."""
    def __init__(self, pid):
        self.pid = pid
        super().__init__(f'writer_exit_unproven: pid={pid}; do not replay or restore clients over it')


def wait_with_diagnostics(command, *, stdout, stderr, progress_path, observe,
                          idle_seconds=120, poll_seconds=1, **options):
    if idle_seconds <= 0 or poll_seconds <= 0:
        raise ValueError('positive diagnostic intervals required')
    process = subprocess.Popen(command, stdout=stdout, stderr=stderr, **options)
    def report(value):
        try:
            observe(value)
        except Exception:
            pass  # A failed diagnostic sink cannot orphan an admitted writer.
    start = last_change = last_notice = time.monotonic()
    revision = None
    while True:
        try:
            code = process.wait(timeout=poll_seconds)
            report({'status': 'exited', 'pid': process.pid, 'returncode': code,
                     'elapsed_seconds': time.monotonic()-start})
            return subprocess.CompletedProcess(command, code)
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            try:
                stat = Path(progress_path).stat()
                current = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                current = None
            if current != revision:
                revision, last_change = current, now
            if now-last_notice >= idle_seconds:
                report({'status': 'suspected_stall' if now-last_change >= idle_seconds else 'running',
                    'pid': process.pid, 'elapsed_seconds': now-start,
                    'progress_idle_seconds': now-last_change,
                    'writer_terminated': False, 'replay_allowed': False})
                last_notice = now
        except BaseException as exc:
            report({'status': 'writer_exit_unproven', 'pid': process.pid,
                    'writer_terminated': False, 'replay_allowed': False})
            raise WriterStillRunning(process.pid) from exc
    # Deliberately no context-manager/finally kill: observer failure or parent
    # interruption cannot justify killing a mutator or restoring clients over it.


def main():
    # Private child entry; only diagnostics and normal CLI dispatch, no plan
    # generation, mutation policy or alternative recovery logic lives here.
    stack_path, interval, *arguments = sys.argv[1:]
    with Path(stack_path).open('a', encoding='utf-8') as stacks:
        faulthandler.enable(file=stacks)
        faulthandler.dump_traceback_later(float(interval), repeat=True, file=stacks)
        try:
            from .entrypoint import main as cli_main
            code = cli_main(arguments)
            if code:
                # Terminal failures often precede the periodic sample.
                faulthandler.dump_traceback(file=stacks, all_threads=True)
            return code
        except BaseException:
            faulthandler.dump_traceback(file=stacks, all_threads=True)
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
