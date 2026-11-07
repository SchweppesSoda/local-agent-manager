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
