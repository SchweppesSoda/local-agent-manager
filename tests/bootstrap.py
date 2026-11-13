"""Install a temporary, real editable-registration fixture before loading tests.

No production selector is patched and no state-directory environment override
exists. Plain unittest discovery, package discovery and direct test scripts all
enter here; ordinary production subprocesses inherit the fixture registration.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent.parent / "src"
_PROJECT = '[project]\nname = "local-agent-manager"\nversion = "0.3.0"\n'
_METADATA = 'Metadata-Version: 2.1\nName: local-agent-manager\nVersion: 0.3.0\n'
_TOKEN_ENV = "_LAM_TEST_INSTALLATION"
_inherited = os.environ.get(_TOKEN_ENV)
_created = not bool(_inherited)
if _inherited:
    token = json.loads(_inherited)
    ROOT = Path(token["root"])
    if (set(token) != {"root", "nonce"} or not ROOT.is_absolute()
            or ROOT.resolve(strict=True) != ROOT
            or not ROOT.name.startswith("lam-tests-")):
        raise RuntimeError("Invalid inherited test installation")
    marker = json.loads((ROOT / "test-installation.json").read_text(encoding="utf-8"))
    if marker != {"schema_version": "lam.test-installation.v1", **token, "source": str(SOURCE)}:
        raise RuntimeError("Inherited test installation binding changed")
else:
    import secrets
    ROOT = Path(tempfile.mkdtemp(prefix="lam-tests-")).resolve()
    token = {"root": str(ROOT), "nonce": secrets.token_hex(32)}
    (ROOT / "test-installation.json").write_text(json.dumps({"schema_version": "lam.test-installation.v1", **token,
        "source": str(SOURCE)}), encoding="utf-8")
    os.environ[_TOKEN_ENV] = json.dumps(token)
CHECKOUT = ROOT / "checkout"
METADATA = ROOT / "installation"
DIST = METADATA / "local_agent_manager-0.3.0.dist-info"
_url = {"dir_info": {"editable": True}, "url": CHECKOUT.as_uri()}
if _created:
    CHECKOUT.mkdir()
    (CHECKOUT / "pyproject.toml").write_text(_PROJECT, encoding="utf-8")
    DIST.mkdir(parents=True)
    (DIST / "METADATA").write_text(_METADATA, encoding="utf-8")
    (DIST / "direct_url.json").write_text(json.dumps(_url), encoding="utf-8")
    (DIST / "RECORD").write_text('', encoding="utf-8")
else:
    if ((CHECKOUT / "pyproject.toml").read_text(encoding="utf-8") != _PROJECT
            or (DIST / "METADATA").read_text(encoding="utf-8") != _METADATA
            or json.loads((DIST / "direct_url.json").read_text(encoding="utf-8")) != _url):
        raise RuntimeError("Inherited test registration changed")

sys.path.insert(0, str(METADATA))
# Include the actual source path so children never need an obsolete package shim.
SOURCE = Path(__file__).resolve().parent.parent / "src"
os.environ["PYTHONPATH"] = os.pathsep.join([str(METADATA), str(SOURCE), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))])
if _created:
    atexit.register(shutil.rmtree, ROOT, ignore_errors=True)

from local_agent_manager import office_leveldb as _level
_runtime = _level._runtime_directory()
if os.environ.get("LARJ_LEVELDB_RUNTIME") or _runtime.exists():
    _node, _dependency, _helper = _level.runtime()
    os.environ["LARJ_LEVELDB_RUNTIME"] = str(_dependency)
