"""Exception metadata with controlled policy reasons and no source/locals."""
import os
import traceback


def exception_metadata(exc):
    causes = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        source_path = getattr(exc, 'filename', None) or getattr(exc, 'source_path', None)
        reason = getattr(exc, 'diagnostic_reason', None)
        if isinstance(exc, OSError) and exc.errno:
            reason = os.strerror(exc.errno)
        causes.append({
            'error_type': type(exc).__name__,
            'code': getattr(exc, 'code', None),
            'reason': reason,
            'errno': getattr(exc, 'errno', None),
            'winerror': getattr(exc, 'winerror', None),
            'sqlite_errorname': getattr(exc, 'sqlite_errorname', None),
            'path': str(source_path) if source_path else None,
            'stage': getattr(exc, 'stage', None),
            'stack': [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                      for frame in traceback.extract_tb(exc.__traceback__)],
        })
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return {'code': 'inventory_source_failed', 'causes': causes,
            'captured_at': [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                            for frame in traceback.extract_stack()[:-1]]}
