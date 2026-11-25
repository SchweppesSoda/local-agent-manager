"""Route independent management domains through the public command."""
from __future__ import annotations
import sys
from pathlib import Path

def main(argv=None):
    if Path(sys.argv[0]).stem.lower() == "local-agent-record-janitor":
        print("This command has been removed. Use local-agent-manager.", file=sys.stderr)
        return 2
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["remote-worker"]:
        from .remote_records import worker
        return worker(sys.stdin.buffer, sys.stdout.buffer)
    if arguments == ["--version"]:
        from . import __version__
        print(__version__)
        return 0
    if arguments and arguments[0] == "skills":
        from .skills.core import cli_main
        return cli_main(arguments[1:])
    if arguments and arguments[0] == "state":
        from .state_maintenance import main as state_main
        return state_main(arguments[1:])
    if arguments and arguments[0] == "maintenance":
        from .artifact_maintenance import main as maintenance_main
        return maintenance_main(arguments[1:])
    if arguments == ["resources"] or arguments == ["resources", "--json"]:
        from .resources import describe_resources
        import json
        print(json.dumps(describe_resources(), ensure_ascii=False, indent=2))
        return 0
    from .cli import main as records_main
    return records_main(arguments)
