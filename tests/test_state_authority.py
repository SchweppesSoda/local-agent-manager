from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap

import json
import os
from importlib import metadata
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import state_paths
from local_agent_manager.operation_store import OperationStoreError
from local_agent_manager.mutation_guard import mutation_roots, mutation_guard, MutationScope, MutationRootLockedError, UnknownMutationError
from tests.test_mutation_guard import basic_action, write_journal


class StateAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "agent"
        self.home.mkdir()

    def test_environment_override_cannot_hide_unknown_mutation(self):
        store = write_journal(self.home, "unknown", basic_action(self.home))
        before = state_paths.manager_state_base()
        with patch.dict(os.environ, {"LAM_STATE_DIR": str(self.root / "other"), "LAM_TEST_STATE_DIR": str(self.root / "other")}):
            self.assertEqual(state_paths.manager_state_base(), before)
            with self.assertRaises(UnknownMutationError):
                with mutation_guard([MutationScope(self.home, None)]):
                    self.fail("unknown mutation was bypassed")
        self.assertTrue(store.plan_path.is_file())
        self.assertFalse((self.root / "other").exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_second_source_checkout_and_child_share_authority(self):
        package = self.root / "second" / "src" / "local_agent_manager"
        package.mkdir(parents=True)
        original = Path(state_paths.__file__).parent
        (package / "__init__.py").write_text('__path__.append(' + repr(str(original)) + ')', encoding="utf-8")
        (package / "state_paths.py").write_bytes((original / "state_paths.py").read_bytes())
        source_metadata = package.parent / "local_agent_manager.egg-info"
        source_metadata.mkdir()
        (source_metadata / "PKG-INFO").write_text("Name: local-agent-manager\nVersion: 0.3.0\n", encoding="utf-8")
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(package.parent), os.environ["PYTHONPATH"]]),
                           LAM_STATE_DIR=str(self.root / "wrong"))
        code = "from local_agent_manager import state_paths as s; import json; print(json.dumps([s.__file__,str(s.manager_state_base())]))"
        result = subprocess.run([sys.executable, "-c", code], cwd=package.parent.parent, env=environment,
                                capture_output=True, text=True, timeout=20, check=True)
        path, base = json.loads(result.stdout)
        self.assertEqual(Path(path), package / "state_paths.py")
        self.assertEqual(Path(base), state_paths.manager_state_base())
        self.assertFalse((self.root / "wrong").exists())

    def test_full_module_preload_cannot_change_test_authority_or_first_write(self):
        loader = unittest.TestLoader()
        suite = loader.discover(str(Path(__file__).resolve().parent), top_level_dir=str(Path(__file__).resolve().parents[1]))
        self.assertFalse(loader.errors)
        self.assertGreater(suite.countTestCases(), 0)
        state_paths.authoritative_checkout.cache_clear()
        self.addCleanup(state_paths.authoritative_checkout.cache_clear)
        self.assertEqual(state_paths.manager_state_base(), bootstrap.CHECKOUT / ".agent-manager-state")
        store = write_journal(self.home, "preloaded", basic_action(self.home))
        self.assertTrue(store.directory.is_relative_to(bootstrap.CHECKOUT))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_test_module_child_reuses_parent_registration_without_cleaning_it(self):
        code = "from tests import bootstrap; from local_agent_manager.state_paths import manager_state_base; print(manager_state_base())"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20, check=True)
        self.assertEqual(Path(result.stdout.strip()), state_paths.manager_state_base())
        self.assertTrue(bootstrap.DIST.is_dir())

    def test_invalid_inherited_test_registration_is_rejected(self):
        environment = dict(os.environ, _LAM_TEST_INSTALLATION=json.dumps({"root": str(self.home), "nonce": "fake"}))
        result = subprocess.run([sys.executable, "-c", "import tests"], env=environment, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_missing_noneditable_and_duplicate_registrations_fail_closed(self):
        state_paths.authoritative_checkout.cache_clear()
        self.addCleanup(state_paths.authoritative_checkout.cache_clear)
        with patch.object(metadata, "distribution", side_effect=metadata.PackageNotFoundError("local-agent-manager")):
            with self.assertRaises(OperationStoreError):
                state_paths.manager_state_base()
        registration = self.root / "installation" / "local_agent_manager-0.3.0.dist-info"
        registration.mkdir(parents=True)
        (registration / "METADATA").write_text("Name: local-agent-manager\nVersion: 0.3.0\n", encoding="utf-8")
        (registration / "direct_url.json").write_text('{"url":"file:///missing"}', encoding="utf-8")
        distribution = metadata.PathDistribution(registration)
        with patch.object(metadata, "distribution", return_value=distribution):
            with self.assertRaisesRegex(OperationStoreError, "editable"):
                state_paths.manager_state_base()
            (registration / "direct_url.json").unlink()
            # A broken installed dist-info cannot fall through to the valid
            # installation that remains later on the real sys.path.
            with self.assertRaisesRegex(OperationStoreError, "editable"):
                state_paths.manager_state_base()
            duplicate = registration.parent / "local_agent_manager-0.4.0.dist-info"
            duplicate.mkdir()
            (duplicate / "METADATA").write_text("Name: local-agent-manager\nVersion: 0.4.0\n", encoding="utf-8")
            with self.assertRaisesRegex(OperationStoreError, "exactly one"):
                state_paths.manager_state_base()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_literal_percent_checkout_path_is_decoded_once(self):
        checkout = self.root / "literal%20checkout"
        checkout.mkdir()
        (checkout / "pyproject.toml").write_text('[project]\nname = "local-agent-manager"\n', encoding="utf-8")
        registration = self.root / "percentage-installation" / "local_agent_manager-0.3.0.dist-info"
        registration.mkdir(parents=True)
        (registration / "METADATA").write_text("Name: local-agent-manager\nVersion: 0.3.0\n", encoding="utf-8")
        (registration / "direct_url.json").write_text(json.dumps({"dir_info": {"editable": True}, "url": checkout.as_uri()}), encoding="utf-8")
        state_paths.authoritative_checkout.cache_clear()
        self.addCleanup(state_paths.authoritative_checkout.cache_clear)
        with patch.object(metadata, "distribution", return_value=metadata.PathDistribution(registration)):
            self.assertEqual(state_paths.manager_state_base(), checkout / ".agent-manager-state")

    def test_optional_leveldb_skips_only_when_unconfigured(self):
        from tests.runtime_support import require_leveldb
        from local_agent_manager import office_leveldb
        missing = self.root / "not-installed"
        failure = office_leveldb.OfficeLevelDBError("dependency_unavailable")
        with patch.dict(os.environ, {}, clear=True), patch.object(office_leveldb, "_runtime_directory", return_value=missing), patch.object(office_leveldb, "runtime", side_effect=failure):
            with self.assertRaises(unittest.SkipTest):
                require_leveldb()
            os.environ["LARJ_LEVELDB_RUNTIME"] = str(missing)
            with self.assertRaises(office_leveldb.OfficeLevelDBError):
                require_leveldb()
            os.environ.pop("LARJ_LEVELDB_RUNTIME")
            missing.mkdir()
            with self.assertRaises(office_leveldb.OfficeLevelDBError):
                require_leveldb()
            missing.rmdir()
            os.environ["LARJ_REQUIRE_LEVELDB_TESTS"] = "1"
            with self.assertRaises(office_leveldb.OfficeLevelDBError):
                require_leveldb()

    def test_store_binding_is_atomic_and_existing_corruption_is_never_accepted(self):
        with mutation_roots([self.home]):
            control = state_paths.control_directory(self.home)
            record = control.parent / "store.json"
            original = record.read_bytes()
            self.assertEqual(record.stat().st_nlink, 1)
            with patch("local_agent_manager.operation_store.atomic_write_json", side_effect=AssertionError("existing binding overwritten")):
                state_paths.record_store_root(self.home, control)
            self.assertEqual(record.read_bytes(), original)
            for value in ('{"schema_version":', json.dumps({"schema_version": state_paths.STORE_RECORD_SCHEMA, "root": str(self.root)})):
                record.write_text(value, encoding="utf-8")
                with self.assertRaises(OperationStoreError):
                    state_paths.record_store_root(self.home, control)
                self.assertEqual(record.read_text(encoding="utf-8"), value)
            record.write_bytes(original)

    def test_store_binding_write_and_publication_failures_leave_no_partial_record(self):
        with mutation_roots([self.home]):
            control = state_paths.control_directory(self.home)
            record = control.parent / "store.json"
            for function in ("fsync", "replace"):
                record.unlink(missing_ok=True)
                before = {entry.name for entry in record.parent.iterdir()}
                with patch.object(state_paths.os, function, side_effect=OSError("injected binding failure")):
                    with self.assertRaisesRegex(OperationStoreError, "binding"):
                        state_paths.record_store_root(self.home, control)
                self.assertFalse(record.exists())
                self.assertEqual({entry.name for entry in record.parent.iterdir()}, before)
            state_paths.record_store_root(self.home, control)
            self.assertEqual(record.stat().st_nlink, 1)

    def test_process_loss_before_or_after_first_binding_publication_is_reentrant(self):
        code = """from pathlib import Path
import os, sys
from unittest.mock import patch
from local_agent_manager import operation_store
from local_agent_manager.mutation_guard import mutation_roots
original = operation_store._atomic_replace_with_retry
def publish(source,destination):
 if destination.name == 'store.json' and sys.argv[2] == 'before': os._exit(73)
 original(source,destination)
 if destination.name == 'store.json' and sys.argv[2] == 'after': os._exit(73)
with patch.object(operation_store,'_atomic_replace_with_retry',publish):
 with mutation_roots([Path(sys.argv[1])]): raise AssertionError('crash boundary missed')
"""
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                home = self.root / stage
                home.mkdir()
                process = subprocess.run([sys.executable, "-c", code, str(home), stage],
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(process.returncode, 73, process.stderr)
                control = state_paths.control_directory(home)
                record = control.parent / "store.json"
                lock = control / "operations" / ".mutation.lock"
                lock_identity = lock.stat().st_ino
                self.assertEqual(record.exists(), stage == "after")
                if record.exists():
                    self.assertEqual(record.stat().st_nlink, 1)
                with mutation_roots([home]):
                    self.assertEqual(state_paths.store_root_for_control(control), home)
                self.assertEqual(record.stat().st_nlink, 1)
                self.assertEqual(lock.stat().st_ino, lock_identity)
                self.assertEqual(list(home.iterdir()), [])

    def test_concurrent_first_binding_is_serialized_by_permanent_os_gate(self):
        code = """from pathlib import Path
import sys
from local_agent_manager.mutation_guard import mutation_roots
with mutation_roots([Path(sys.argv[1])]):
 print('ready',flush=True)
 sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(self.home)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with self.assertRaises(MutationRootLockedError):
                with mutation_roots([self.home]):
                    self.fail("concurrent owner was admitted")
        finally:
            stdout, stderr = process.communicate("done\n", timeout=20)
        self.assertEqual(process.returncode, 0, stderr)
        with mutation_roots([self.home]):
            record = state_paths.control_directory(self.home).parent / "store.json"
            self.assertEqual(record.stat().st_nlink, 1)
            self.assertEqual(state_paths.store_root_for_control(state_paths.control_directory(self.home)), self.home)
        self.assertEqual(list(self.home.iterdir()), [])


class BootstrapRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.environment = dict(os.environ, LOCALAPPDATA=str(self.root / "local"))
        for name in ("_LAM_TEST_INSTALLATION", "LARJ_LEVELDB_RUNTIME", "LARJ_REQUIRE_LEVELDB_TESTS"):
            self.environment.pop(name, None)

    def child(self, arguments, **environment):
        return subprocess.run([sys.executable, *arguments], cwd=Path(__file__).resolve().parents[1],
            env={**self.environment, **environment}, capture_output=True, text=True, timeout=30)

    def test_invalid_optional_runtime_does_not_block_ordinary_test_entrypoints(self):
        for arguments in (("-m", "unittest", "tests.test_core_types"),
                          ("-m", "unittest", "discover", "-s", "tests", "-p", "test_core_types.py"),
                          ("tests/test_core_types.py",)):
            with self.subTest(arguments=arguments):
                result = self.child(arguments, LARJ_LEVELDB_RUNTIME=str(self.root / "missing-runtime"))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("OK", result.stderr)

    def test_related_tests_keep_optional_runtime_failure_and_skip_gates(self):
        arguments = ("-m", "unittest", "tests.test_office_leveldb.OfficeLevelDBTests")
        broken = self.root / "broken" / "local-agent-manager/runtimes/leveldb-v1"
        broken.mkdir(parents=True)
        cases = (
            ({}, True),
            ({"LARJ_LEVELDB_RUNTIME": str(self.root / "missing-runtime")}, False),
            ({"LOCALAPPDATA": str(self.root / "broken")}, False),
            ({"LARJ_REQUIRE_LEVELDB_TESTS": "1"}, False),
        )
        for environment, skipped in cases:
            with self.subTest(environment=environment):
                result = self.child(arguments, **environment)
                if skipped:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("skipped=1", result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("office_leveldb_dependency_unavailable", result.stderr)
                    self.assertNotIn("skipped=", result.stderr)

    def test_default_runtime_location_survives_localappdata_isolation(self):
        runtime = self.root / "local/local-agent-manager/runtimes/leveldb-v1"
        runtime.mkdir(parents=True)
        code = """from tests import bootstrap
import json, os, sys
from local_agent_manager import office_leveldb
from tests.runtime_support import require_leveldb
os.environ['LOCALAPPDATA'] = sys.argv[1]
try:
    require_leveldb()
except office_leveldb.OfficeLevelDBError as exc:
    print(json.dumps([os.environ['LARJ_LEVELDB_RUNTIME'], str(office_leveldb._runtime_directory()), exc.kind]))
else:
    raise AssertionError('damaged optional runtime was accepted')
"""
        result = self.child(("-c", code, str(self.root / "isolated-local")))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout),
            [str(runtime), str(runtime), "office_leveldb_dependency_unavailable"])


if __name__ == "__main__":
    unittest.main()
