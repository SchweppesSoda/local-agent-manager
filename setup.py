"""Build installed resources from their single authoritative source trees."""
from pathlib import Path
import json
import shutil
import re
from urllib.parse import unquote

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildResources(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        manifest = json.loads((root / "resource-manifest.json").read_text(encoding="utf-8"))
        destination = Path(self.build_lib) / "local_agent_manager" / "_resources"
        if destination.exists():
            if destination.is_symlink() or not destination.resolve().is_relative_to(Path(self.build_lib).resolve()):
                raise RuntimeError("Build resource destination is outside its build directory")
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for relative in manifest["directories"]:
            source = root / relative
            if not source.is_dir():
                raise RuntimeError(f"Required resource directory is missing {relative}")
            shutil.copytree(source, destination / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for relative in manifest["files"]:
            source = root / relative
            if not source.is_file():
                raise RuntimeError(f"Required resource file is missing {relative}")
            shutil.copy2(source, destination / relative)
        for required in ("docs/agent-operation-contract.md", "docs/operation-cli.md", "skills/agent-management/SKILL.md", "skills/skill-management/SKILL.md"):
            if not (destination / required).is_file():
                raise RuntimeError(f"Required resource is missing {required}")

        for document in destination.rglob("*.md"):
            for target in re.findall(r"\[[^\]]*\]\(([^)\n]+)\)", document.read_text(encoding="utf-8")):
                target = unquote(target.strip().strip("<>").split("#", 1)[0])
                if not target.endswith(".md") or ":" in target or target.startswith("/"):
                    continue
                resolved = (document.parent / target).resolve()
                if not resolved.is_relative_to(destination.resolve()) or not resolved.is_file():
                    raise RuntimeError(f"Missing local resource reference in {document.relative_to(destination)}")


setup(cmdclass={"build_py": BuildResources})
