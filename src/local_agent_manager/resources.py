"""Stable resource locations in source, editable and wheel installations."""
from pathlib import Path


def resource_root() -> Path:
    package = Path(__file__).resolve().parent
    source = package.parents[1]
    manifest = source / "resource-manifest.json"
    if manifest.is_file() and (source / "skills/skill-management/SKILL.md").is_file():
        return source
    installed = package / "_resources"
    if not (installed / "docs/agent-operation-contract.md").is_file():
        raise FileNotFoundError("Local Agent Manager resources are incomplete")
    return installed


def describe_resources() -> dict:
    root = resource_root()
    return {
        "root": str(root),
        "skills": {name: str(root / "skills" / name) for name in ("agent-management", "skill-management")},
        "operationContract": str(root / "docs/agent-operation-contract.md"),
        "clientSchema": str(Path(__file__).with_name("skills") / "schemas/client-model.schema.json"),
    }
