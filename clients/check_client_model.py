"""Standard-library schema sanity check plus authoritative model validation."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import jsonschema
import yaml

from work_skills_clients import ClientModelError, load_client_model
from local_agent_manager.resources import describe_resources


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Path(describe_resources()["clientSchema"])
CONFIG = REPO_ROOT / "config" / "skill-targets.yaml"
V3_FIELDS = {"version", "mountPoints", "consumers", "adapters"}


def check_schema() -> None:
    with SCHEMA.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("client schema must declare JSON Schema draft 2020-12")
    if set(schema.get("required", [])) != V3_FIELDS:
        raise ValueError("client schema required fields do not match the v3 API")
    if schema.get("properties", {}).get("version", {}).get("const") != 3:
        raise ValueError("client schema must constrain version to 3")
    with CONFIG.open("r", encoding="utf-8") as handle:
        instance = yaml.safe_load(handle) or {}
    jsonschema.Draft202012Validator.check_schema(schema)
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"client model does not match JSON Schema: {details}")


def main() -> int:
    try:
        check_schema()
        model = load_client_model(CONFIG)
    except (ClientModelError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"client model check failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"client model ok: {len(model.mount_points)} mount points, "
        f"{len(model.consumers)} consumers, {len(model.adapters)} adapters"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
