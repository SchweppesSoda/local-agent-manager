from __future__ import annotations

import importlib.util
import io
import json
import yaml
import sys
import tempfile
import textwrap
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "src/local_agent_manager/skills/core.py"
SPEC = importlib.util.spec_from_file_location("local_agent_manager.skills.work_skills_under_test", SCRIPT)
manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manager
SPEC.loader.exec_module(manager)

HEALTHY_SYSTEM_AUDIT = manager.SystemDependencyAudit(
    "C:/Python312/python.exe",
    0,
    {
        "executable": "C:/Python312/python.exe",
        "version": "3.12.10",
        "errors": [],
        "packages": 0,
        "imports": 0,
        "scripts": 0,
    },
)


class RepoFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.skills = root / "skills"
        self.target_codex = root / "targets" / "codex"
        self.target_workbuddy = root / "targets" / "workbuddy"
        self.target_qoderwork = root / "targets" / "qoderwork"
        self.skills.mkdir(parents=True)
        (root / "config").mkdir()

    def write_config(self, *, exclude: list[str] | None = None, import_local: bool = False) -> None:
        excluded = "[]" if not exclude else "[" + ", ".join(exclude) + "]"
        roots = {
            "codex": self.target_codex.as_posix(),
            "workbuddy": self.target_workbuddy.as_posix(),
            "qoderwork": self.target_qoderwork.as_posix(),
        }
        text = f"""
        version: 1
        repo:
          skillsRoot: skills
        defaults:
          importLocal: false
          backupRoot: "{{targetRoot}}_backups"
        targetSets:
          default: [codex, workbuddy]
          both: [codex, workbuddy]
          all: [codex, workbuddy, qoderwork]
        targets:
          codex:
            label: Codex
            root: "{roots['codex']}"
            mount:
              exclude: {excluded}
          workbuddy:
            label: WorkBuddy
            root: "{roots['workbuddy']}"
            importLocal: {str(import_local).lower()}
            mount:
              exclude: {excluded}
          qoderwork:
            label: QoderWork
            root: "{roots['qoderwork']}"
            mount:
              exclude: {excluded}
        """
        (self.root / "config" / "skill-targets.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
        with (self.root / "config" / "skill-targets.yaml").open("a", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump({"sourceAudit": {"forbidDesktopResourcePaths": True, "forbidClientResourcePaths": True, "sharedResourceReferences": {'fixture-a': ['fixture-b/references/professional-research.md'], 'fixture-c': ['fixture-d/SKILL.md', 'fixture-d/references/fee-bill-archive.md'], 'fixture-d': ['fixture-e/SKILL.md', 'fixture-f/SKILL.md', 'fixture-c/SKILL.md', 'fixture-g/references/settlement-reconciliation.md'], 'fixture-h': ['fixture-d/references/contract-settlement-package.md'], 'fixture-i': ['fixture-g/SKILL.md', 'fixture-j/SKILL.md']}}}))

    def add_skill(self, name: str = "alpha", *, body: str = "# Alpha\n") -> Path:
        root = self.skills / name
        (root / "agents").mkdir(parents=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use this test skill for audit fixtures.\n---\n\n{body}",
            encoding="utf-8",
        )
        (root / "agents" / "openai.yaml").write_text(
            "interface:\n"
            '  display_name: "Alpha"\n'
            '  short_description: "Audit fixture skill for tests"\n'
            f'  default_prompt: "Use ${name} to exercise the fixture."\n',
            encoding="utf-8",
        )
        return root

    def options(self, target: str = "workbuddy", **kwargs):
        return manager.RuntimeOptions(
            repo_root=self.root,
            config_path=self.root / "config" / "skill-targets.yaml",
            targets=[target],
            **kwargs,
        )


class TargetSetTests(unittest.TestCase):
    def test_default_and_both_exclude_qoderwork_while_all_includes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            options = fixture.options()
            options.targets = []

            _, default = manager.load_target_configs(options, ["default"])
            _, both = manager.load_target_configs(options, ["both"])
            _, all_targets = manager.load_target_configs(options, ["all"])

            self.assertEqual([t.name for t in default], ["codex", "workbuddy"])
            self.assertEqual([t.name for t in both], ["codex", "workbuddy"])
            self.assertEqual([t.name for t in all_targets], ["codex", "workbuddy", "qoderwork"])


class PlanTests(unittest.TestCase):
    def test_real_skill_conflict_is_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config(import_local=True)
            fixture.add_skill()
            local = fixture.target_workbuddy / "alpha"
            local.mkdir(parents=True)
            (local / "SKILL.md").write_text("local", encoding="utf-8")

            plan = manager.build_plan(fixture.options())
            self.assertIn("REPORT_IMPORT_CONFLICT", [a.kind for a in plan.blockers])

    def test_exclusion_suppresses_link_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config(exclude=["alpha"])
            fixture.add_skill()

            plan = manager.build_plan(fixture.options())
            kinds = [a.kind for a in plan.actions if a.path and a.path.name == "alpha"]
            self.assertIn("EXCLUDED", kinds)
            self.assertNotIn("CREATE_LINK", kinds)

    def test_global_delete_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            options = fixture.options(delete_skills=["alpha"])

            blocked = manager.build_plan(options)
            self.assertIn("GLOBAL_DELETE_NEEDS_CONFIRM", [a.kind for a in blocked.blockers])

            options.confirm_global_delete = True
            confirmed = manager.build_plan(options)
            self.assertIn("GLOBAL_DELETE_REPO_SKILL", [a.kind for a in confirmed.actions])

    def test_wrong_managed_link_can_be_planned_for_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()
            fixture.target_workbuddy.mkdir(parents=True)
            target_path = fixture.target_workbuddy / "alpha"
            target_path.mkdir()
            wrong = fixture.skills / "old-alpha"
            wrong.mkdir()
            options = fixture.options(replace_wrong_links=True)

            real_link_target = manager.link_target

            def fake_link_target(path: Path):
                if path == target_path:
                    return wrong
                return real_link_target(path)

            with mock.patch.object(manager, "link_target", side_effect=fake_link_target):
                plan = manager.build_plan(options)
            repairs = [a for a in plan.actions if a.kind == "REPLACE_WRONG_LINK" and a.path == target_path]
            self.assertTrue(repairs)
            self.assertEqual(repairs[-1].source, source.resolve(strict=False))


class SharedResourceBoundaryTests(unittest.TestCase):
    # Synthetic policy supplied by the source fixture.
    APPROVED = (
        ("fixture-a", "fixture-b/references/professional-research.md"),
        ("fixture-c", "fixture-d/SKILL.md"),
        ("fixture-c", "fixture-d/references/fee-bill-archive.md"),
        ("fixture-d", "fixture-e/SKILL.md"),
        ("fixture-d", "fixture-f/SKILL.md"),
        ("fixture-d", "fixture-c/SKILL.md"),
        ("fixture-d", "fixture-g/references/settlement-reconciliation.md"),
        ("fixture-h", "fixture-d/references/contract-settlement-package.md"),
        ("fixture-i", "fixture-g/SKILL.md"),
        ("fixture-i", "fixture-j/SKILL.md"),
    )

    def shared_fixture(self, root, borrower_name, target):
        fixture = RepoFixture(root)
        fixture.write_config()
        borrower = fixture.add_skill(borrower_name)
        owner = fixture.add_skill(Path(target).parts[0])
        shared = fixture.skills / target
        if shared.name != "SKILL.md":
            shared.parent.mkdir(parents=True, exist_ok=True)
            shared.write_text("# Shared reference\n", encoding="utf-8")
        return fixture, borrower, owner, shared

    def test_reviewed_routes_from_entry_and_nested_anchor(self):
        for borrower_name, target in self.APPROVED:
            with self.subTest(borrower=borrower_name, target=target), tempfile.TemporaryDirectory() as tmp:
                fixture, borrower, _, _ = self.shared_fixture(Path(tmp), borrower_name, target)
                entry = borrower / "SKILL.md"
                entry.write_text(entry.read_text(encoding="utf-8") + f"[route](../{target})\n", encoding="utf-8")
                refs = borrower / "references"
                refs.mkdir()
                (refs / "route.md").write_text(f"[route](../../{target}#scope)\n", encoding="utf-8")
                self.assertEqual(manager.run_content_audit(fixture.root).errors, [])

    def test_shared_target_must_be_a_file_with_an_owner_entry(self):
        for borrower_name, target in self.APPROVED:
            for missing in ("target", "directory", "owner"):
                with self.subTest(borrower=borrower_name, target=target, missing=missing), tempfile.TemporaryDirectory() as tmp:
                    fixture, borrower, owner, shared = self.shared_fixture(Path(tmp), borrower_name, target)
                    entry = borrower / "SKILL.md"
                    entry.write_text(entry.read_text(encoding="utf-8") + f"[route](../{target})\n", encoding="utf-8")
                    if missing == "owner":
                        (owner / "SKILL.md").unlink()
                    else:
                        shared.unlink()
                        if missing == "directory":
                            shared.mkdir()
                    errors = [e for e in manager.run_content_audit(fixture.root).errors if e.skill == borrower_name]
                    self.assertEqual(len(errors), 1)
                    self.assertIn(errors[0].code, {"REFERENCE_ESCAPE", "REFERENCE_MISSING"})

    def test_other_borrower_cannot_reuse_approved_targets(self):
        for _, target in self.APPROVED:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                fixture, borrower, _, _ = self.shared_fixture(Path(tmp), "unreviewed-borrower", target)
                entry = borrower / "SKILL.md"
                entry.write_text(entry.read_text(encoding="utf-8") + f"[route](../{target})\n", encoding="utf-8")
                self.assertEqual([e.code for e in manager.run_content_audit(fixture.root).errors], ["REFERENCE_ESCAPE"])

    def test_neighbour_documents_scripts_and_repo_files_are_not_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, borrower, owner, _ = self.shared_fixture(
                Path(tmp), "fixture-c", "fixture-d/references/fee-bill-archive.md"
            )
            (owner / "references" / "unreviewed.md").write_text("# Other\n", encoding="utf-8")
            (owner / "scripts").mkdir()
            (owner / "scripts" / "other.py").write_text("# Not an approved dependency\n", encoding="utf-8")
            (fixture.root / "outside.md").write_text("# Outside\n", encoding="utf-8")
            entry = borrower / "SKILL.md"
            entry.write_text(entry.read_text(encoding="utf-8") + (
                "[other](../fixture-d/references/unreviewed.md)\n"
                "[script](../fixture-d/scripts/other.py)\n"
                "[outside](../../outside.md)\n"
            ), encoding="utf-8")
            self.assertEqual([e.code for e in manager.run_content_audit(fixture.root).errors], ["REFERENCE_ESCAPE"] * 3)

    def test_written_approved_path_cannot_redirect_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, borrower, _, shared = self.shared_fixture(
                Path(tmp), "fixture-c", "fixture-d/references/fee-bill-archive.md"
            )
            outside = fixture.root / "outside.md"
            outside.write_text("# Outside\n", encoding="utf-8")
            entry = borrower / "SKILL.md"
            entry.write_text(entry.read_text(encoding="utf-8") + "[route](../fixture-d/references/fee-bill-archive.md)\n", encoding="utf-8")
            resolve = manager.resolve_resource_reference
            def redirected(skill_root, source, value):
                target = resolve(skill_root, source, value)
                return outside if target == shared else target
            with mock.patch.object(manager, "resolve_resource_reference", side_effect=redirected):
                self.assertEqual([e.code for e in manager.run_content_audit(fixture.root).errors], ["REFERENCE_ESCAPE"])

    def test_unapproved_written_path_cannot_resolve_to_approved_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, borrower, owner, shared = self.shared_fixture(
                Path(tmp), "fixture-c", "fixture-d/references/fee-bill-archive.md"
            )
            alias = owner / "references" / "alias.md"
            alias.write_text("# Alias\n", encoding="utf-8")
            entry = borrower / "SKILL.md"
            entry.write_text(entry.read_text(encoding="utf-8") + "[route](../fixture-d/references/alias.md)\n", encoding="utf-8")
            resolve = manager.resolve_resource_reference
            def redirected(skill_root, source, value):
                target = resolve(skill_root, source, value)
                return shared if target == alias else target
            with mock.patch.object(manager, "resolve_resource_reference", side_effect=redirected):
                self.assertEqual([e.code for e in manager.run_content_audit(fixture.root).errors], ["REFERENCE_ESCAPE"])


class AuditTests(unittest.TestCase):
    def test_inline_script_arguments_are_not_part_of_resource_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            skill = fixture.add_skill(body=(
                '# Commands\n\nRun `scripts/cli.py INPUT.json`, '
                '`scripts/cli.py --help`, `scripts/cli.py <input.json>`, '
                '`"scripts/my tool.py" "input file.json"`, '
                "`'scripts/my tool.py' --help`, `scripts/run.ps1 -Help`, "
                '`./scripts/run.sh --help`, and `scripts\\run.ps1 -Help`.\n'
                'Read `references/guide with spaces.md`.\n'
            ))
            (skill / "scripts").mkdir()
            for name in ("cli.py", "my tool.py", "run.ps1", "run.sh"):
                (skill / "scripts" / name).write_text("# fixture\n", encoding="utf-8")
            (skill / "references").mkdir()
            (skill / "references" / "guide with spaces.md").write_text("# Guide\n", encoding="utf-8")

            self.assertEqual(manager.run_content_audit(fixture.root).errors, [])

            (skill / "scripts" / "cli.py").unlink()
            errors = manager.run_content_audit(fixture.root).errors
            self.assertEqual([issue.code for issue in errors], ["REFERENCE_MISSING"])
            self.assertIn("scripts/cli.py", errors[0].message)

    def test_script_commands_still_reject_escape_and_missing_placeholder_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill(body=(
                "Run `scripts/../../other.py <input.json>` and "
                "`scripts/missing.py {input}`.\n"
            ))
            errors = manager.run_content_audit(fixture.root).errors
            self.assertEqual(
                {issue.code for issue in errors}, {"REFERENCE_ESCAPE", "REFERENCE_MISSING"}
            )

    def test_reviewed_shared_document_passes_from_entry_and_nested_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            borrower = fixture.add_skill("fixture-a", body=(
                "Read [research](../fixture-b/references/professional-research.md).\n"
            ))
            owner = fixture.add_skill("fixture-b")
            (owner / "references").mkdir()
            shared = owner / "references" / "professional-research.md"
            shared.write_text("# Research\n", encoding="utf-8")
            (borrower / "references").mkdir()
            (borrower / "references" / "map.md").write_text(
                "Read [research](../../fixture-b/references/professional-research.md#scope).\n",
                encoding="utf-8",
            )

            self.assertEqual(manager.run_content_audit(fixture.root).errors, [])

            shared.unlink()
            errors = manager.run_content_audit(fixture.root).errors
            self.assertEqual([issue.code for issue in errors], ["REFERENCE_MISSING"] * 2)

            shared.mkdir()
            errors = manager.run_content_audit(fixture.root).errors
            self.assertEqual([issue.code for issue in errors], ["REFERENCE_MISSING"] * 2)

    def test_shared_document_exception_does_not_allow_other_borrowers_or_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill("alpha", body=(
                "Read [research](../fixture-b/references/professional-research.md).\n"
            ))
            fixture.add_skill("fixture-a", body=(
                "Read [other](../fixture-b/references/other.md) "
                "and [outside](../../outside.md).\n"
            ))
            owner = fixture.add_skill("fixture-b")
            (owner / "references").mkdir()
            for name in ("professional-research.md", "other.md"):
                (owner / "references" / name).write_text("# Reference\n", encoding="utf-8")
            (fixture.root / "outside.md").write_text("# Outside\n", encoding="utf-8")

            errors = manager.run_content_audit(fixture.root).errors
            self.assertEqual([issue.code for issue in errors], ["REFERENCE_ESCAPE"] * 3)

    def test_shared_document_exception_does_not_relax_import_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            borrower = fixture.add_skill("fixture-a", body=(
                "Read [research](../fixture-b/references/professional-research.md).\n"
            ))
            owner = fixture.add_skill("fixture-b")
            (owner / "references").mkdir()
            (owner / "references" / "professional-research.md").write_text("# Research\n", encoding="utf-8")

            with self.assertRaisesRegex(manager.BlockedError, "unsafe or missing resource"):
                manager.validate_import_tree(borrower, "fixture-a")

    def test_valid_skill_and_help_entry_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            skill = fixture.add_skill(body="# Alpha\n\nRun `scripts/cli.py`.\n")
            scripts = skill / "scripts"
            scripts.mkdir()
            (scripts / "cli.py").write_text(
                "import argparse\nparser = argparse.ArgumentParser()\nparser.parse_args()\n",
                encoding="utf-8",
            )

            report = manager.run_content_audit(fixture.root)
            self.assertEqual(report.errors, [])

    def test_optional_metadata_reference_and_forbidden_path_are_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            skill = fixture.add_skill(body="# Alpha\n\nRead `references/missing.md`.\n\nUse dangerouslyDisableSandbox.\n")
            skill_md = skill / "SKILL.md"
            skill_md.write_text(skill_md.read_text(encoding="utf-8").replace("description:", "version: 1\ndescription:"), encoding="utf-8")

            report = manager.run_content_audit(fixture.root)
            error_codes = {issue.code for issue in report.errors}
            warning_codes = {issue.code for issue in report.warnings}
            self.assertTrue({"REFERENCE_MISSING", "SANDBOX_BYPASS"}.issubset(error_codes))
            self.assertNotIn("FRONTMATTER_EXTRA_FIELDS", error_codes)
            self.assertIn("FRONTMATTER_UNKNOWN_FIELDS", warning_codes)

    def test_nested_markdown_resource_references_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            skill = fixture.add_skill()
            references = skill / "references"
            references.mkdir()
            guide = references / "guide.md"
            peer = references / "peer.md"
            peer.write_text("# Peer\n", encoding="utf-8")
            assets = skill / "assets"
            assets.mkdir()
            (assets / "example.txt").write_text("fixture\n", encoding="utf-8")
            guide.write_text(
                "# Guide\n\nRead [the peer](peer.md) and use `assets/example.txt`.\n",
                encoding="utf-8",
            )

            report = manager.run_content_audit(fixture.root)
            self.assertEqual(report.errors, [])

            guide.write_text(
                guide.read_text(encoding="utf-8") + "Read `references/missing.md`.\n",
                encoding="utf-8",
            )
            report = manager.run_content_audit(fixture.root)
            missing = [issue for issue in report.errors if issue.code == "REFERENCE_MISSING"]
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0].path, guide)
            self.assertIn("references/missing.md", missing[0].message)

    def test_agents_openai_metadata_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            skill = fixture.add_skill()
            (skill / "agents" / "openai.yaml").unlink()

            report = manager.run_content_audit(fixture.root)

            self.assertEqual(report.errors, [])

    def test_undeclared_required_dependency_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            skill = fixture.add_skill(body="# Alpha\n\nRun `scripts/cli.py`.\n")
            scripts = skill / "scripts"
            scripts.mkdir()
            (scripts / "cli.py").write_text("import module_that_cannot_exist_42\n", encoding="utf-8")

            report = manager.run_content_audit(fixture.root)
            codes = {issue.code for issue in report.issues}
            self.assertIn("RUNTIME_DEPENDENCY_UNDECLARED", codes)
            self.assertNotIn("HELP_SKIPPED", codes)

    def test_declared_pywin32_aliases_and_xlrd_are_not_reported_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            skill = fixture.add_skill("fixture-f")
            (skill / "requirements.txt").write_text(
                "openpyxl==3.1.5\n"
                'pywin32==312; platform_system == "Windows"\n'
                "xlrd==2.0.2\n",
                encoding="utf-8",
            )
            scripts = skill / "scripts"
            scripts.mkdir()
            (scripts / "fill.py").write_text(
                "def excel_backend():\n"
                "    import pythoncom\n"
                "    import win32com.client\n"
                "    import ntsecuritycon, pywintypes, win32api, win32con, win32process, win32security\n",
                encoding="utf-8",
            )
            (scripts / "dump.py").write_text(
                "def legacy_dump():\n"
                "    import xlrd\n",
                encoding="utf-8",
            )
            report = manager.run_content_audit(fixture.root)

            dependency_issues = {
                issue.message.rsplit(": ", 1)[-1]: issue
                for issue in report.issues
                if issue.skill == "fixture-f"
                and issue.code.startswith("RUNTIME_DEPENDENCY_")
            }
            self.assertEqual(dependency_issues, {})

    def test_pywin32_aliases_still_require_declared_distribution(self):
        for module in ("pythoncom", "win32com", "ntsecuritycon", "pywintypes", "win32api", "win32con", "win32process", "win32security"):
            with self.subTest(module=module):
                self.assertIsNone(manager.declared_distribution_for_import(module, {}))
                self.assertEqual(manager.declared_distribution_for_import(module, {"pywin32": "pywin32"}), "pywin32")
        self.assertIsNone(manager.declared_distribution_for_import("win32_unreviewed", {"pywin32": "pywin32"}))

    def test_doctor_fails_when_content_audit_has_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            skill = fixture.add_skill()
            (skill / "SKILL.md").write_text("# Missing frontmatter\n", encoding="utf-8")
            fixture.target_workbuddy.mkdir(parents=True)
            options = fixture.options()

            with redirect_stdout(io.StringIO()):
                result = manager.run_doctor(
                    options, None, False, HEALTHY_SYSTEM_AUDIT
                )
            self.assertEqual(result, 3)


class SystemDependencyAuditTests(unittest.TestCase):
    def test_resolver_prefers_explicit_system_python_then_path(self):
        lookups: list[str] = []

        def fake_which(value: str):
            lookups.append(value)
            return "C:/Python312/python.exe"

        explicit, explicit_error = manager.resolve_system_python_executable(
            {manager.SYSTEM_PYTHON_ENV: "C:/Chosen/python.exe"}, fake_which
        )
        path_value, path_error = manager.resolve_system_python_executable({}, fake_which)

        self.assertIsNone(explicit_error)
        self.assertIsNone(path_error)
        self.assertTrue(explicit.endswith("Python312\\python.exe"))
        self.assertTrue(path_value.endswith("Python312\\python.exe"))
        self.assertEqual(lookups, ["C:/Chosen/python.exe", "python"])

    def test_manager_launches_checker_with_selected_python_and_clean_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "check_system_python.py").write_text("# fixture\n", encoding="utf-8")
            observed: dict[str, object] = {}

            commands: list[list[str]] = []

            def fake_runner(command, **kwargs):
                commands.append(command)
                observed["command"] = command
                observed["env"] = kwargs["env"]
                payload = {
                    "executable": command[0],
                    "version": "3.12.10",
                    "errors": [],
                    "errorCount": 0,
                    "packages": 1,
                    "imports": 1,
                    "scripts": 1,
                }
                return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

            result = manager.run_system_dependency_audit(
                root,
                environ={
                    "PATH": "fixture",
                    "PYTHONPATH": "C:/Users/test/.codex/runtime",
                    "VIRTUAL_ENV": "C:/manager-venv",
                },
                which=lambda _: "C:/Python312/python.exe",
                runner=fake_runner,
            )

            self.assertEqual(result.returncode, 0)
            command = observed["command"]
            self.assertTrue(command[0].endswith("Python312\\python.exe"))
            self.assertIn("check_system_python.py", command[2])
            self.assertNotEqual(command[0], manager.sys.executable)
            child_env = observed["env"]
            self.assertNotIn("PYTHONPATH", child_env)
            self.assertNotIn("VIRTUAL_ENV", child_env)

            strict_result = manager.run_system_dependency_audit(
                root,
                strict_versions=True,
                environ={"PATH": "fixture"},
                which=lambda _: "C:/Python312/python.exe",
                runner=fake_runner,
            )
            self.assertEqual(strict_result.returncode, 0)
            self.assertNotIn("--strict-versions", commands[0])
            self.assertIn("--strict-versions", commands[1])

    def test_audit_exit_code_and_json_include_system_dependency_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            failure = manager.SystemDependencyAudit(
                "C:/Python312/python.exe",
                3,
                {
                    "executable": "C:/Python312/python.exe",
                    "version": "3.12.10",
                    "errors": ["package is not installed: Example==1.0"],
                    "packages": 1,
                    "imports": 0,
                    "scripts": 0,
                },
            )
            output = io.StringIO()
            with mock.patch.object(
                manager, "run_system_dependency_audit", return_value=failure
            ) as system_check, redirect_stdout(output):
                result = manager.main(
                    [
                        "audit",
                        "--strict-versions",
                        "--repo-root",
                        str(fixture.root),
                        "--config",
                        str(fixture.root / "config" / "skill-targets.yaml"),
                        "--format",
                        "json",
                    ], runtime_audit=True
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(result, 3)
            self.assertEqual(payload["errors"], 1)
            self.assertEqual(
                payload["systemPython"]["selectedExecutable"],
                "C:/Python312/python.exe",
            )
            self.assertIn("package is not installed", payload["systemPython"]["errors"][0])
            system_check.assert_called_once_with(
                fixture.root, strict_versions=True
            )

    def test_doctor_counts_injected_system_dependency_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            options = fixture.options()
            options.output_format = "json"
            plan = manager.Plan(
                repo_root=fixture.root,
                skills_root=fixture.skills,
                targets=[],
                source_skills={},
                manifest={"consumers": {}},
            )
            static_report = manager.AuditReport(fixture.root, fixture.skills, ["alpha"])
            failure = manager.SystemDependencyAudit(
                "C:/Python312/python.exe",
                3,
                {
                    "executable": "C:/Python312/python.exe",
                    "version": "3.12.10",
                    "errors": ["pip check failed: dependency conflict"],
                    "packages": 1,
                    "imports": 0,
                    "scripts": 0,
                },
            )
            output = io.StringIO()
            with mock.patch.object(manager, "build_plan", return_value=plan), mock.patch.object(
                manager, "run_content_audit", return_value=static_report
            ), redirect_stdout(output):
                result = manager.run_doctor(options, None, False, failure)

            payload = json.loads(output.getvalue())
            self.assertEqual(result, 3)
            self.assertEqual(payload["audit"]["errors"], 1)
            self.assertEqual(payload["audit"]["systemPython"]["returnCode"], 3)

    def test_generic_doctor_does_not_execute_source_runtime_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            options = fixture.options()
            options.output_format = "json"
            plan = manager.Plan(
                repo_root=fixture.root,
                skills_root=fixture.skills,
                targets=[],
                source_skills={},
                manifest={"consumers": {}},
            )
            static_report = manager.AuditReport(
                fixture.root, fixture.skills, ["alpha"]
            )
            failure = manager.SystemDependencyAudit(
                "C:/private/python.exe",
                3,
                {
                    "errors": ["private runtime rejected"],
                    "packages": 0,
                    "imports": 0,
                    "scripts": 0,
                },
            )
            with mock.patch.object(
                manager, "run_system_dependency_audit", return_value=failure
            ) as system_check, mock.patch.object(
                manager, "build_plan", return_value=plan
            ), mock.patch.object(
                manager, "run_content_audit", return_value=static_report
            ), redirect_stdout(io.StringIO()):
                result = manager.run_doctor(options, None, False)

            self.assertEqual(result, 0)
            system_check.assert_not_called()


class ClientPythonRuntimeIntegrationTests(unittest.TestCase):
    @staticmethod
    def runtime_model(root: Path):
        policy = mock.Mock(
            context_id="standalone-codex",
            capture_command="work-skills clients capture --target codex",
            python_command="python",
            required_version="3.12",
            context_environment="CODEX_HOME",
            expected_context=root / ".codex",
            allow_unset_context=True,
        )
        discovery = mock.Mock(mount_point_id="shared", priority=100, role="primary")
        adapter = mock.Mock(
            id="codex",
            consumer_ids=("codex",),
            consumer_detection="codex",
            discovery_roots=(discovery,),
            python_runtime_verification=policy,
            runtime_binding=None,
        )
        mount = mock.Mock(discovery_manifest=None,
            id="shared",
            root=root / "shared",
            source_selection="all",
            delivery="shared",
        )
        return manager.ClientModel(3, {"shared": mount}, {}, {"codex": adapter})

    def test_policy_adapter_requires_and_validates_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = self.runtime_model(root)
            options = manager.RuntimeOptions(
                repo_root=root,
                config_path=root / "config.yaml",
                targets=["codex"],
            )
            patches = (
                mock.patch.object(manager, "read_yaml_simple", return_value={"version": 3}),
                mock.patch.object(manager, "_load_v3_model", return_value=model),
                mock.patch.object(manager, "detect_consumers", return_value=()),
            )
            with patches[0], patches[1], patches[2]:
                with self.assertRaises(manager.BlockedError) as missing:
                    manager.run_clients_verify(options, "1.0", True)
            self.assertIn("requires --runtime-evidence", str(missing.exception))

            evidence_path = root / "evidence.json"
            evidence_path.write_text("{}\n", encoding="utf-8")
            with patches[0], patches[1], patches[2], mock.patch.object(
                manager,
                "validate_python_runtime_evidence",
                return_value=("evidence belongs to another client",),
            ):
                with self.assertRaises(manager.BlockedError) as invalid:
                    manager.run_clients_verify(
                        options, "1.0", True, str(evidence_path)
                    )
            self.assertIn("another client", str(invalid.exception))

    def test_valid_runtime_evidence_is_stored_in_per_adapter_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = self.runtime_model(root)
            options = manager.RuntimeOptions(
                repo_root=root,
                config_path=root / "config.yaml",
                targets=["codex"],
            )
            evidence = {"schemaVersion": 1, "adapterId": "codex", "nested": {"x": 1}}
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            target_root = model.mount_points["shared"].root
            manifest = {
                "consumers": {
                    "shared": {
                        "root": str(target_root),
                        "skills": {
                            "alpha": {
                                "deploymentOwned": True,
                                "path": str(target_root / "alpha"),
                                "source": str(root / "skills" / "alpha"),
                            }
                        },
                    }
                }
            }
            detection = manager.ConsumerDetection(
                "codex", "installed", ("codex",), (), "fixture installed"
            )
            writes: list[dict] = []
            with mock.patch.object(
                manager, "read_yaml_simple", return_value={"version": 3}
            ), mock.patch.object(
                manager, "_load_v3_model", return_value=model
            ), mock.patch.object(
                manager, "detect_consumers", return_value=(detection,)
            ), mock.patch.object(
                manager, "validate_python_runtime_evidence", return_value=()
            ), mock.patch.object(
                manager, "validate_python_runtime_evidence_freshness", return_value=()
            ), mock.patch.object(
                manager, "apply_lock", return_value=nullcontext()
            ), mock.patch.object(
                manager, "load_manifest", return_value=manifest
            ), mock.patch.object(
                manager, "direct_link_to", return_value=True
            ), mock.patch.object(
                manager,
                "atomic_write_json",
                side_effect=lambda _path, value: writes.append(value),
            ), redirect_stdout(io.StringIO()):
                result = manager.run_clients_verify(
                    options, "client-1.0", True, str(evidence_path)
                )

            self.assertEqual(result, 0)
            stored = writes[-1]["consumers"]["shared"]["nativeVerifications"]["codex"]
            self.assertEqual(stored["evidence"]["pythonRuntime"], evidence)
            self.assertEqual(
                writes[-1]["consumers"]["shared"]["nativeVerifiedAdapters"],
                ["codex"],
            )

    def test_native_record_revalidates_runtime_and_rejects_legacy_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = self.runtime_model(root)
            target = manager.TargetConfig(
                "shared",
                "Shared",
                root / "shared",
                root / "backups",
                False,
                [],
                [],
                mount_point_id="shared",
                adapter_ids=("codex",),
                consumer_ids=("codex",),
                adapter_consumers={"codex": ("codex",)},
                client_model=model,
            )
            evidence = {
                "method": "manual-native-check",
                "verifiedVersion": "client-1.0",
                "verifiedOn": manager.datetime.date.today().isoformat(),
                "pythonRuntime": {"adapterId": "codex"},
            }
            record = {
                "status": "verified",
                "adapterId": "codex",
                "consumerIds": ["codex"],
                "mountPointId": "shared",
                "root": str(root / "shared"),
                "evidence": evidence,
            }
            with mock.patch.object(
                manager, "validate_python_runtime_evidence", return_value=()
            ), mock.patch.object(
                manager,
                "validate_python_runtime_evidence_freshness",
                side_effect=AssertionError(
                    "stored verification records must not expire by wall-clock age"
                ),
            ):
                self.assertTrue(
                    manager._adapter_native_record_is_current(
                        record, target, "codex", ("codex",)
                    )
                )
            with mock.patch.object(
                manager,
                "validate_python_runtime_evidence",
                return_value=("stale policy fingerprint",),
            ):
                self.assertFalse(
                    manager._adapter_native_record_is_current(
                        record, target, "codex", ("codex",)
                    )
                )
            aggregate = {
                "nativeVerification": {
                    "status": "verified",
                    "adapterIds": ["codex"],
                    "consumerIds": ["codex"],
                    "mountPointId": "shared",
                    "root": str(root / "shared"),
                    "evidence": evidence,
                }
            }
            self.assertFalse(manager.native_verification_is_current(aggregate, target))

    def test_clients_capture_builds_policy_command_with_clean_system_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            (root / "clients").mkdir(parents=True)
            (root / "clients" / "capture_runtime_evidence.py").write_text(
                "# fixture\n", encoding="utf-8"
            )
            model = self.runtime_model(root)
            destination = base / "runtime-evidence.json"
            options = manager.RuntimeOptions(
                repo_root=root,
                config_path=root / "config.yaml",
                targets=["codex"],
            )
            observed: dict[str, object] = {}

            def fake_runner(command, **kwargs):
                observed["command"] = command
                observed["env"] = kwargs["env"]
                output = Path(command[command.index("--output") + 1])
                output.write_text("{}\n", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="written\n", stderr="")

            with mock.patch.object(
                manager, "load_client_model", return_value=model
            ), mock.patch.object(
                manager, "validate_python_runtime_evidence", return_value=()
            ), redirect_stdout(io.StringIO()):
                result = manager.run_clients_capture(
                    options,
                    str(destination),
                    environ={
                        "PATH": "fixture",
                        "CODEX_HOME": str(root / ".codex"),
                        "PYTHONPATH": "C:/Users/test/.codex/private",
                        "VIRTUAL_ENV": "C:/manager-venv",
                    },
                    which=lambda _: "C:/Python312/python.exe",
                    runner=fake_runner,
                )

            self.assertEqual(result, 0)
            command = observed["command"]
            self.assertIn("--context-id", command)
            self.assertIn("standalone-codex", command)
            self.assertIn("--required-version", command)
            self.assertIn("3.12", command)
            self.assertNotEqual(command[0], manager.sys.executable)
            child_env = observed["env"]
            self.assertEqual(child_env["CODEX_HOME"], str(root / ".codex"))
            self.assertNotIn("PYTHONPATH", child_env)
            self.assertNotIn("VIRTUAL_ENV", child_env)

    def test_clients_capture_rejects_repo_internal_and_existing_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            inside = repo / "runtime-evidence.json"
            outside = base / "runtime-evidence.json"
            outside.write_text("preserve me\n", encoding="utf-8")

            with self.assertRaises(manager.BlockedError) as internal:
                manager.validate_runtime_evidence_destination(repo, str(inside))
            self.assertIn("outside the repository", str(internal.exception))

            with self.assertRaises(manager.BlockedError) as relative:
                manager.validate_runtime_evidence_destination(
                    repo, "runtime-evidence.json"
                )
            self.assertIn("new absolute path", str(relative.exception))

            with self.assertRaises(manager.BlockedError) as existing:
                manager.validate_runtime_evidence_destination(repo, str(outside))
            self.assertIn("overwrite existing", str(existing.exception))
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve me\n")

    def test_clients_capture_missing_destination_explains_safe_path_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = manager.RuntimeOptions(
                repo_root=root,
                config_path=root / "config.yaml",
                targets=["codex"],
            )

            with self.assertRaises(manager.ConfigError) as missing:
                manager.run_clients_capture(options, None)

            self.assertIn(
                "new-absolute-path-outside-repository", str(missing.exception)
            )

    def test_clients_verify_rejects_stale_and_future_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = self.runtime_model(root)
            options = manager.RuntimeOptions(
                repo_root=root,
                config_path=root / "config.yaml",
                targets=["codex"],
            )
            now = manager.datetime.datetime(
                2026, 8, 6, 8, 0, tzinfo=manager.datetime.timezone.utc
            )
            patches = (
                mock.patch.object(manager, "read_yaml_simple", return_value={"version": 3}),
                mock.patch.object(manager, "_load_v3_model", return_value=model),
                mock.patch.object(manager, "detect_consumers", return_value=()),
                mock.patch.object(
                    manager, "validate_python_runtime_evidence", return_value=()
                ),
            )
            cases = (
                ("stale", "2026-08-05T07:59:59Z", "stale"),
                ("future", "2026-08-06T08:00:01Z", "in the future"),
            )
            for label, captured_at, expected in cases:
                with self.subTest(label=label):
                    evidence_path = root / f"{label}.json"
                    evidence_path.write_text(
                        json.dumps({"capturedAt": captured_at}), encoding="utf-8"
                    )
                    with patches[0], patches[1], patches[2], patches[3]:
                        with self.assertRaises(manager.BlockedError) as blocked:
                            manager.run_clients_verify(
                                options,
                                "client-1.0",
                                True,
                                str(evidence_path),
                                runtime_evidence_now=now,
                            )
                    self.assertIn(expected, str(blocked.exception))


if __name__ == "__main__":
    unittest.main()
