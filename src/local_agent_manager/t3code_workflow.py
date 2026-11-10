"""Compatibility entry point for T3's shared frontend batch workflow."""
from .frontend_workflow import run_frontend_batch


def run_server_batch(coordinator, *, record_ids, roots=(), authorized=False,
                     out=None, timeout=30, progress_callback=None):
    return run_frontend_batch(coordinator, client="t3code", record_ids=record_ids,
        roots=roots, authorized=authorized, out=out, timeout=timeout,
        progress_callback=progress_callback)
