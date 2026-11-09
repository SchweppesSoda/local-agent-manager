from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import cindy_memory as memory


class CindyMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="m")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "測試 project"
        self.project.mkdir()
        self.root = self.base / "owners" / "x" / "maker-memory"
        folder = str(self.project).replace("\\", "-").replace("/", "-").replace(":", "-").lstrip("-")
        self.store = self.root / folder
        self.store.mkdir(parents=True)
        self.meta = self.store / "meta.json"
        self.meta.write_text(json.dumps({"absPath": str(self.project)}), encoding="utf-8")
        self.out = self.base / "private-snapshot.md"

    def inspect(self) -> dict:
        return memory.inspect_memory(maker_memory_root=self.root, project=self.project)

    def export(self, *shards: str, out: Path | None = None) -> dict:
        return memory.export_memory(
            maker_memory_root=self.root,
            project=self.project,
            shards=shards,
            out=self.out if out is None else out,
        )

    def write_shard(self, name: str, body: str) -> Path:
        shard = self.store / name
        shard.write_bytes(body.encode("utf-8"))
        return shard

    def assert_error(self, code: str, call, *args, **kwargs) -> None:
        with self.assertRaises(memory.CindyMemoryError) as result:
            call(*args, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_inspect_opens_only_binding_metadata(self) -> None:
        for name in ("project_selected.md", "feedback_legacy_legacy.md", "digest_private.md",
                     "MEMORY.md", "fts.db", "reference_not_a_shard.MD"):
            self.write_shard(name, "SYNTHETIC_DO_NOT_READ")
        original_read = memory.read_file

        def allow_metadata_only(root, relative, **options):
            self.assertEqual(relative, f"{self.store.name}/meta.json")
            return original_read(root, relative, **options)

        with patch.object(memory, "read_file", side_effect=allow_metadata_only):
            report = self.inspect()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["project"], str(self.project))
        self.assertEqual(report["store"], str(self.store))
        self.assertEqual(
            [(row["filename"], row["type"]) for row in report["shards"]],
            [("feedback_legacy_legacy.md", "feedback"), ("project_selected.md", "project")],
        )
        self.assertTrue(all(row["size_bytes"] == len("SYNTHETIC_DO_NOT_READ") for row in report["shards"]))
        self.assertNotIn("SYNTHETIC_DO_NOT_READ", json.dumps(report))

    def test_lossy_path_collision_rejected_before_any_shard_read(self) -> None:
        nested = self.base / "parent" / "child"
        flat = self.base / "parent-child"
        nested.mkdir(parents=True)
        flat.mkdir()
        folder = str(flat).replace("\\", "-").replace("/", "-").replace(":", "-").lstrip("-")
        candidate = self.root / folder
        candidate.mkdir()
        (candidate / "meta.json").write_text(json.dumps({"absPath": str(nested)}), encoding="utf-8")
        (candidate / "project_secret.md").write_text("SYNTHETIC_COLLISION", encoding="utf-8")
        with patch.object(memory, "read_file", wraps=memory.read_file) as reader:
            self.assert_error(
                "project_mismatch", memory.export_memory,
                maker_memory_root=self.root, project=flat,
                shards=("project_secret.md",), out=self.out,
            )
        self.assertEqual([call.args[1] for call in reader.call_args_list], [f"{folder}/meta.json"])
        self.assertFalse(self.out.exists())

    def test_missing_store_and_invalid_metadata_do_not_fall_back(self) -> None:
        different_project = self.base / "different project"
        different_project.mkdir()
        self.assert_error(
            "directory_missing", memory.inspect_memory,
            maker_memory_root=self.root, project=different_project,
        )
        encoded = json.dumps(str(self.project))
        self.meta.write_text(f'{{"absPath": {encoded}, "absPath": {encoded}}}', encoding="utf-8")
        self.assert_error("invalid_metadata", self.inspect)
        self.meta.write_text(f'{{"absPath": {encoded}, "extra": NaN}}', encoding="utf-8")
        self.assert_error("invalid_metadata", self.inspect)
        self.meta.write_text(json.dumps({"absPath": "bot:synthetic"}), encoding="utf-8")
        self.assert_error("project_mismatch", self.inspect)

    def test_export_selected_only_and_fences_recursive_imports(self) -> None:
        raw = "---\r\ntype: project\r\n---\r\n測試 `literal`\r\n````\r\n@../private.md\r\n"
        one = self.write_shard("project_first.md", raw)
        self.write_shard("project_unselected.md", "SYNTHETIC_UNSELECTED")
        self.write_shard("reference_second.md", "SYNTHETIC_SELECTED")
        self.write_shard("digest_internal.md", "SYNTHETIC_DIGEST")
        loader = self.project / "CLAUDE.local.md"
        loader.write_bytes(b"original loader\n")
        with patch.object(memory, "read_file", wraps=memory.read_file) as reader:
            report = self.export("reference_second.md", "project_first.md")
        self.assertEqual(report["status"], "exported")
        self.assertEqual(report["out"], str(self.out))
        names = [Path(call.args[1]).name for call in reader.call_args_list if call.args[0] == self.root]
        self.assertEqual(
            names, ["meta.json", "reference_second.md", "project_first.md", "meta.json"],
        )
        output = self.out.read_bytes()
        self.assertIn(("`````text\n" + raw + "`````\n").encode("utf-8"), output)
        self.assertIn(b"SYNTHETIC_SELECTED", output)
        self.assertNotIn(b"SYNTHETIC_UNSELECTED", output)
        self.assertNotIn(b"SYNTHETIC_DIGEST", output)
        self.assertNotIn(b"SYNTHETIC_SELECTED", json.dumps(report).encode())
        self.assertEqual(report["size_bytes"], len(output))
        self.assertEqual(one.read_bytes(), raw.encode("utf-8"))
        self.assertEqual(loader.read_bytes(), b"original loader\n")

    def test_existing_output_is_not_replaced_or_read_source_bodies(self) -> None:
        self.write_shard("user_note.md", "synthetic")
        self.out.write_bytes(b"keep me")
        with patch.object(memory, "read_file", wraps=memory.read_file) as reader:
            self.assert_error("output_exists", self.export, "user_note.md")
        self.assertTrue(all(Path(call.args[1]).name == "meta.json" for call in reader.call_args_list))
        self.assertEqual(self.out.read_bytes(), b"keep me")

    def test_concurrent_output_creation_never_overwrites(self) -> None:
        self.write_shard("user_note.md", "synthetic")
        operation = "rename" if os.name == "nt" else "link"
        publish = getattr(os, operation)

        def race(source, destination, **options):
            self.out.write_bytes(b"concurrent writer")
            return publish(source, destination, **options)

        with patch.object(memory.os, operation, side_effect=race):
            self.assert_error("output_exists", self.export, "user_note.md")
        self.assertEqual(self.out.read_bytes(), b"concurrent writer")
        self.assertEqual(list(self.base.glob(".cindy-memory-*")), [])

    def test_publication_verification_failure_preserves_output(self) -> None:
        self.write_shard("user_note.md", "synthetic")
        operation = "rename" if os.name == "nt" else "link"
        publish = getattr(os, operation)

        def change_after_publish(source, destination, **options):
            publish(source, destination, **options)
            self.out.write_bytes(b"changed after publication")

        with patch.object(memory.os, operation, side_effect=change_after_publish):
            self.assert_error("output_unverified", self.export, "user_note.md")
        self.assertEqual(self.out.read_bytes(), b"changed after publication")
        self.assertEqual(list(self.base.glob(".cindy-memory-*")), [])

    def test_preparation_file_replacement_is_not_deleted(self) -> None:
        self.write_shard("user_note.md", "synthetic")
        original_read = memory.read_file
        parked = self.base / "parked.md"

        def replace_prepared(root, relative, **options):
            if relative.startswith(".cindy-memory-"):
                preparation = root / relative
                preparation.rename(parked)
                preparation.write_bytes(b"replacement")
            return original_read(root, relative, **options)

        with patch.object(memory, "read_file", side_effect=replace_prepared):
            self.assert_error("output_drift", self.export, "user_note.md")
        [replacement] = self.base.glob(".cindy-memory-*")
        self.assertEqual(replacement.read_bytes(), b"replacement")
        self.assertFalse(self.out.exists())

    def test_shard_drift_during_export_stops_publication(self) -> None:
        earlier = self.write_shard("project_early.md", "old body")
        self.write_shard("project_later.md", "later body")
        original_read = memory.read_file

        def change_earlier_shard(root, relative, **options):
            result = original_read(root, relative, **options)
            if relative.endswith("/project_later.md"):
                earlier.write_bytes(b"changed after the earlier read")
            return result

        with patch.object(memory, "read_file", side_effect=change_earlier_shard):
            self.assert_error("source_drift", self.export, "project_early.md", "project_later.md")
        self.assertFalse(self.out.exists())

    def test_binding_drift_during_export_stops_publication(self) -> None:
        self.write_shard("reference_one.md", "synthetic")
        original_read = memory.read_file

        def change_binding(root, relative, **options):
            result = original_read(root, relative, **options)
            if relative.endswith("/reference_one.md"):
                self.meta.write_text(json.dumps({"absPath": str(self.project), "other": True}), encoding="utf-8")
            return result

        with patch.object(memory, "read_file", side_effect=change_binding):
            self.assert_error("source_drift", self.export, "reference_one.md")
        self.assertFalse(self.out.exists())

    def test_reject_invalid_selections_and_output_paths(self) -> None:
        for selection in (
            (), ("digest_history.md",), ("MEMORY.md",), ("../project_away.md",),
            ("project_item.md:secret",), ("project_*.md",), ("project_item.md", "project_item.md"),
        ):
            with self.subTest(selection=selection):
                self.assert_error("invalid_selection", self.export, *selection)
        self.write_shard("project_item.md", "synthetic")
        rules = self.project / ".claude" / "rules"
        rules.mkdir(parents=True)
        for out in (
            Path("relative.md"), self.store / "inside.md",
            self.project / "CLAUDE.local.md", rules / "automatic.md",
        ):
            with self.subTest(out=out):
                self.assert_error(
                    "invalid_path" if not out.is_absolute() else "invalid_output",
                    self.export, "project_item.md", out=out,
                )
                self.assertFalse(out.exists())

    def test_invalid_utf8_and_budgets_leave_no_snapshot(self) -> None:
        self.write_shard("project_item.md", "synthetic").write_bytes(b"\xff")
        self.assert_error("invalid_utf8", self.export, "project_item.md")
        self.write_shard("project_item.md", "synthetic")
        with patch.object(memory, "_MAX_SHARD_BYTES", 3):
            self.assert_error("budget_exceeded", self.export, "project_item.md")
        with patch.object(memory, "_MAX_SNAPSHOT_BYTES", 3):
            self.assert_error("budget_exceeded", self.export, "project_item.md")
        self.assertFalse(self.out.exists())

    def test_hardlink_shard_rejected(self) -> None:
        original = self.write_shard("unselected.txt", "synthetic")
        alias = self.store / "project_link.md"
        try:
            os.link(original, alias)
        except OSError as error:
            self.skipTest(f"Fixture cannot create hardlinks ({type(error).__name__}).")
        self.assert_error("unsafe_shard", self.inspect)
        self.assert_error("unsafe_shard", self.export, "project_link.md")
        self.assertFalse(self.out.exists())

    def test_reparse_or_symlink_ancestor_rejected(self) -> None:
        alias = self.base / "linked owner"
        owner = self.root.parent
        if os.name == "nt":
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(owner)],
                check=False, capture_output=True,
            )
            if result.returncode:
                self.skipTest("Fixture cannot create NTFS junctions.")
        else:
            alias.symlink_to(owner, target_is_directory=True)
        with patch.object(memory, "read_file", side_effect=AssertionError("should not read source")):
            self.assert_error(
                "unsafe_directory", memory.inspect_memory,
                maker_memory_root=alias / self.root.name, project=self.project,
            )


if __name__ == "__main__":
    unittest.main()
