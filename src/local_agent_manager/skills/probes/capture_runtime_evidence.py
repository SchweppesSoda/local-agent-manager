#!/usr/bin/env python3
"""Capture adapter-bound Python evidence from inside a client tool context.

This command is deliberately standard-library-only.  Its JSON must still be
validated against the repository's client model before native verification can
be recorded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__:
    from ..clients.model import PythonRuntimeVerification
    from ..clients.runtime_evidence import RuntimeEvidenceError, capture_python_runtime
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from clients.model import PythonRuntimeVerification
    from clients.runtime_evidence import RuntimeEvidenceError, capture_python_runtime


def write_evidence_exclusive(destination: Path, output: str) -> None:
    """Create one evidence file without following or replacing any entry."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(output)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture the actual Python resolved in one native client context."
    )
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--consumer", action="append", required=True)
    parser.add_argument("--context-id", required=True)
    parser.add_argument("--context-environment", required=True)
    parser.add_argument("--expected-context", required=True)
    parser.add_argument("--allow-unset-context", action="store_true")
    parser.add_argument("--python-command", default="python")
    parser.add_argument("--required-version", default="3.12")
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    policy = PythonRuntimeVerification(
        status="required",
        context_id=args.context_id,
        capture_command="client-context-command",
        python_command=args.python_command,
        required_version=args.required_version,
        context_environment=args.context_environment,
        expected_context=Path(args.expected_context).expanduser(),
        allow_unset_context=args.allow_unset_context,
    )
    try:
        evidence = capture_python_runtime(
            args.adapter,
            tuple(args.consumer),
            policy,
        )
    except RuntimeEvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    output = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        destination = Path(args.output).expanduser()
        try:
            write_evidence_exclusive(destination, output)
        except FileExistsError:
            print(
                f"Refusing to overwrite existing runtime evidence path: {destination}",
                file=sys.stderr,
            )
            return 3
        except OSError as exc:
            print(f"Could not create runtime evidence file {destination}: {exc}", file=sys.stderr)
            return 3
        print(f"Python runtime evidence written: {destination}")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
