from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_agent_record_janitor import frozen_files
from local_agent_record_janitor.environment_inventory import collect_environment_inventory


class EnvironmentInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "User\u6e2c\u8a66"
        self.roaming = self.root / "Roaming"
        self.local = self.root / "Local"
        self.root.mkdir()

    def file(self, relative, content=b"not an executable"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def inventory(self, **kwargs):
        options = dict(user_home=self.root, appdata=self.roaming, local_appdata=self.local,
                       path_value="", environ={})
        options.update(kwargs)
        return collect_environment_inventory(**options)

    def junction(self, target, link):
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, check=False,
            )
            if result.returncode:
                self.skipTest("NTFS junction creation unavailable")
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_native_desktop_and_cindy_candidates_keep_separate_ownership(self):
        stable = self.file("Local/Programs/OpenAI/Codex/bin/codex.exe")
        desktop = self.file("Local/OpenAI/Codex/bin/non-version-hash/codex.exe")
        packaged = self.file("Roaming/CindyGlobal/codex-package/0.166.0/bin/codex.exe")
        legacy = self.file("Roaming/CindyGlobal/codex/0.145.0/codex.exe")
        pi = self.file("Roaming/CindyGlobal/pi/0.85.2/pi.exe")
        self.file("Roaming/CindyGlobal/claude-code/2.1.312/claude.exe")
        self.file("Roaming/CindyGlobal/codex-home/auth.json", b"AUTH_SENTINEL")
        self.file("Roaming/CindyGlobal/pi-agent-home/sessions/conversation.jsonl", b"CHAT_SENTINEL")
        self.file("Roaming/CindyGlobal/claude-home/settings.json", b"CLAUDE_SENTINEL")
        with patch("subprocess.Popen", side_effect=AssertionError("executable run")), patch.object(
            Path, "open", side_effect=AssertionError("body read"),
        ), patch.object(frozen_files, "read_file", side_effect=AssertionError("body read")):
            result = self.inventory(path_value=f'{stable.parent};"{desktop.parent}"',
                                    process_executables=(desktop,))
        self.assertEqual(result["schema_version"], "larj.environment-inventory.v1")
        self.assertIsNotNone(datetime.fromisoformat(result["observed_at"]).utcoffset())
        rows = {row["path"]: row for row in result["runtimes"]}
        self.assertEqual(rows[str(stable)]["client"], "codex")
        self.assertEqual(rows[str(stable)]["path_index"], 0)
        self.assertEqual(rows[str(stable)]["selection"], "unknown")
        self.assertEqual(rows[str(desktop)]["client"], "codex-desktop")
        self.assertEqual(rows[str(desktop)]["selection"], "observed_process_path")
        self.assertIsNone(rows[str(desktop)]["version"])
        self.assertEqual(rows[str(packaged)]["version"], "0.166.0")
        self.assertEqual(rows[str(legacy)]["version"], "0.145.0")
        self.assertEqual(rows[str(pi)]["engine"], "pi")
        self.assertEqual(rows[str(packaged)]["selection"], "unknown")
        cindy = [row for row in result["bindings"] if row["client"] == "cindy"]
        claude = next(row for row in cindy if row["engine"] == "claude")
        self.assertEqual(claude["store_status"], "present")
        self.assertEqual(claude["selection"], "unknown")
        self.assertNotIn("SENTINEL", json.dumps(result))
        self.assertNotIn("deletable", json.dumps(result))

    def test_windows_installer_aliases_point_to_selected_release_without_using_newest(self):
        releases = ".codex/packages/standalone/releases"
        older = self.file(releases + "/0.166.0-x86_64-pc-windows-msvc/bin/codex.exe")
        newer = self.file(releases + "/0.172.0-x86_64-pc-windows-msvc/bin/codex.exe")
        current = self.root / ".codex/packages/standalone/current"
        visible = self.local / "Programs/OpenAI/Codex/bin"
        self.junction(older.parent.parent, current)
        self.junction(current / "bin", visible)

        result = self.inventory(clients=("codex",), path_value=str(visible))
        rows = {row["path"]: row for row in result["runtimes"]}
        selected = rows[str(visible / "codex.exe")]
        self.assertEqual(selected["selection"], "installer_pointer")
        self.assertEqual(selected["path_index"], 0)
        self.assertEqual(selected["version"], "0.166.0")
        self.assertEqual(selected["alias_target_id"], rows[str(current / "bin/codex.exe")]["id"])
        self.assertEqual(rows[str(newer)]["selection"], "unknown")
        self.assertEqual(result["coverage"]["status"], "bounded", result["coverage"])

    def test_unapproved_installer_redirect_is_not_traversed(self):
        outsider = self.file("Other/bin/codex.exe")
        visible = self.local / "Programs/OpenAI/Codex/bin"
        self.junction(outsider.parent, visible)
        original = Path.lstat

        def deny_alias_access(path):
            if visible in path.parents:
                raise AssertionError("inspected a descendant of an unproven redirect")
            return original(path)

        with patch.object(Path, "lstat", deny_alias_access):
            result = self.inventory(clients=("codex",), path_value=str(visible))
        self.assertEqual(result["runtimes"], [])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertIn("unverified_installer_pointer", {issue["code"] for issue in result["coverage"]["issues"]})

    def test_antigravity_runtime_families_and_safe_config_projection(self):
        agy = self.file("Local/agy/bin/agy.exe")
        desktop = self.file("Local/Programs/antigravity/Antigravity.exe")
        acp = self.file(".t3/tools/antigravity-acp/win32-x64/versions/digest/agy_acp_server.exe")
        helper = self.file(".t3/tools/antigravity-acp/win32-x64/versions/digest/localharness_external.exe")
        codex = self.file("Local/Programs/OpenAI/Codex/bin/codex.exe")
        self.file(".gemini/antigravity-cli/settings.json", b"CLI_SECRET")
        self.file(".t3/userdata/providers/antigravity/hash/config/acp_token.json", b"ACP_SECRET")
        self.file(".paseo/config.json", json.dumps({
            "version": 1, "agents": {"providers": {"antigravity": {
                "command": [str(agy), "--private", "ARGS_SECRET"], "env": {"KEY": "ENV_SECRET"},
            }}},
        }).encode())
        self.file(".t3/userdata/settings.json", json.dumps({"providerInstances": {
            "codex": {"driver": "codex", "enabled": False, "config": {
                "binaryPath": str(codex), "homePath": "https://home.example/HOME_SECRET",
                "launchArgs": ["ARG_SECRET"],
            }},
            "antigravity": {"driver": "antigravity", "config": {
                "binaryPath": "https://binary.example/BINARY_SECRET", "token": "SETTINGS_SECRET",
            }},
        }}).encode())
        with patch.object(frozen_files, "read_file", wraps=frozen_files.read_file) as read:
            result = self.inventory()
        self.assertEqual({call.args[1] for call in read.call_args_list}, {"config.json", "userdata/settings.json"})
        rows = {row["path"]: row for row in result["runtimes"]}
        self.assertEqual(rows[str(agy)]["engine"], "antigravity-cli")
        self.assertEqual(rows[str(desktop)]["engine"], "antigravity-desktop")
        self.assertEqual(rows[str(acp)]["engine"], "antigravity-acp")
        self.assertEqual(rows[str(acp)]["companion"], {"path": str(helper), "path_status": "present"})
        hints = {(row["client"], row["engine"]): row for row in result["bindings"]
                 if row["selection"] == "configured_hint"}
        self.assertEqual(hints["paseo", "antigravity-cli"]["executable_id"], rows[str(agy)]["id"])
        self.assertEqual(hints["t3code", "codex"]["executable_id"], rows[str(codex)]["id"])
        self.assertIsNone(hints["t3code", "codex"]["store_path"])
        self.assertFalse(hints["t3code", "codex"]["configured_enabled"])
        self.assertIsNone(hints["t3code", "antigravity-acp"]["executable_id"])
        self.assertNotIn("SECRET", json.dumps(result))


    def test_t3_cli_acp_override_identifies_binary_without_claiming_native_profile(self):
        agy=self.file('Local/agy/bin/agy.exe')
        self.file('.gemini/antigravity-cli/settings.json',b'CLI_SECRET')
        self.file('.t3/userdata/settings.json',json.dumps({'providerInstances':{
            'antigravity':{'driver':'antigravity','enabled':True,'config':{'binaryPath':str(agy)}}
        }}).encode())
        result=self.inventory(clients=('antigravity','t3code'))
        runtime=next(r for r in result['runtimes'] if r['path']==str(agy))
        self.assertEqual(runtime['engine'],'antigravity-cli')
        binding=next(b for b in result['bindings'] if b['client']=='t3code' and b['selection']=='configured_hint')
        self.assertEqual(binding['engine'],'antigravity-acp')
        self.assertEqual(binding['executable_id'],runtime['id'])
        self.assertEqual(binding['configuration']['launch_mode_hint'],'cli_acp')
        self.assertEqual(binding['configuration']['native_cleanup_status'],'unsupported_cli_acp')
        self.assertIsNone(binding['store_path'])
        self.assertEqual(binding['store_scope'],'unknown')
        self.assertNotIn('companion',runtime)
        self.assertNotIn('SECRET',json.dumps(result))

    def test_t3_cli_acp_command_names_do_not_prove_resolution(self):
        for binary in ('agy','antigravity','agy.EXE','agy.cmd','antigravity.bat'):
            with self.subTest(binary=binary):
                self.file('.t3/userdata/settings.json',json.dumps({'providerInstances':{
                    'antigravity':{'driver':'antigravity','config':{'binaryPath':binary}}
                }}).encode())
                result=self.inventory(clients=('t3code',))
                binding=next(b for b in result['bindings'] if b['selection']=='configured_hint')
                self.assertEqual(binding['configuration']['launch_mode_hint'],'cli_acp')
                self.assertIsNone(binding['executable_id'])
                self.assertIsNone(binding['store_path'])

    def test_antigravity_storage_markers_keep_cli_t3_and_paseo_artifacts_separate(self):
        agy = self.file("Local/agy/bin/agy.exe")
        night = self.file("Local/Programs/t3code/T3 Code (Nightly).exe")
        t3_desktop = self.root / "Roaming/t3code-v2"
        t3_desktop.mkdir(parents=True)
        cli = self.root / ".gemini/antigravity-cli"
        t3_profile = self.root / ".t3/userdata/providers/antigravity" / ("a" * 64)
        acp = t3_profile / "antigravity-acp"
        for store in (cli, acp):
            for child in ("brain/PRIVATE_TITLE", "conversations/PRIVATE_ID.db",
                          "conversation_summaries.db", "conversation_summaries.db-wal",
                          "acp_token.json", "settings.json"):
                self.file((store / child).relative_to(self.root), b"PRIVATE_BODY")
        backup = self.root / ".paseo/antigravity-provider.bak"
        self.file((backup / "paseo-plugin.json").relative_to(self.root),
                  b'{"id":"antigravity-provider","label":"PRIVATE_LABEL"}')
        self.file((backup / "index.server.ts").relative_to(self.root), b"PRIVATE_SOURCE")
        self.file(".paseo/config.json", json.dumps({
            "version": 1, "agents": {"providers": {"antigravity": {"command": [str(agy)]}}},
        }).encode())
        scandir = os.scandir

        def no_store_listing(path):
            if Path(path) in (cli, t3_profile, acp, backup) or any(
                p in Path(path).parents for p in (cli, t3_profile, backup)
            ):
                raise AssertionError("inventory enumerated records or plugin source")
            return scandir(path)

        with patch.object(frozen_files, "read_file", wraps=frozen_files.read_file) as read, patch.object(
            os, "scandir", no_store_listing,
        ):
            result = self.inventory(clients=("antigravity", "paseo", "t3code"))
        self.assertEqual({call.args[1] for call in read.call_args_list},
                         {"config.json", "paseo-plugin.json"})
        rows = {row["store_path"]: row for row in result["bindings"]}
        for store in (cli, acp):
            self.assertEqual(rows[str(store)]["store_scope"], "native_profile")
            markers = {marker["kind"]: marker["path_status"] for marker in rows[str(store)]["storage_artifacts"]}
            self.assertEqual(markers["brain"], "present")
            self.assertEqual(markers["conversations"], "present")
            self.assertEqual(markers["summary_catalog"], "present")
            self.assertEqual(markers["summary_catalog_wal"], "present")
            self.assertEqual(markers["summary_catalog_shm"], "missing")
        self.assertEqual(rows[str(acp)]["profile_path"], str(t3_profile))
        self.assertEqual(rows[str(acp)]["selection"], "layout_hint")
        self.assertIsNone(rows[str(acp)]["executable_id"])
        self.assertEqual(rows[str(backup)]["store_scope"], "legacy_plugin_candidate")
        self.assertEqual(rows[str(backup)]["selection"], "unknown")
        self.assertNotIn("storage_artifacts", rows[str(backup)])
        self.assertEqual(rows[str(t3_desktop)]["store_scope"], "frontend_profile")
        self.assertIn(str(night), {row["path"] for row in result["runtimes"]})
        paseo = next(row for row in result["bindings"] if row["client"] == "paseo"
                     and row["engine"] == "antigravity-cli")
        self.assertIsNone(paseo["store_path"])
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertNotIn("deletable", json.dumps(result))

    def test_t3_acp_pointer_requires_matching_manifest_and_files(self):
        platform = ".t3/tools/antigravity-acp/win32-x64"
        active = "a" * 64
        retained = "b" * 64
        self.file(platform + "/active.json", json.dumps({"releaseId": active}).encode())
        release = platform + "/versions/" + active
        exe = self.file(release + "/agy_acp_server.exe", b"acp")
        companion = self.file(release + "/localharness_external.exe", b"helper")
        other = self.file(platform + "/versions/" + retained + "/agy_acp_server.exe")
        self.file(platform + "/versions/" + retained + "/localharness_external.exe")
        self.file(release + "/.install-complete.json", json.dumps({
            "releaseId": active, "version": "1.4.5", "executable": {"name": exe.name, "bytes": 3},
            "harness": {"name": companion.name, "bytes": 6},
        }).encode())
        rows = {row["path"]: row for row in self.inventory(clients=("t3code",))["runtimes"]}
        self.assertEqual(rows[str(exe)]["selection"], "installer_pointer")
        self.assertEqual(rows[str(exe)]["version"], "1.4.5")
        self.assertEqual(rows[str(exe)]["version_source"], "t3_managed_install_manifest")
        self.assertEqual(rows[str(other)]["selection"], "unknown")
        companion.write_bytes(b"changed length")
        result = self.inventory(clients=("t3code",))
        self.assertTrue(all(row["selection"] == "unknown" for row in result["runtimes"]))
        self.assertIn("unverified_installer_pointer", {issue["code"] for issue in result["coverage"]["issues"]})

        self.file(platform + "/active.json", b'{"releaseId":"../PRIVATE_PATH"}')
        result = self.inventory(clients=("t3code",))
        self.assertTrue(all(row["selection"] == "unknown" for row in result["runtimes"]))
        self.assertIn("metadata_schema_unknown", {issue["code"] for issue in result["coverage"]["issues"]})
        self.assertNotIn("PRIVATE_PATH", json.dumps(result))

    def test_redirected_t3_provider_profile_is_never_descended(self):
        profile = self.root / ".t3/userdata/providers/antigravity" / ("c" * 64)
        store = profile / "antigravity-acp"
        store.mkdir(parents=True)
        original = Path.lstat

        def observe(path):
            if profile in path.parents:
                raise AssertionError("descended through redirected profile")
            if path == profile:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x0400)
            return original(path)

        with patch.object(Path, "lstat", observe):
            result = self.inventory(clients=("t3code",))
        row = next(row for row in result["bindings"] if row["store_path"] == str(store))
        self.assertEqual(row["store_status"], "redirected")
        self.assertNotIn("storage_artifacts", row)
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_npm_hardlinks_are_distinguished_from_equal_independent_files(self):
        native = self.file(".local/bin/claude.exe")
        version = self.file(".local/share/claude/versions/2.1.319")
        package = "Roaming/npm/node_modules/@anthropic-ai/claude-code-win32-x64"
        npm = self.file(package + "/claude.exe")
        self.file(package + "/package.json", b'{"name":"@anthropic-ai/claude-code-win32-x64","version":"2.1.320"}')
        alias = self.root / "other-bin/claude.exe"
        alias.parent.mkdir()
        try:
            os.link(npm, alias)
        except OSError:
            self.skipTest("hardlink creation unavailable")
        result = self.inventory(clients=("claude",), path_value=f"{native.parent};{alias.parent}")
        rows = {row["path"]: row for row in result["runtimes"]}
        self.assertEqual(rows[str(version)]["version"], "2.1.319")
        self.assertEqual(rows[str(npm)]["version"], "2.1.320")
        self.assertNotEqual(rows[str(native)]["file_identity"]["file_id"], rows[str(version)]["file_identity"]["file_id"])
        identity = rows[str(npm)]["file_identity"]
        self.assertEqual(identity["nlink"], 2)
        self.assertEqual(set(identity["observed_runtime_ids"]), {rows[str(npm)]["id"], rows[str(alias)]["id"]})
        self.assertFalse(identity["alias_coverage_complete"])
        self.assertEqual({row["code"] for row in result["findings"]}, {
            "same_observed_file", "multiple_path_candidates", "multiple_runtime_candidates",
        })

    def test_unreadable_cindy_package_leaves_legacy_candidate_and_partial_coverage(self):
        self.file("Roaming/CindyGlobal/codex-package/0.166.0/bin/codex.exe")
        legacy = self.file("Roaming/CindyGlobal/codex/0.145.0/codex.exe")
        package = self.roaming / "CindyGlobal/codex-package"
        original = Path.lstat

        def observe(path):
            if package in path.parents:
                raise AssertionError("descended into an unreadable package")
            if path == package:
                raise PermissionError("DO_NOT_EXPORT")
            return original(path)

        with patch.object(Path, "lstat", observe):
            result = self.inventory(clients=("cindy",))
        self.assertEqual([row["path"] for row in result["runtimes"]], [str(legacy)])
        self.assertEqual(result["runtimes"][0]["selection"], "unknown")
        self.assertIn("unreadable", {row["code"] for row in result["coverage"]["issues"]})
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(result))

    def test_redirected_path_parent_is_not_traversed_without_junction_privilege(self):
        known = self.file("Local/OpenAI/Codex/bin/stale/codex.exe")
        redirected = known.parent
        original = Path.lstat

        def observe(path):
            if redirected in path.parents:
                raise AssertionError("descended through a reparse point")
            if path == redirected:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x0400)
            return original(path)

        with patch.object(Path, "lstat", observe):
            result = self.inventory(clients=("codex-desktop",))
        self.assertFalse(result["runtimes"])
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_unknown_schema_relative_remote_and_nonlocal_paths_are_not_guessed(self):
        self.file(".paseo/config.json", b'{"version":99,"agents":{"providers":{"codex":{"command":["NOT_A_PATH"]}}}}')
        self.file(".t3/userdata/settings.json", b'{"token":"DUPLICATE_SECRET","token":"OTHER_SECRET"}')
        result = self.inventory(clients=("paseo", "t3code"), path_value=r"relative;\\host\share")
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["runtimes"], [])
        self.assertFalse(any(row["selection"] == "configured_hint" for row in result["bindings"]))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("NOT_A_PATH", json.dumps(result))
        self.assertNotIn("host", [issue["path"] for issue in result["coverage"]["issues"]])
        with patch.object(Path, "lstat", side_effect=AssertionError("mapped drive touched")):
            remote = self.inventory(clients=("codex",), drive_is_local=lambda _: False)
        self.assertTrue(all(row["code"] == "nonlocal_drive" for row in remote["coverage"]["issues"]))

    def test_injected_environment_does_not_fall_back_to_host_home(self):
        with patch.object(Path, "home", side_effect=AssertionError("host home")):
            empty = collect_environment_inventory(environ={}, clients=("codex",))
            value = collect_environment_inventory(
                environ={"HOME": str(self.root), "USERPROFILE": str(self.root)}, clients=("codex",),
            )
        self.assertIsNone(empty["roots"]["user_home"])
        self.assertEqual(value["roots"]["user_home"], str(self.root))

    def test_directory_budget_does_not_select_an_order_dependent_subset(self):
        self.file("Local/OpenAI/Codex/bin/hash/codex.exe")
        self.file("Local/OpenAI/Codex/bin/another/codex.exe")
        with patch("local_agent_record_janitor.environment_paths.MAX_DIRECTORY_ENTRIES", 1):
            result = self.inventory(clients=("codex-desktop",))
        self.assertEqual(result["runtimes"], [])
        self.assertIn("directory_budget_exceeded", {issue["code"] for issue in result["coverage"]["issues"]})


if __name__ == "__main__":
    unittest.main()
