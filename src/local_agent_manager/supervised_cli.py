"""Wait for a CLI without killing an ambiguous writer on a wall-clock limit.

Idle observations are diagnostic, not success/failure or permission to replay.
A separate child entry samples Python stacks when the GIL is available.
RPC/lock-specific safety deadlines remain owned by their adapters.
"""
import faulthandler
from contextlib import contextmanager
import math
from pathlib import Path
import subprocess
import sys
import time
import threading


@contextmanager
def periodic_stacks(stream, interval):
    """Sample through Python's GIL-protected frame API, never the C watchdog.

    CPython 3.12's dump_traceback_later walks another thread's changing frame
    chain without the GIL and can itself fault. Keep strong frame/code references
    through the public Python API; copy metadata before doing any output I/O.
    A C call holding the GIL can delay this sampler. The external process waiter
    remains responsible for reporting elapsed/progress-idle time in that case.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('positive finite diagnostic interval required')
    stop = threading.Event()
    lock = threading.Lock()

    def dump(label='Python stacks'):
        # Diagnostics cannot replace a writer's result or interrupt its work.
        try:
            with lock:
                rows = [label + '\n']
                frames = sys._current_frames()
                try:
                    for ident, frame in list(frames.items())[:100]:
                        rows.append(f'Thread 0x{ident:x} (most recent call first):\n')
                        for _ in range(100):
                            if frame is None:
                                break
                            code = frame.f_code
                            rows.append(f'  File {code.co_filename!r}, line {frame.f_lineno} in {code.co_name}\n')
                            frame = frame.f_back
                        else:
                            rows.append('  ...\n')
                finally:
                    frames.clear()
                    frame = code = None
                stream.write(''.join(rows))
                stream.flush()
        except Exception:
            pass

    def sample():
        while not stop.wait(interval):
            dump(f'Timeout ({interval:g} seconds); GIL-protected Python sample')

    sampler = threading.Thread(target=sample, name='lam-stack-sampler', daemon=True)
    sampler.start()
    try:
        yield dump
    finally:
        stop.set()
        sampler.join()


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
        # Fatal diagnostics are still useful, but are not a periodic sampler.
        # Do not redirect an already enabled handler whose sink we do not own.
        owns_handler = not faulthandler.is_enabled()
        if owns_handler:
            faulthandler.enable(file=stacks)
        try:
            with periodic_stacks(stacks, float(interval)) as dump:
                try:
                    from .entrypoint import main as cli_main
                    code = cli_main(arguments)
                    if code:
                        dump('Terminal CLI failure')
                    return code
                except BaseException:
                    dump('Terminal CLI exception')
                    raise
        finally:
            if owns_handler:
                faulthandler.disable()


if __name__ == '__main__':
    raise SystemExit(main())
