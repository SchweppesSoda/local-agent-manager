from __future__ import annotations

import importlib.util
import io
import json
from local_agent_manager.resources import describe_resources
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "src/local_agent_manager/skills/core.py"
SPEC = importlib.util.spec_from_file_location("local_agent_manager.skills.work_skills_v3_under_test", SCRIPT)
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

CLIENT_MODEL_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "local_agent_manager"
    / "skills"
    / "clients"
    / "model.py"
)
CLIENT_SPEC = importlib.util.spec_from_file_location(
    "work_skills_client_model_under_test", CLIENT_MODEL_SCRIPT
)
client_model = importlib.util.module_from_spec(CLIENT_SPEC)
sys.modules[CLIENT_SPEC.name] = client_model
CLIENT_SPEC.loader.exec_module(client_model)


def create_real_directory_link(source: Path, target: Path) -> None:
    """Create the platform's real directory-link primitive, without mocks."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if manager.is_windows():
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode:
            raise unittest.SkipTest(f"Windows junction creation unavailable: {proc.stdout}{proc.stderr}")
    else:
        os.symlink(source, target, target_is_directory=True)


class V3RepoFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.skills = root / "skills"
        self.config = root / "config" / "skill-targets.yaml"
        self.targets = {
            "one": root / "targets" / "one",
            "two": root / "targets" / "two",
            "three": root / "targets" / "three",
        }
        self.backups = {
            name: root / "backups" / name for name in self.targets
        }
        self.skills.mkdir(parents=True)
        self.config.parent.mkdir(parents=True)

    def add_skill(self, name: str = "alpha", *, extra_files: int = 0) -> Path:
        skill = self.skills / name
        (skill / "agents").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: Use this fixture to verify work-skills manager safety.\n"
            "---\n\n"
            f"# {name.title()}\n",
            encoding="utf-8",
        )
        (skill / "agents" / "openai.yaml").write_text(
            "interface:\n"
            f"  display_name: \"{name.title()}\"\n"
            '  short_description: "Safety fixture for manager tests"\n'
            f"  default_prompt: \"Use ${name} to exercise the safety fixture.\"\n",
            encoding="utf-8",
        )
        for index in range(extra_files):
            (skill / f"payload-{index}.txt").write_text(
                f"payload {index}\n", encoding="utf-8"
            )
        return skill

    def write_config(
        self,
        *,
        roots: dict[str, Path] | None = None,
        backups: dict[str, Path] | None = None,
        import_targets: set[str] | None = None,
        verified_targets: set[str] | None = None,
    ) -> None:
        actual_roots = self.targets | (roots or {})
        actual_backups = self.backups | (backups or {})
        import_targets = import_targets or set()
        if verified_targets is None:
            verified_targets = {"one", "two", "three"}
        lines = [
            "version: 1",
            "repo:",
            "  skillsRoot: skills",
            "defaults:",
            "  importLocal: false",
            '  backupRoot: "{targetRoot}_backups"',
            "targetSets:",
            "  all: [one, two, three]",
            "targets:",
        ]
        for name in ("one", "two", "three"):
            lines.extend(
                [
                    f"  {name}:",
                    f"    label: Consumer {name}",
                    f'    root: "{actual_roots[name].as_posix()}"',
                    f'    backupRoot: "{actual_backups[name].as_posix()}"',
                    f"    importLocal: {str(name in import_targets).lower()}",
                    f"    verified: {str(name in verified_targets).lower()}",
                    "    mount:",
                    "      exclude: []",
                ]
            )
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def options(self, targets: list[str] | None = None, **kwargs):
        return manager.RuntimeOptions(
            repo_root=self.root,
            config_path=self.config,
            targets=targets or ["one"],
            **kwargs,
        )

    def state_context(self):
        return mock.patch.dict(
            os.environ,
            {"WORK_SKILLS_STATE_DIR": str(self.root / "state")},
        )

    def add_local_skill(self, target: str, name: str = "alpha", *, extra_files: int = 0) -> Path:
        skill = self.targets[target] / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: Use this local fixture to test safe static import.\n"
            "---\n\n"
            f"# Local {name.title()}\n",
            encoding="utf-8",
        )
        for index in range(extra_files):
            (skill / f"local-{index}.txt").write_text(
                f"local payload {index}\n", encoding="utf-8"
            )
        return skill


def apply_mounts(fixture: V3RepoFixture, targets: list[str]) -> None:
    options = fixture.options(targets, yes=True)
    plan = manager.build_plan(options)
    if plan.blockers:
        raise AssertionError([action.line() for action in plan.blockers])
    with redirect_stdout(io.StringIO()):
        manager.apply_plan(plan, options)


def load_fixture_manifest(fixture: V3RepoFixture) -> dict:
    return json.loads(
        (manager.state_dir(fixture.root) / "manifest.json").read_text(encoding="utf-8")
    )


def minimal_client_model_mapping(home: Path) -> dict:
    return {
        "version": 3,
        "mountPoints": {
            "shared": {
                "label": "Shared",
                "root": "${HOME}/.agents/skills",
                "delivery": "shared",
                "priority": 100,
                "linkPolicy": {
                    "windows": "junction",
                    "posix": "symlink",
                    "unmanagedLinks": "preserve",
                },
            }
        },
        "consumers": {
            "alpha": {
                "label": "Alpha",
                "status": "confirmed",
                "detector": {"mode": "any", "commands": ["alpha"]},
                "verification": {
                    "status": "verified",
                    "instructions": "Restart Alpha and list skills.",
                },
            },
            "beta": {
                "label": "Beta",
                "status": "report-only",
                "detector": {
                    "mode": "any",
                    "paths": ["${HOME}/.beta"],
                },
                "verification": {
                    "status": "required",
                    "instructions": "Verify Beta discovery natively.",
                },
            },
        },
        "adapters": {
            "alpha": {
                "label": "Alpha adapter",
                "consumerIds": ["alpha"],
                "mountPointIds": ["shared"],
                "priority": 100,
                "discoveryRoots": [
                    {
                        "mountPointId": "shared",
                        "priority": 100,
                        "role": "primary",
                    }
                ],
                "linkSupport": {
                    "status": "verified",
                    "evidence": {
                        "sourceUrl": "https://example.invalid/alpha",
                        "sourceLevel": "official-docs",
                        "verifiedVersion": "1.0",
                        "verifiedOn": "2026-08-03",
                    },
                },
                "consumerDetection": "alpha",
                "nativeVerification": "verified",
                "management": {
                    "status": "confirmed",
                    "manageByDefault": True,
                    "createWhenAbsent": False,
                },
            },
            "beta": {
                "label": "Beta adapter",
                "consumerIds": ["beta"],
                "mountPointIds": ["shared"],
                "priority": 50,
                "discoveryRoots": [
                    {
                        "mountPointId": "shared",
                        "priority": 100,
                        "role": "shared",
                    }
                ],
                "linkSupport": {
                    "status": "unknown",
                    "evidence": {
                        "sourceUrl": None,
                        "sourceLevel": "local-observation",
                        "verifiedVersion": "unknown",
                        "verifiedOn": "2026-08-03",
                    },
                },
                "consumerDetection": "beta",
                "nativeVerification": "required",
                "management": {
                    "status": "report-only",
                    "manageByDefault": False,
                    "createWhenAbsent": False,
                },
            },
        },
    }


class PathIdentityTests(unittest.TestCase):
    def test_real_direct_link_preserves_lexical_and_final_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            consumer = root / "consumer"
            create_real_directory_link(source, consumer)

            state = manager.inspect_path(consumer)

            self.assertEqual(state.raw, os.fspath(consumer))
            self.assertEqual(
                manager.lexical_norm_path(state.lexical),
                manager.lexical_norm_path(consumer),
            )
            self.assertTrue(state.is_link)
            self.assertTrue(state.direct)
            self.assertFalse(state.broken)
            self.assertEqual(len(state.chain), 1)
            self.assertEqual(manager.norm_path(state.final), manager.norm_path(source))
            self.assertTrue(manager.direct_link_to(consumer, source))

    def test_real_multihop_link_is_not_treated_as_direct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            middle = root / "middle"
            consumer = root / "consumer"
            create_real_directory_link(source, middle)
            create_real_directory_link(middle, consumer)

            state = manager.inspect_path(consumer)

            self.assertTrue(state.is_link)
            self.assertFalse(state.direct)
            self.assertFalse(state.broken)
            self.assertGreaterEqual(len(state.chain), 2)
            self.assertEqual(manager.norm_path(state.final), manager.norm_path(source))
            self.assertFalse(manager.direct_link_to(consumer, source))

    def test_real_broken_link_is_reported_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing"
            consumer = root / "consumer"
            create_real_directory_link(missing, consumer)

            state = manager.inspect_path(consumer)

            self.assertTrue(state.is_link)
            self.assertTrue(state.broken)
            self.assertFalse(state.direct)
            self.assertFalse(manager.direct_link_to(consumer, missing))


class PlanningBoundaryTests(unittest.TestCase):
    def assert_plan_rejected(self, fixture: V3RepoFixture, targets: list[str]) -> None:
        try:
            plan = manager.build_plan(fixture.options(targets))
        except manager.WorkSkillsError as exc:
            self.assertIn("overlap", str(exc).lower())
            return
        overlap_blockers = [
            action
            for action in plan.blockers
            if "OVERLAP" in action.kind or "overlap" in action.message.lower()
        ]
        self.assertTrue(overlap_blockers, "overlapping managed paths were accepted")

    def test_target_root_link_is_not_resolved_away_or_enumerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            backing = fixture.root / "target-backing"
            backing.mkdir()
            sentinel = backing / "DO-NOT-TOUCH.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            create_real_directory_link(backing, fixture.targets["one"])

            options = fixture.options(yes=True)
            with fixture.state_context():
                plan = manager.build_plan(options)

            roots = [target for target in plan.targets if target.name == "one"]
            self.assertEqual(len(roots), 1)
            self.assertEqual(
                manager.lexical_norm_path(roots[0].root),
                manager.lexical_norm_path(fixture.targets["one"]),
            )
            root_link_blockers = [
                action for action in plan.blockers if action.kind == "TARGET_ROOT_LINK"
            ]
            self.assertTrue(root_link_blockers)
            self.assertFalse(
                any(
                    action.path is not None
                    and manager.lexical_norm_path(action.path).startswith(
                        manager.lexical_norm_path(backing) + "/"
                    )
                    for action in plan.actions
                ),
                "planner followed and enumerated the linked target root",
            )
            with fixture.state_context():
                with self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            self.assertTrue(manager.inspect_path(fixture.targets["one"]).is_link)

    def test_source_target_overlap_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config(roots={"one": fixture.skills / "consumer-root"})
            fixture.add_skill()
            self.assert_plan_rejected(fixture, ["one"])

    def test_two_target_roots_with_ancestor_overlap_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            parent = fixture.root / "overlap"
            fixture.write_config(
                roots={"one": parent, "two": parent / "nested"}
            )
            fixture.add_skill()
            self.assert_plan_rejected(fixture, ["one", "two"])

    def test_backup_overlap_with_another_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config(backups={"one": fixture.targets["two"] / "nested"})
            fixture.add_skill()
            self.assert_plan_rejected(fixture, ["one", "two"])

    def test_backup_overlap_with_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config(backups={"one": fixture.skills / "backups"})
            fixture.add_skill()
            self.assert_plan_rejected(fixture, ["one"])

    def test_two_backup_roots_with_ancestor_overlap_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            parent = fixture.root / "backup-overlap"
            fixture.write_config(
                backups={"one": parent, "two": parent / "nested"}
            )
            fixture.add_skill()
            self.assert_plan_rejected(fixture, ["one", "two"])

    def test_unknown_action_is_rejected_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            fixture.targets["one"].mkdir(parents=True)
            options = fixture.options(yes=True)
            with fixture.state_context():
                plan = manager.build_plan(options)
            plan.actions.append(
                manager.Action(
                    "UNRECOGNIZED_V3_ACTION",
                    "one",
                    fixture.targets["one"] / "must-not-exist",
                    destructive=True,
                )
            )

            with fixture.state_context():
                with self.assertRaises(manager.WorkSkillsError):
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)

            self.assertEqual(list(fixture.targets["one"].iterdir()), [])

    def test_stale_plan_is_rejected_without_disturbing_new_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            fixture.targets["one"].mkdir(parents=True)
            options = fixture.options(yes=True)
            with fixture.state_context():
                plan = manager.build_plan(options)
            obstruction = fixture.targets["one"] / "alpha"
            obstruction.mkdir()
            sentinel = obstruction / "new-content.txt"
            sentinel.write_text("created after plan\n", encoding="utf-8")

            with fixture.state_context():
                with self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)

            self.assertTrue(obstruction.is_dir())
            self.assertFalse(manager.inspect_path(obstruction).is_link)
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "created after plan\n"
            )

    def test_manifest_race_makes_plan_stale_before_filesystem_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill("alpha")

            with fixture.state_context():
                apply_mounts(fixture, ["one"])
                fixture.add_skill("beta")
                options = fixture.options(["one"], yes=True)
                plan = manager.build_plan(options)
                raced_manifest = load_fixture_manifest(fixture)
                raced_manifest["externalRevision"] = "changed-after-plan"
                manager.atomic_write_json(
                    manager.state_dir(fixture.root) / "manifest.json",
                    raced_manifest,
                )

                with self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)

            self.assertEqual(
                manager.inspect_path(fixture.targets["one"] / "beta").lstat_kind,
                "missing",
            )


class ConsumerHealthTests(unittest.TestCase):
    def test_apply_created_verified_direct_consumer_is_doctor_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()
            options = fixture.options(["one"], yes=True)

            with fixture.state_context():
                apply_mounts(fixture, ["one"])
                with redirect_stdout(io.StringIO()):
                    result = manager.run_doctor(
                        options, None, False, HEALTHY_SYSTEM_AUDIT
                    )

            self.assertEqual(result, 0)
            self.assertTrue(
                manager.direct_link_to(fixture.targets["one"] / "alpha", source)
            )

    def test_unregistered_direct_link_requires_explicit_adopt_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config(verified_targets=set())
            source = fixture.add_skill()
            fixture.targets["one"].mkdir(parents=True)
            consumer = fixture.targets["one"] / "alpha"
            create_real_directory_link(source, consumer)
            options = fixture.options(["one"], yes=True)

            with fixture.state_context():
                plan = manager.build_plan(options)
                self.assertIn(
                    "UNREGISTERED_EXPECTED_LINK",
                    [action.kind for action in plan.blockers],
                )
                with redirect_stdout(io.StringIO()):
                    self.assertNotEqual(
                        manager.run_doctor(
                            options, None, False, HEALTHY_SYSTEM_AUDIT
                        ),
                        0,
                    )
                adopt_options = fixture.options(
                    ["one"], yes=True, adopt_existing=True
                )
                adopt_plan = manager.build_plan(adopt_options)
                self.assertIn(
                    "VERIFY_CONSUMER",
                    [action.kind for action in adopt_plan.actions],
                )
                with redirect_stdout(io.StringIO()):
                    manager.apply_plan(adopt_plan, adopt_options)
                    self.assertNotEqual(
                        manager.run_doctor(
                            options, None, False, HEALTHY_SYSTEM_AUDIT
                        ),
                        0,
                    )
                manifest = load_fixture_manifest(fixture)

            registered = manifest["consumers"]["one"]["skills"]["alpha"]
            self.assertTrue(registered["deploymentOwned"])
            self.assertTrue(registered["deploymentHealthy"])
            self.assertFalse(registered["nativeVerified"])
            self.assertFalse(manifest["consumers"]["one"]["nativeVerified"])
            self.assertTrue(manager.direct_link_to(consumer, source))

    def test_native_unverified_consumer_does_not_report_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config(verified_targets=set())
            source = fixture.add_skill()
            fixture.targets["one"].mkdir(parents=True)
            create_real_directory_link(source, fixture.targets["one"] / "alpha")
            options = fixture.options(
                ["one"], yes=True, adopt_existing=True
            )

            with fixture.state_context():
                plan = manager.build_plan(options)
                with redirect_stdout(io.StringIO()):
                    manager.apply_plan(plan, options)
                    result = manager.run_doctor(
                        options, None, False, HEALTHY_SYSTEM_AUDIT
                    )
                manifest = load_fixture_manifest(fixture)

            self.assertNotEqual(result, 0)
            self.assertFalse(manifest["consumers"]["one"]["nativeVerified"])

    def test_missing_owned_consumer_makes_doctor_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill()
            options = fixture.options(["one"], yes=True)

            with fixture.state_context():
                apply_mounts(fixture, ["one"])
                manager.remove_link(fixture.targets["one"] / "alpha")
                with redirect_stdout(io.StringIO()):
                    result = manager.run_doctor(
                        options, None, False, HEALTHY_SYSTEM_AUDIT
                    )

            self.assertNotEqual(result, 0)

    def test_broken_owned_consumer_makes_doctor_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()
            fixture.add_skill("beta")
            options = fixture.options(["one"], yes=True)

            with fixture.state_context():
                apply_mounts(fixture, ["one"])
                shutil.rmtree(source)
                state = manager.inspect_path(fixture.targets["one"] / "alpha")
                self.assertTrue(state.broken)
                with redirect_stdout(io.StringIO()):
                    result = manager.run_doctor(
                        options, None, False, HEALTHY_SYSTEM_AUDIT
                    )

            self.assertNotEqual(result, 0)

    def test_multihop_owned_consumer_makes_doctor_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()
            options = fixture.options(["one"], yes=True)

            with fixture.state_context():
                apply_mounts(fixture, ["one"])
                consumer = fixture.targets["one"] / "alpha"
                manager.remove_link(consumer)
                middle = fixture.root / "middle-alpha"
                create_real_directory_link(source, middle)
                create_real_directory_link(middle, consumer)
                self.assertFalse(manager.inspect_path(consumer).direct)
                with redirect_stdout(io.StringIO()):
                    result = manager.run_doctor(
                        options, None, False, HEALTHY_SYSTEM_AUDIT
                    )

            self.assertNotEqual(result, 0)

    def test_wrong_name_repo_link_makes_otherwise_healthy_doctor_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()
            options = fixture.options(["one"], yes=True)

            with fixture.state_context():
                apply_mounts(fixture, ["one"])
                alias = fixture.targets["one"] / "not-alpha"
                create_real_directory_link(source, alias)
                plan = manager.build_plan(options)
                with redirect_stdout(io.StringIO()):
                    result = manager.run_doctor(
                        options, None, False, HEALTHY_SYSTEM_AUDIT
                    )

            wrong_name = [
                action
                for action in plan.actions
                if action.path == alias
                and ("NAME" in action.kind or "name" in action.message.lower())
            ]
            self.assertTrue(wrong_name)
            self.assertNotEqual(result, 0)


class GlobalDeletionTransactionTests(unittest.TestCase):
    def test_global_delete_lists_every_owned_consumer_before_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()

            with fixture.state_context():
                apply_mounts(fixture, ["one", "two", "three"])
                options = fixture.options(
                    ["one"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )
                plan = manager.build_plan(options)

            delete_actions = [
                action
                for action in plan.actions
                if action.kind
                in {"GLOBAL_DELETE_CONSUMER", "GLOBAL_DELETE_REPO_SKILL"}
            ]
            self.assertEqual(
                [action.target for action in delete_actions[:-1]],
                ["one", "two", "three"],
            )
            self.assertTrue(
                all(action.kind == "GLOBAL_DELETE_CONSUMER" for action in delete_actions[:-1])
            )
            self.assertEqual(delete_actions[-1].kind, "GLOBAL_DELETE_REPO_SKILL")
            self.assertEqual(delete_actions[-1].path, source)

    def test_global_delete_preserves_unmanaged_and_backup_lookalike_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()
            trash = fixture.root / "test-trash"
            trash.mkdir()

            with fixture.state_context():
                apply_mounts(fixture, ["one", "two", "three"])
                unmanaged = fixture.targets["one"] / "personal-notes"
                unmanaged.mkdir()
                unmanaged_sentinel = unmanaged / "keep.txt"
                unmanaged_sentinel.write_text("keep unmanaged\n", encoding="utf-8")
                backup_like = fixture.targets["one"] / "alpha.backup-20990101"
                backup_like.mkdir()
                backup_sentinel = backup_like / "keep.txt"
                backup_sentinel.write_text("keep lookalike\n", encoding="utf-8")
                options = fixture.options(
                    ["one"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )
                plan = manager.build_plan(options)
                touched = {
                    manager.lexical_norm_path(action.path)
                    for action in plan.actions
                    if action.destructive and action.path is not None
                }
                self.assertNotIn(manager.lexical_norm_path(unmanaged), touched)
                self.assertNotIn(manager.lexical_norm_path(backup_like), touched)

                def fake_trash(path: Path) -> None:
                    shutil.move(str(path), str(trash / path.name))

                with mock.patch.object(manager, "move_to_trash", side_effect=fake_trash):
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)
                manifest = load_fixture_manifest(fixture)

            for target in ("one", "two", "three"):
                self.assertEqual(
                    manager.inspect_path(fixture.targets[target] / "alpha").lstat_kind,
                    "missing",
                )
            self.assertFalse(source.exists())
            self.assertEqual(
                unmanaged_sentinel.read_text(encoding="utf-8"), "keep unmanaged\n"
            )
            self.assertEqual(
                backup_sentinel.read_text(encoding="utf-8"), "keep lookalike\n"
            )
            self.assertNotIn(
                "alpha", manifest.get("consumers", {}).get("one", {}).get("skills", {})
            )

    def test_unregistered_same_name_content_blocks_delete_and_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()

            with fixture.state_context():
                apply_mounts(fixture, ["one", "two"])
                unmanaged = fixture.targets["three"] / "alpha"
                unmanaged.mkdir(parents=True)
                sentinel = unmanaged / "private.txt"
                sentinel.write_text("not registry owned\n", encoding="utf-8")
                options = fixture.options(
                    ["one"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )
                plan = manager.build_plan(options)
                self.assertIn(
                    "GLOBAL_DELETE_UNREGISTERED_CONSUMER",
                    [action.kind for action in plan.blockers],
                )
                with self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)

            self.assertTrue(source.is_dir())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "not registry owned\n")
            self.assertTrue(manager.direct_link_to(fixture.targets["one"] / "alpha", source))
            self.assertTrue(manager.direct_link_to(fixture.targets["two"] / "alpha", source))

    def test_consumer_removal_failure_rolls_back_every_prior_removal(self):
        class InjectedFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()

            with fixture.state_context():
                apply_mounts(fixture, ["one", "two", "three"])
                manifest_before = load_fixture_manifest(fixture)
                options = fixture.options(
                    ["one"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )
                plan = manager.build_plan(options)
                real_remove = manager.remove_link
                calls = 0

                def fail_second_removal(path: Path) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise InjectedFailure("fail after first consumer removal")
                    real_remove(path)

                with mock.patch.object(
                    manager, "remove_link", side_effect=fail_second_removal
                ):
                    with self.assertRaises((InjectedFailure, manager.TransactionError)):
                        with redirect_stdout(io.StringIO()):
                            manager.apply_plan(plan, options)
                manifest_after = load_fixture_manifest(fixture)

            self.assertEqual(manifest_after, manifest_before)
            self.assertTrue(source.is_dir())
            for target in ("one", "two", "three"):
                self.assertTrue(
                    manager.direct_link_to(fixture.targets[target] / "alpha", source),
                    target,
                )

    def test_global_delete_retains_recovery_bundle_without_calling_os_trash(self):
        class InjectedFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill()

            with fixture.state_context():
                apply_mounts(fixture, ["one", "two", "three"])
                manifest_before = load_fixture_manifest(fixture)
                options = fixture.options(
                    ["one"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )
                plan = manager.build_plan(options)
                with mock.patch.object(
                    manager,
                    "move_to_trash",
                    side_effect=InjectedFailure("recycle-bin commit failed"),
                ) as trash, redirect_stdout(io.StringIO()):
                    manager.apply_plan(plan, options)
                    trash.assert_not_called()
                manifest_after = load_fixture_manifest(fixture)
                journals = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in (fixture.root / "state" / "transactions").glob("*.json")
                ]

            self.assertNotEqual(manifest_after, manifest_before)
            self.assertFalse(source.exists())
            for target in ("one", "two", "three"):
                self.assertEqual(
                    manager._lstat_kind(fixture.targets[target] / "alpha"), "missing"
                )
                self.assertIsNone(
                    manager.manifest_consumer(manifest_after, target, "alpha")
                )
            delete_journal = next(item for item in journals if item["planId"] == plan.plan_id)
            self.assertEqual(delete_journal["status"], "committed")
            recovery = Path(delete_journal["recoveryPath"])
            self.assertTrue((recovery / "sources" / "alpha").is_dir())


class ImportAndReplacementTransactionTests(unittest.TestCase):
    def test_unsafe_import_link_is_blocked_without_any_partial_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config(import_targets={"one"})
            fixture.add_skill("beta")
            local = fixture.add_local_skill("one", "alpha", extra_files=2)
            external = fixture.root / "external"
            external.mkdir()
            unsafe = local / "unsafe-link"
            create_real_directory_link(external, unsafe)
            options = fixture.options(["one"], yes=True)

            with fixture.state_context():
                plan = manager.build_plan(options)
                self.assertIn("IMPORT_LOCAL_SKILL", [action.kind for action in plan.actions])
                with self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)

            self.assertTrue(local.is_dir())
            self.assertFalse(manager.inspect_path(local).is_link)
            self.assertTrue(manager.inspect_path(unsafe).is_link)
            self.assertFalse((fixture.skills / "alpha").exists())
            self.assertEqual(
                manager.inspect_path(fixture.targets["one"] / "beta").lstat_kind,
                "missing",
            )
            self.assertEqual(list(fixture.root.glob(".work-skills-import-*")), [])

    def test_interrupted_import_leaves_no_repo_skill_backup_mount_or_staging(self):
        class InjectedFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config(import_targets={"one"})
            fixture.add_skill("beta")
            local = fixture.add_local_skill("one", "alpha", extra_files=3)
            local_files_before = sorted(
                path.relative_to(local).as_posix()
                for path in local.rglob("*")
                if path.is_file()
            )
            options = fixture.options(["one"], yes=True)
            real_copy = manager.shutil.copy2
            calls = 0

            def fail_second_copy(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise InjectedFailure("interrupt isolated import copy")
                return real_copy(*args, **kwargs)

            with fixture.state_context():
                plan = manager.build_plan(options)
                with mock.patch.object(
                    manager.shutil, "copy2", side_effect=fail_second_copy
                ):
                    with self.assertRaises((InjectedFailure, manager.TransactionError)):
                        with redirect_stdout(io.StringIO()):
                            manager.apply_plan(plan, options)
                manifest = load_fixture_manifest(fixture)

            local_files_after = sorted(
                path.relative_to(local).as_posix()
                for path in local.rglob("*")
                if path.is_file()
            )
            self.assertEqual(local_files_after, local_files_before)
            self.assertFalse(manager.inspect_path(local).is_link)
            self.assertFalse((fixture.skills / "alpha").exists())
            self.assertFalse(fixture.backups["one"].exists())
            self.assertEqual(
                manager.inspect_path(fixture.targets["one"] / "beta").lstat_kind,
                "missing",
            )
            self.assertEqual(list(fixture.root.glob(".work-skills-import-*")), [])
            self.assertEqual(manifest.get("consumers", {}), {})

    def test_wrong_link_replacement_failure_restores_original_link_and_manifest(self):
        class InjectedFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            alpha = fixture.add_skill("alpha")
            beta = fixture.add_skill("beta")
            consumer = fixture.targets["one"] / "alpha"

            with fixture.state_context():
                apply_mounts(fixture, ["one"])
                manager.remove_link(consumer)
                create_real_directory_link(beta, consumer)
                manifest_before = load_fixture_manifest(fixture)
                options = fixture.options(
                    ["one"], yes=True, replace_wrong_links=True
                )
                plan = manager.build_plan(options)
                self.assertIn("REPLACE_WRONG_LINK", [action.kind for action in plan.actions])
                real_create = manager.create_link

                def fail_alpha_replacement(source: Path, target: Path) -> None:
                    if manager.same_path(source, alpha) and target == consumer:
                        raise InjectedFailure("replacement creation failed")
                    real_create(source, target)

                with mock.patch.object(
                    manager, "create_link", side_effect=fail_alpha_replacement
                ):
                    with self.assertRaises((InjectedFailure, manager.TransactionError)):
                        with redirect_stdout(io.StringIO()):
                            manager.apply_plan(plan, options)
                manifest_after = load_fixture_manifest(fixture)

            self.assertEqual(manifest_after, manifest_before)
            self.assertTrue(manager.direct_link_to(consumer, beta))
            self.assertFalse(manager.direct_link_to(consumer, alpha))

    def test_real_directory_replacement_failure_restores_local_directory(self):
        class InjectedFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            fixture.add_skill("alpha")
            local = fixture.add_local_skill("one", "alpha")
            sentinel = local / "private.txt"
            sentinel.write_text("restore me\n", encoding="utf-8")
            options = fixture.options(["one"], yes=True, replace_real_dirs=True)

            with fixture.state_context():
                plan = manager.build_plan(options)
                self.assertIn(
                    "MOVE_REAL_DIR_TO_BACKUP", [action.kind for action in plan.actions]
                )
                with mock.patch.object(
                    manager,
                    "create_link",
                    side_effect=InjectedFailure("mount after backup failed"),
                ):
                    with self.assertRaises((InjectedFailure, manager.TransactionError)):
                        with redirect_stdout(io.StringIO()):
                            manager.apply_plan(plan, options)
                manifest = load_fixture_manifest(fixture)

            self.assertTrue(local.is_dir())
            self.assertFalse(manager.inspect_path(local).is_link)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "restore me\n")
            self.assertEqual(manifest.get("consumers", {}), {})

    def test_root_link_replacement_failure_restores_original_link_and_sentinel(self):
        class InjectedFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            source = fixture.add_skill("alpha")
            backing = fixture.root / "legacy-root"
            backing.mkdir()
            sentinel = backing / "keep.txt"
            sentinel.write_text("legacy content\n", encoding="utf-8")
            create_real_directory_link(backing, fixture.targets["one"])
            options = fixture.options(["one"], yes=True, replace_root_link=True)

            with fixture.state_context():
                plan = manager.build_plan(options)
                self.assertIn(
                    "REPLACE_TARGET_ROOT_LINK", [action.kind for action in plan.actions]
                )
                real_create = manager.create_link

                def fail_skill_mount(link_source: Path, target: Path) -> None:
                    if manager.same_path(link_source, source):
                        raise InjectedFailure("mount into replaced root failed")
                    real_create(link_source, target)

                with mock.patch.object(
                    manager, "create_link", side_effect=fail_skill_mount
                ):
                    with self.assertRaises((InjectedFailure, manager.TransactionError)):
                        with redirect_stdout(io.StringIO()):
                            manager.apply_plan(plan, options)

            self.assertTrue(manager.direct_link_to(fixture.targets["one"], backing))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "legacy content\n")

    def test_root_link_replacement_success_keeps_backing_and_mounts_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = V3RepoFixture(Path(tmp))
            fixture.write_config()
            alpha = fixture.add_skill("alpha")
            beta = fixture.add_skill("beta")
            backing = fixture.root / "legacy-root"
            backing.mkdir()
            sentinel = backing / "keep.txt"
            sentinel.write_text("legacy content\n", encoding="utf-8")
            legacy_alpha = backing / "alpha"
            create_real_directory_link(alpha, legacy_alpha)
            create_real_directory_link(backing, fixture.targets["one"])
            options = fixture.options(["one"], yes=True, replace_root_link=True)

            with fixture.state_context():
                plan = manager.build_plan(options)
                kinds = [action.kind for action in plan.actions]
                replacement_index = kinds.index("REPLACE_TARGET_ROOT_LINK")
                post_root_links = [
                    (index, action)
                    for index, action in enumerate(plan.actions)
                    if action.kind == "CREATE_LINK"
                    and action.data.get("postRootReplacement") is True
                ]
                self.assertTrue(post_root_links)
                self.assertTrue(
                    all(index > replacement_index for index, _action in post_root_links)
                )
                with redirect_stdout(io.StringIO()):
                    manager.apply_plan(plan, options)

            new_root = fixture.targets["one"]
            self.assertEqual(manager.inspect_path(new_root).lstat_kind, "directory")
            self.assertFalse(manager.inspect_path(new_root).is_link)
            self.assertTrue(manager.direct_link_to(new_root / "alpha", alpha))
            self.assertTrue(manager.direct_link_to(new_root / "beta", beta))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "legacy content\n")
            self.assertTrue(manager.direct_link_to(legacy_alpha, alpha))


class ClientModelV3Tests(unittest.TestCase):

    def test_multiple_consumers_share_one_mount_point_without_duplicate_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            model = client_model.load_client_model(
                minimal_client_model_mapping(home), environ={"HOME": str(home)}
            )

            self.assertEqual(set(model.mount_points), {"shared"})
            self.assertEqual(model.adapters["alpha"].mount_point_ids, ("shared",))
            self.assertEqual(model.adapters["beta"].mount_point_ids, ("shared",))
            self.assertEqual(
                model.mount_points["shared"].root,
                home / ".agents" / "skills",
            )
            self.assertEqual(model.default_adapter_ids, ("alpha",))

    def test_uninstalled_consumer_detection_is_read_only_and_creates_no_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            model = client_model.load_client_model(
                minimal_client_model_mapping(home), environ={"HOME": str(home)}
            )
            mount_root = model.mount_points["shared"].root
            beta_detector_path = model.consumers["beta"].detector.paths[0]
            self.assertFalse(mount_root.exists())
            self.assertFalse(beta_detector_path.exists())

            observed_paths = []

            def read_only_path_probe(path: Path) -> bool:
                observed_paths.append(path)
                return False

            detections = client_model.detect_consumers(
                model,
                command_lookup=lambda command: (
                    str(home / "bin" / command) if command == "alpha" else None
                ),
                path_exists=read_only_path_probe,
            )

            by_id = {detection.consumer_id: detection for detection in detections}
            self.assertEqual(by_id["alpha"].state, "installed")
            self.assertEqual(by_id["beta"].state, "not_installed")
            self.assertEqual(observed_paths, [beta_detector_path])
            self.assertFalse(mount_root.exists())
            self.assertFalse(beta_detector_path.exists())
            self.assertTrue(
                all(not adapter.create_when_absent for adapter in model.adapters.values())
            )

    def test_partial_all_detector_and_explicit_ambiguous_policy_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            raw = minimal_client_model_mapping(home)
            raw["consumers"]["alpha"]["detector"] = {
                "mode": "all",
                "commands": ["alpha"],
                "paths": ["${HOME}/.alpha"],
            }
            raw["consumers"]["beta"]["status"] = "ambiguous"
            model = client_model.load_client_model(
                raw, environ={"HOME": str(home)}
            )

            detections = client_model.detect_consumers(
                model,
                command_lookup=lambda command: str(home / "bin" / command),
                path_exists=lambda path: path.name == ".beta",
            )
            by_id = {detection.consumer_id: detection for detection in detections}

            self.assertEqual(by_id["alpha"].state, "ambiguous")
            self.assertEqual(by_id["beta"].state, "ambiguous")

    def test_invalid_cross_reference_and_create_when_absent_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            raw = minimal_client_model_mapping(home)
            raw["adapters"]["alpha"]["mountPointIds"] = ["missing"]
            raw["adapters"]["beta"]["management"]["createWhenAbsent"] = True

            with self.assertRaises(client_model.ClientModelError) as caught:
                client_model.load_client_model(raw, environ={"HOME": str(home)})

            errors = "\n".join(caught.exception.errors)
            self.assertIn("unknown mount point", errors)
            self.assertIn("discoveryRoots", errors)
            self.assertIn("createWhenAbsent", errors)

    def test_nonconfirmed_adapter_cannot_be_default_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            raw = minimal_client_model_mapping(home)
            raw["adapters"]["beta"]["management"]["manageByDefault"] = True

            with self.assertRaises(client_model.ClientModelError) as caught:
                client_model.load_client_model(raw, environ={"HOME": str(home)})

            self.assertIn(
                "manageByDefault only when management.status is confirmed",
                "\n".join(caught.exception.errors),
            )



    def test_json_schema_and_real_registry_instance_validate(self):
        try:
            import jsonschema
        except (ImportError, OSError) as exc:
            self.skipTest(f"optional jsonschema test dependency unavailable: {exc}")
        import yaml

        repo = Path(__file__).resolve().parents[1]
        schema = json.loads(
            Path(describe_resources()["clientSchema"]).read_text(encoding="utf-8")
        )
        registry = yaml.safe_load(
            (repo / "config" / "skill-targets.yaml").read_text(encoding="utf-8")
        )

        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(registry), key=lambda error: list(error.path))

        self.assertEqual(
            errors,
            [],
            "\n".join(
                f"{'.'.join(map(str, error.path))}: {error.message}" for error in errors
            ),
        )


if __name__ == "__main__":
    unittest.main()
