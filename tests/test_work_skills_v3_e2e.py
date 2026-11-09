from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from local_agent_manager.skills.coordination import shared_resource_locks, SharedResourceBusyError

try:
    from tests.test_work_skills_v3 import (
        HEALTHY_SYSTEM_AUDIT,
        create_real_directory_link,
        manager,
    )
except ModuleNotFoundError:  # unittest discovery with ``-s tests``
    from test_work_skills_v3 import (
        HEALTHY_SYSTEM_AUDIT,
        create_real_directory_link,
        manager,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]


def _doctor(options) -> int:
    return manager.run_doctor(options, None, False, HEALTHY_SYSTEM_AUDIT)


def _mount(root: Path, *, delivery: str = "dedicated", source_selection: str = "all") -> dict:
    return {
        "label": root.name,
        "root": str(root),
        "delivery": delivery,
        "priority": 100,
        "sourceSelection": source_selection,
        "linkPolicy": {
            "windows": "junction",
            "posix": "symlink",
            "unmanagedLinks": "preserve",
        },
    }


def _consumer(consumer_id: str, *, status: str = "confirmed") -> dict:
    return {
        "label": consumer_id,
        "status": status,
        "detector": {"mode": "any", "commands": [consumer_id]},
        "verification": {
            # This descriptive registry value is deliberately not sufficient
            # to make doctor green; realized evidence belongs in state.
            "status": "verified",
            "instructions": f"Ask {consumer_id} to list its native skills.",
        },
    }


def _adapter(
    adapter_id: str,
    consumer_id: str,
    mount_ids: list[str],
    roots: list[tuple[str, str]],
    *,
    management_status: str = "confirmed",
    manage_by_default: bool = False,
) -> dict:
    return {
        "label": adapter_id,
        "consumerIds": [consumer_id],
        "mountPointIds": mount_ids,
        "priority": 100,
        "discoveryRoots": [
            {"mountPointId": mount_id, "priority": 100 - index, "role": role}
            for index, (mount_id, role) in enumerate(roots)
        ],
        "linkSupport": {
            "status": "verified",
            "evidence": {
                "sourceUrl": f"https://example.invalid/{adapter_id}",
                "sourceLevel": "source-code",
                "verifiedVersion": "test-v3",
                "verifiedOn": "2026-08-03",
            },
        },
        "consumerDetection": consumer_id,
        "nativeVerification": "verified",
        "management": {
            "status": management_status,
            "manageByDefault": manage_by_default,
            "createWhenAbsent": False,
        },
    }


class ClientRegistryFixture:
    """A real v3 registry with shared, private and report-only consumers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.skills = root / "skills"
        self.config = root / "config" / "skill-targets.yaml"
        self.state = root / "state"
        self.mounts = {
            "agents-shared": root / "consumers" / "agents-shared",
            "codex-compat": root / "consumers" / "codex-compat",
            "pi-private": root / "consumers" / "pi-private",
            "absent-root": root / "consumers" / "absent-root",
            "ambiguous-root": root / "consumers" / "ambiguous-root",
            "report-root": root / "consumers" / "report-root",
        }
        self.skills.mkdir(parents=True)
        self.config.parent.mkdir(parents=True)
        self.mapping = self._mapping()
        self.states = {
            "codex": "installed",
            "pi": "installed",
            "cindy": "installed",
            "absent": "not_installed",
            "ambiguous": "ambiguous",
            "report-only": "installed",
        }
        self.write_config()

    def _mapping(self) -> dict:
        mounts = {
            "agents-shared": _mount(self.mounts["agents-shared"], delivery="shared"),
            "codex-compat": _mount(
                self.mounts["codex-compat"], delivery="compatibility"
            ),
            "pi-private": _mount(
                self.mounts["pi-private"],
                delivery="private-only",
                source_selection="none",
            ),
            "absent-root": _mount(self.mounts["absent-root"]),
            "ambiguous-root": _mount(self.mounts["ambiguous-root"]),
            "report-root": _mount(self.mounts["report-root"]),
        }
        consumers = {
            "codex": _consumer("codex"),
            "pi": _consumer("pi"),
            "cindy": _consumer("cindy"),
            "absent": _consumer("absent"),
            "ambiguous": _consumer("ambiguous", status="ambiguous"),
            "report-only": _consumer("report-only", status="report-only"),
        }
        adapters = {
            "codex": _adapter(
                "codex",
                "codex",
                ["agents-shared", "codex-compat"],
                [("agents-shared", "shared"), ("codex-compat", "transitional")],
                manage_by_default=True,
            ),
            "pi": _adapter(
                "pi",
                "pi",
                ["agents-shared", "pi-private"],
                [("agents-shared", "shared"), ("pi-private", "private-only")],
                manage_by_default=True,
            ),
            "cindy": _adapter(
                "cindy",
                "cindy",
                ["agents-shared"],
                [("agents-shared", "shared")],
                manage_by_default=True,
            ),
            "absent": _adapter(
                "absent", "absent", ["absent-root"], [("absent-root", "primary")]
            ),
            "ambiguous": _adapter(
                "ambiguous",
                "ambiguous",
                ["ambiguous-root"],
                [("ambiguous-root", "primary")],
                management_status="ambiguous",
            ),
            "report-only": _adapter(
                "report-only",
                "report-only",
                ["report-root"],
                [("report-root", "primary")],
                management_status="report-only",
            ),
        }
        return {
            "version": 3,
            "repo": {"skillsRoot": "skills"},
            "defaults": {
                "importLocal": False,
                "backupRoot": str(self.root / "backups" / "{targetRoot}"),
            },
            "mountPoints": mounts,
            "consumers": consumers,
            "adapters": adapters,
            "targetSets": {
                "default": ["codex", "pi", "cindy"],
                "all": ["codex", "pi", "cindy"],
            },
            "targets": {
                mount_id: {
                    "label": mount_id,
                    "root": str(path),
                    "backupRoot": str(self.root / "backups" / mount_id),
                    "importLocal": False,
                    "mount": {"exclude": []},
                }
                for mount_id, path in self.mounts.items()
            },
        }

    def write_config(self) -> None:
        # JSON is valid YAML and keeps fixture generation dependency-free.
        self.config.write_text(json.dumps(self.mapping, indent=2), encoding="utf-8")

    def add_skill(self, name: str = "alpha") -> Path:
        skill = self.skills / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: A static fixture used to validate the v3 manager.\n"
            "---\n\n"
            f"# {name}\n",
            encoding="utf-8",
        )
        return skill

    def add_local_skill(self, name: str = "alpha") -> Path:
        skill = self.mounts["agents-shared"] / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: A local fixture that must pass static import validation.\n"
            "---\n\n"
            f"# local {name}\n",
            encoding="utf-8",
        )
        return skill

    def options(self, targets: list[str] | None = None, **kwargs):
        return manager.RuntimeOptions(
            repo_root=self.root,
            config_path=self.config,
            targets=targets or ["codex"],
            **kwargs,
        )

    def detections(self):
        return tuple(
            manager.ConsumerDetection(
                consumer_id,
                state,
                (consumer_id,) if state == "installed" else (),
                (),
                f"fixture detection: {state}",
            )
            for consumer_id, state in self.states.items()
        )

    @contextmanager
    def context(self):
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {"WORK_SKILLS_STATE_DIR": str(self.state)},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    manager,
                    "detect_consumers",
                    side_effect=lambda model: self.detections(),
                )
            )
            yield

    def cli(self, command: str, targets: list[str], *extra: str) -> list[str]:
        argv = [
            command,
            "--repo-root",
            str(self.root),
            "--config",
            str(self.config),
        ]
        for target in targets:
            argv.extend(["--target", target])
        argv.extend(extra)
        return argv

    def manifest(self) -> dict:
        return json.loads(
            (manager.state_dir(self.root) / "manifest.json").read_text(encoding="utf-8")
        )

    def write_manifest(self, value: dict) -> None:
        manager.atomic_write_json(manager.state_dir(self.root) / "manifest.json", value)


def _matching_native_record(target) -> dict:
    return {
        "status": "verified",
        "adapterIds": list(target.adapter_ids),
        "consumerIds": list(target.consumer_ids),
        "mountPointId": target.mount_point_id,
        "root": str(target.root),
        "evidence": {
            "method": "manual-native-check",
            "verifiedVersion": "fixture-v1",
            "verifiedOn": "2026-08-03",
        },
    }


def _clients_verify_argv(
    fixture: ClientRegistryFixture,
    targets: list[str],
    *,
    verified_version: str | None = "fixture-client-v1",
    confirm: bool = True,
    output_format: str = "json",
) -> list[str]:
    argv = [
        "clients",
        "verify",
        "--repo-root",
        str(fixture.root),
        "--config",
        str(fixture.config),
    ]
    for target in targets:
        argv.extend(["--target", target])
    if verified_version is not None:
        argv.extend(["--verified-version", verified_version])
    if confirm:
        argv.append("--confirm-native-check")
    argv.extend(["--format", output_format])
    return argv


def _apply_v3_mount(fixture: ClientRegistryFixture, targets: list[str]):
    options = fixture.options(targets, yes=True)
    plan = manager.build_plan(options)
    if plan.blockers:
        raise AssertionError([action.line() for action in plan.blockers])
    with redirect_stdout(io.StringIO()):
        manager.apply_plan(plan, options)
    return plan, options


class TopologyPolicyTests(unittest.TestCase):
    def test_explicit_compatibility_root_overlap_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            overlap = fixture.skills / "compat-root"
            fixture.mapping["mountPoints"]["codex-compat"]["root"] = str(overlap)
            fixture.mapping["targets"]["codex-compat"]["root"] = str(overlap)
            fixture.write_config()

            with fixture.context():
                plan = manager.build_plan(fixture.options(["codex-compat"]))

            self.assertEqual([target.name for target in plan.targets], ["codex-compat"])
            overlaps = [action for action in plan.blockers if action.kind == "PATH_OVERLAP"]
            self.assertTrue(
                any("consumer root" in action.message for action in overlaps),
                [action.line() for action in plan.actions],
            )

    def test_explicit_compatibility_backup_overlap_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            fixture.mapping["targets"]["codex-compat"]["backupRoot"] = str(
                fixture.skills / "compat-backups"
            )
            fixture.write_config()

            with fixture.context():
                plan = manager.build_plan(fixture.options(["codex-compat"]))

            self.assertEqual([target.name for target in plan.targets], ["codex-compat"])
            overlaps = [action for action in plan.blockers if action.kind == "PATH_OVERLAP"]
            self.assertTrue(
                any("backup root" in action.message for action in overlaps),
                [action.line() for action in plan.actions],
            )


class DetectionPlanningAndCliTests(unittest.TestCase):
    def test_clients_detect_reports_states_without_creating_any_discovery_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            output = io.StringIO()
            argv = [
                "clients",
                "detect",
                "--repo-root",
                str(fixture.root),
                "--config",
                str(fixture.config),
                "--format",
                "json",
            ]

            with fixture.context(), redirect_stdout(output):
                self.assertEqual(manager.main(argv), 0)

            payload = json.loads(output.getvalue())
            by_id = {item["id"]: item["state"] for item in payload["consumers"]}
            self.assertEqual(by_id, fixture.states)
            self.assertTrue(all(not path.exists() for path in fixture.mounts.values()))

    def test_absent_ambiguous_and_report_only_explicit_targets_are_read_only(self):
        cases = {
            "absent": ("CONSUMER_NOT_INSTALLED", "absent-root"),
            "ambiguous": ("CONSUMER_AMBIGUOUS", "ambiguous-root"),
            "report-only": ("CONSUMER_INSTALLED", "report-root"),
        }
        for adapter_id, (observation, mount_id) in cases.items():
            with self.subTest(adapter=adapter_id), tempfile.TemporaryDirectory() as tmp:
                fixture = ClientRegistryFixture(Path(tmp))
                fixture.add_skill()
                options = fixture.options([adapter_id], yes=True)

                with fixture.context():
                    plan = manager.build_plan(options)
                    self.assertEqual(plan.targets, [])
                    self.assertIn(observation, [action.kind for action in plan.actions])
                    self.assertFalse(
                        any(
                            action.kind in manager.KNOWN_MUTATING_ACTIONS
                            for action in plan.actions
                        )
                    )
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)
                        # A non-mutating CLI apply needs no plan ID.
                        self.assertEqual(
                            manager.main(fixture.cli("apply", [adapter_id], "--yes")),
                            0,
                        )
                        self.assertEqual(_doctor(options), 0)

                self.assertFalse(fixture.mounts[mount_id].exists())

    def test_shared_consumers_project_one_target_and_never_mount_pi_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            options = fixture.options(
                ["codex", "pi", "cindy", "pi-private"], yes=True
            )

            with fixture.context():
                plan = manager.build_plan(options)
                self.assertEqual([target.name for target in plan.targets], ["agents-shared"])
                target = plan.targets[0]
                self.assertEqual(set(target.adapter_ids), {"codex", "pi", "cindy"})
                self.assertEqual(set(target.consumer_ids), {"codex", "pi", "cindy"})
                creates = [
                    action
                    for action in plan.actions
                    if action.kind == "CREATE_TARGET_ROOT"
                ]
                links = [action for action in plan.actions if action.kind == "CREATE_LINK"]
                self.assertEqual([action.path for action in creates], [fixture.mounts["agents-shared"]])
                self.assertEqual([action.path for action in links], [fixture.mounts["agents-shared"] / "alpha"])
                self.assertFalse(
                    any(
                        action.path is not None
                        and manager.lexical_contains(action.path, fixture.mounts["pi-private"])
                        for action in plan.actions
                    )
                )
                with redirect_stdout(io.StringIO()):
                    manager.apply_plan(plan, options)

                manifest = fixture.manifest()
                manifest["consumers"]["agents-shared"]["nativeVerification"] = (
                    _matching_native_record(target)
                )
                fixture.write_manifest(manifest)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(_doctor(options), 0)

            self.assertTrue(
                manager.direct_link_to(fixture.mounts["agents-shared"] / "alpha", source)
            )
            self.assertFalse(fixture.mounts["pi-private"].exists())

    def test_mutating_cli_apply_requires_current_reviewed_plan_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()

            with fixture.context(), redirect_stdout(io.StringIO()):
                reviewed = manager.build_plan(fixture.options(["codex"], yes=True))
                with self.assertRaises(manager.BlockedError):
                    manager.main(fixture.cli("apply", ["codex"], "--yes"))
                with self.assertRaises(manager.BlockedError):
                    manager.main(
                        fixture.cli(
                            "apply", ["codex"], "--yes", "--plan-id", "not-reviewed"
                        )
                    )
                self.assertFalse(fixture.mounts["agents-shared"].exists())

                # Source content is part of the reviewed plan, not merely its path.
                # A post-review edit must therefore make the once-correct ID stale.
                skill_md = source / "SKILL.md"
                skill_md.write_text(
                    skill_md.read_text(encoding="utf-8") + "\nchanged after review\n",
                    encoding="utf-8",
                )
                with self.assertRaises(manager.BlockedError):
                    manager.main(
                        fixture.cli(
                            "apply",
                            ["codex"],
                            "--yes",
                            "--plan-id",
                            reviewed.plan_id,
                        )
                    )

                current = manager.build_plan(fixture.options(["codex"], yes=True))
                self.assertNotEqual(reviewed.plan_id, current.plan_id)
                self.assertEqual(
                    manager.main(
                        fixture.cli(
                            "apply",
                            ["codex"],
                            "--yes",
                            "--plan-id",
                            current.plan_id,
                        )
                    ),
                    0,
                )

            self.assertTrue(
                manager.direct_link_to(fixture.mounts["agents-shared"] / "alpha", source)
            )


class GlobalDeleteInventoryTests(unittest.TestCase):
    def _stray_link_case(self, adapter_id: str, mount_id: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            consumer = fixture.mounts[mount_id] / "alpha"
            consumer.parent.mkdir(parents=True)
            create_real_directory_link(source, consumer)
            options = fixture.options(
                [adapter_id],
                delete_skills=["alpha"],
                confirm_global_delete=True,
                yes=True,
            )

            with fixture.context():
                plan = manager.build_plan(options)
                matching = [
                    action for action in plan.actions
                    if action.kind == "GLOBAL_DELETE_CONSUMER_REQUIRES_CLEANUP"
                    and action.path == consumer
                ]
                self.assertEqual(len(matching), 1, [action.line() for action in plan.actions])
                self.assertNotIn(mount_id, [target.name for target in plan.targets])
                with self.assertRaises(manager.BlockedError):
                    manager.apply_plan(plan, options)

            self.assertTrue(source.is_dir())
            self.assertTrue(manager.inspect_path(consumer).is_link)
            self.assertTrue(
                all(
                    path.exists() if name == mount_id else not path.exists()
                    for name, path in fixture.mounts.items()
                )
            )

    def test_report_only_absent_and_ambiguous_strays_block_source_delete(self):
        cases = (
            ("report-only", "report-root"),
            ("absent", "absent-root"),
            ("ambiguous", "ambiguous-root"),
        )
        for adapter_id, mount_id in cases:
            with self.subTest(adapter=adapter_id):
                self._stray_link_case(adapter_id, mount_id)

    def test_source_selection_none_private_stray_blocks_source_delete(self):
        self._stray_link_case("pi-private", "pi-private")

    def test_manifest_owned_history_under_report_only_adapter_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            consumer = fixture.mounts["report-root"] / "alpha"
            consumer.parent.mkdir(parents=True)
            create_real_directory_link(source, consumer)
            manifest = manager.empty_manifest(fixture.root)
            manifest["consumers"]["report-root"] = {
                "skills": {
                    "alpha": {
                        "path": str(consumer),
                        "source": str(source),
                        "deploymentOwned": True,
                        "deploymentHealthy": True,
                    }
                }
            }
            options = fixture.options(
                ["report-only"],
                delete_skills=["alpha"],
                confirm_global_delete=True,
            )

            with fixture.context():
                fixture.write_manifest(manifest)
                plan = manager.build_plan(options)

            matching = [
                action for action in plan.actions
                if action.kind == "GLOBAL_DELETE_CONSUMER_REQUIRES_CLEANUP"
                and action.path == consumer
            ]
            self.assertEqual(len(matching), 1)
            self.assertIn("no confirmed installed adapter", matching[0].message)
            self.assertTrue(source.exists())

    def test_manifest_root_removed_from_config_remains_blocking_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            historical_root = fixture.root / "historical-consumer"
            consumer = historical_root / "alpha"
            historical_root.mkdir()
            create_real_directory_link(source, consumer)
            manifest = manager.empty_manifest(fixture.root)
            manifest["consumers"]["removed-adapter-root"] = {
                "root": str(historical_root),
                "skills": {
                    "alpha": {
                        "path": str(consumer),
                        "source": str(source),
                        "deploymentOwned": True,
                        "deploymentHealthy": True,
                    }
                },
            }
            options = fixture.options(
                ["report-only"],
                delete_skills=["alpha"],
                confirm_global_delete=True,
            )

            with fixture.context():
                fixture.write_manifest(manifest)
                plan = manager.build_plan(options)

            blockers = [
                action for action in plan.blockers
                if action.kind == "GLOBAL_DELETE_CONSUMER_REQUIRES_CLEANUP"
                and action.path == consumer
            ]
            self.assertEqual(len(blockers), 1, [action.line() for action in plan.actions])
            self.assertEqual(blockers[0].data["eligibleAdapterIds"], [])
            self.assertTrue(
                any(
                    action.kind == "GLOBAL_DELETE_INVENTORY_SCAN"
                    and action.path == historical_root
                    for action in plan.actions
                )
            )
            self.assertTrue(source.exists())

    def test_broad_historical_root_blocks_without_enumeration_or_recursive_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            manifest = manager.empty_manifest(fixture.root)
            manifest["consumers"]["unsafe-history"] = {
                "root": str(fixture.root),
                "skills": {},
            }
            options = fixture.options(
                ["report-only"],
                delete_skills=["alpha"],
                confirm_global_delete=True,
            )
            real_iterdir = Path.iterdir
            real_walk = manager.os.walk

            def guarded_iterdir(path):
                if manager.lexical_norm_path(path) == manager.lexical_norm_path(fixture.root):
                    raise AssertionError("broad inventory root was enumerated")
                return real_iterdir(path)

            def guarded_walk(top, *args, **kwargs):
                if manager.lexical_norm_path(Path(top)) == manager.lexical_norm_path(fixture.root):
                    raise AssertionError("broad inventory root was recursively walked")
                return real_walk(top, *args, **kwargs)

            with fixture.context(), mock.patch.object(
                Path, "iterdir", guarded_iterdir
            ), mock.patch.object(manager.os, "walk", side_effect=guarded_walk):
                fixture.write_manifest(manifest)
                plan = manager.build_plan(options)

            unsafe = [
                action for action in plan.blockers
                if action.kind == "GLOBAL_DELETE_INVENTORY_UNSAFE_ROOT"
                and action.path == fixture.root
            ]
            self.assertEqual(len(unsafe), 1, [action.line() for action in plan.actions])
            self.assertFalse(
                any(
                    action.kind == "GLOBAL_DELETE_INVENTORY_SCAN"
                    and action.path == fixture.root
                    for action in plan.actions
                )
            )

    def test_shared_root_is_inventoried_once_for_three_consumers(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            consumer = fixture.mounts["agents-shared"] / "alpha"
            consumer.parent.mkdir(parents=True)
            create_real_directory_link(source, consumer)
            options = fixture.options(
                ["report-only"],
                delete_skills=["alpha"],
                confirm_global_delete=True,
            )

            with fixture.context():
                plan = manager.build_plan(options)

            blockers = [
                action for action in plan.actions
                if action.kind == "GLOBAL_DELETE_CONSUMER_REQUIRES_CLEANUP"
                and action.path == consumer
            ]
            self.assertEqual(len(blockers), 1)
            reports = {
                action.target for action in plan.actions
                if action.kind.startswith("CONSUMER_")
            }
            self.assertTrue({"codex", "pi", "cindy"}.issubset(reports))
            self.assertEqual(plan.targets, [])

    def test_wrong_name_and_multihop_links_are_explicit_blockers(self):
        modes = ("wrong-name", "multihop")
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                fixture = ClientRegistryFixture(Path(tmp))
                source = fixture.add_skill()
                root = fixture.mounts["report-root"]
                root.mkdir(parents=True)
                if mode == "wrong-name":
                    consumer = root / "alias-alpha"
                    create_real_directory_link(source, consumer)
                else:
                    middle = fixture.root / "middle-alpha"
                    create_real_directory_link(source, middle)
                    consumer = root / "alpha"
                    create_real_directory_link(middle, consumer)
                options = fixture.options(
                    ["report-only"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                )

                with fixture.context():
                    plan = manager.build_plan(options)

                matching = [
                    action for action in plan.actions
                    if action.kind == "GLOBAL_DELETE_CONSUMER_REQUIRES_CLEANUP"
                    and action.path == consumer
                ]
                self.assertEqual(len(matching), 1, [action.line() for action in plan.actions])
                self.assertTrue(source.exists())

    def test_only_eligible_owned_direct_correct_name_is_auto_removable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            with fixture.context():
                # Compatibility delivery is opt-in for ordinary mounting, but
                # global deletion must still inventory an existing owned link.
                _apply_v3_mount(fixture, ["codex-compat"])
                options = fixture.options(
                    ["report-only"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )
                plan = manager.build_plan(options)

            self.assertEqual(plan.targets, [])
            removals = [
                action for action in plan.actions
                if action.kind == "GLOBAL_DELETE_CONSUMER"
                and action.path == fixture.mounts["codex-compat"] / "alpha"
            ]
            self.assertEqual(len(removals), 1, [action.line() for action in plan.actions])
            self.assertFalse(plan.blockers)
            kinds = [action.kind for action in plan.actions]
            self.assertLess(
                kinds.index("GLOBAL_DELETE_CONSUMER"),
                kinds.index("GLOBAL_DELETE_REPO_SKILL"),
            )
            with fixture.context(), mock.patch.object(
                manager, "move_to_trash"
            ) as trash, redirect_stdout(io.StringIO()):
                with shared_resource_locks([fixture.mounts["codex-compat"]]):
                    with self.assertRaises(SharedResourceBusyError):
                        manager.apply_plan(plan, options)
                self.assertTrue(source.exists())
                self.assertTrue((fixture.mounts["codex-compat"] / "alpha").exists())
                manager.apply_plan(plan, options)
                committed_manifest = fixture.manifest()
                trash.assert_not_called()

            journals = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (fixture.state / "transactions").glob("*.json")
            ]
            delete_journal = next(item for item in journals if item["planId"] == plan.plan_id)
            recovery = Path(delete_journal["recoveryPath"])

            self.assertFalse(source.exists())
            self.assertTrue((recovery / "sources" / "alpha").is_dir())
            self.assertEqual(delete_journal["status"], "committed")
            self.assertEqual(
                manager._lstat_kind(fixture.mounts["codex-compat"] / "alpha"),
                "missing",
            )
            self.assertIsNone(
                manager.manifest_consumer(committed_manifest, "codex-compat", "alpha")
            )

    def test_source_staging_failure_fully_rolls_back_before_persistent_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            consumer = fixture.mounts["codex-compat"] / "alpha"
            with fixture.context():
                _apply_v3_mount(fixture, ["codex-compat"])
                baseline = fixture.manifest()
                options = fixture.options(
                    ["report-only"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )
                plan = manager.build_plan(options)
                real_write_transaction = manager.write_transaction
                injected = False

                def fail_after_source_stage(repo_root, transaction):
                    nonlocal injected
                    staged_source = any(
                        str(description).startswith("restore source ")
                        for description in transaction.get("undo", [])
                    )
                    if staged_source and not injected:
                        injected = True
                        raise OSError("injected journal failure after source staging")
                    return real_write_transaction(repo_root, transaction)

                with mock.patch.object(
                    manager, "write_transaction", side_effect=fail_after_source_stage
                ), mock.patch.object(manager, "move_to_trash") as trash, redirect_stdout(
                    io.StringIO()
                ):
                    with self.assertRaises(OSError):
                        manager.apply_plan(plan, options)
                    trash.assert_not_called()

                rolled_back_manifest = fixture.manifest()

            self.assertTrue(source.is_dir())
            self.assertTrue(manager.direct_link_to(consumer, source))
            self.assertEqual(rolled_back_manifest, baseline)
            journals = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (fixture.state / "transactions").glob("*.json")
            ]
            delete_journal = next(item for item in journals if item["planId"] == plan.plan_id)
            self.assertEqual(delete_journal["status"], "rolled_back")
            self.assertEqual(manager._lstat_kind(Path(delete_journal["recoveryPath"])), "missing")

    def test_stale_inventory_plan_rejects_added_or_retargeted_source_link(self):
        modes = ("missing-root-created", "existing-link-retargeted")
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                fixture = ClientRegistryFixture(Path(tmp))
                source = fixture.add_skill()
                root = fixture.mounts["report-root"]
                consumer = root / "alpha"
                if mode == "existing-link-retargeted":
                    unrelated = fixture.root / "unrelated"
                    unrelated.mkdir()
                    root.mkdir(parents=True)
                    create_real_directory_link(unrelated, consumer)
                options = fixture.options(
                    ["report-only"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )

                with fixture.context():
                    reviewed = manager.build_plan(options)
                    self.assertFalse(reviewed.blockers)
                    scans = [
                        action for action in reviewed.actions
                        if action.kind == "GLOBAL_DELETE_INVENTORY_SCAN"
                        and action.data.get("inventoryRoot") == str(root)
                    ]
                    self.assertTrue(scans)
                    if mode == "missing-root-created":
                        root.mkdir(parents=True)
                    else:
                        manager.remove_link(consumer)
                    create_real_directory_link(source, consumer)
                    with self.assertRaises(manager.BlockedError):
                        manager.apply_plan(reviewed, options)

                self.assertTrue(source.exists())
                self.assertTrue(manager.direct_link_to(consumer, source))

    def test_shallow_inventory_seals_transitive_immediate_link_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            middle = fixture.root / "external-middle"
            middle.mkdir()
            root = fixture.mounts["report-root"]
            root.mkdir(parents=True)
            alias = root / "alias"
            create_real_directory_link(middle, alias)
            options = fixture.options(
                ["report-only"],
                delete_skills=["alpha"],
                confirm_global_delete=True,
                yes=True,
            )

            with fixture.context():
                reviewed = manager.build_plan(options)
                self.assertFalse(reviewed.blockers)
                middle.rmdir()
                create_real_directory_link(source, middle)
                with self.assertRaises(manager.BlockedError):
                    manager.apply_plan(reviewed, options)

            alias_state = manager.inspect_path(alias)
            self.assertTrue(source.is_dir())
            self.assertFalse(alias_state.broken)
            self.assertEqual(
                manager.lexical_norm_path(alias_state.final),
                manager.lexical_norm_path(source),
            )

    def test_source_stage_rechecks_inventory_after_consumer_removal_retarget(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            middle = fixture.root / "external-middle"
            middle.mkdir()
            report_root = fixture.mounts["report-root"]
            report_root.mkdir(parents=True)
            alias = report_root / "alias"
            create_real_directory_link(middle, alias)

            with fixture.context():
                _apply_v3_mount(fixture, ["codex-compat"])
                baseline = fixture.manifest()
                managed_consumer = fixture.mounts["codex-compat"] / "alpha"
                options = fixture.options(
                    ["report-only"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                    yes=True,
                )
                reviewed = manager.build_plan(options)
                self.assertFalse(reviewed.blockers)
                real_remove_link = manager.remove_link
                retargeted = False

                def remove_then_retarget(path):
                    nonlocal retargeted
                    real_remove_link(path)
                    if (
                        not retargeted
                        and manager.lexical_norm_path(path)
                        == manager.lexical_norm_path(managed_consumer)
                    ):
                        middle.rmdir()
                        create_real_directory_link(source, middle)
                        retargeted = True

                with mock.patch.object(
                    manager, "remove_link", side_effect=remove_then_retarget
                ), redirect_stdout(io.StringIO()):
                    with self.assertRaises(manager.BlockedError):
                        manager.apply_plan(reviewed, options)
                rolled_back_manifest = fixture.manifest()

            alias_state = manager.inspect_path(alias)
            self.assertTrue(retargeted)
            self.assertTrue(source.is_dir())
            self.assertTrue(manager.direct_link_to(managed_consumer, source))
            self.assertEqual(rolled_back_manifest, baseline)
            self.assertFalse(alias_state.broken)
            self.assertEqual(
                manager.lexical_norm_path(alias_state.final),
                manager.lexical_norm_path(source),
            )

    def test_inventory_root_link_to_skill_blocks_but_link_to_source_parent_does_not(self):
        modes = ("skill", "source-parent")
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                fixture = ClientRegistryFixture(Path(tmp))
                source = fixture.add_skill()
                root = fixture.mounts["report-root"]
                create_real_directory_link(
                    source if mode == "skill" else fixture.skills,
                    root,
                )
                options = fixture.options(
                    ["report-only"],
                    delete_skills=["alpha"],
                    confirm_global_delete=True,
                )

                with fixture.context():
                    plan = manager.build_plan(options)

                root_blockers = [
                    action for action in plan.actions
                    if action.kind == "GLOBAL_DELETE_CONSUMER_REQUIRES_CLEANUP"
                    and action.path == root
                    and action.data.get("rootPointsToSource") is True
                ]
                if mode == "skill":
                    self.assertEqual(len(root_blockers), 1)
                    self.assertTrue(plan.blockers)
                else:
                    self.assertEqual(root_blockers, [])
                    self.assertFalse(plan.blockers)


class PlanSemanticSealTests(unittest.TestCase):
    def test_comments_and_formatting_do_not_change_canonical_plan_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            with fixture.context():
                first = manager.build_plan(fixture.options(["codex"]))
                text = fixture.config.read_text(encoding="utf-8")
                fixture.config.write_text("# formatting-only comment\n\n" + text, encoding="utf-8")
                second = manager.build_plan(fixture.options(["codex"]))

            self.assertEqual(first.semantic_summary, second.semantic_summary)
            self.assertEqual(first.plan_id, second.plan_id)

    def test_model_semantics_and_selection_intent_change_plan_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            with fixture.context():
                codex = manager.build_plan(fixture.options(["codex"]))
                extra_selection = manager.build_plan(
                    fixture.options(["codex", "report-only"])
                )
                fixture.mapping["adapters"]["codex"]["linkSupport"]["evidence"][
                    "verifiedVersion"
                ] = "changed-semantic-version"
                fixture.write_config()
                changed_model = manager.build_plan(fixture.options(["codex"]))

            self.assertNotEqual(codex.plan_id, extra_selection.plan_id)
            self.assertNotEqual(codex.plan_id, changed_model.plan_id)

    def test_apply_lock_revalidates_config_and_implementation_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            options = fixture.options(["codex"], yes=True)
            with fixture.context():
                plan = manager.build_plan(options)
                fixture.mapping["adapters"]["codex"]["linkSupport"]["evidence"][
                    "verifiedVersion"
                ] = "changed-after-plan"
                fixture.write_config()
                with self.assertRaises(manager.BlockedError):
                    manager.apply_plan(plan, options)
                fixture.mapping["adapters"]["codex"]["linkSupport"]["evidence"][
                    "verifiedVersion"
                ] = "test-v3"
                fixture.write_config()
                with mock.patch.object(
                    manager, "manager_implementation_digest", return_value="changed"
                ):
                    with self.assertRaises(manager.BlockedError):
                        manager.apply_plan(plan, options)

            self.assertFalse(fixture.mounts["agents-shared"].exists())


class LinkIdentityAndRollbackTests(unittest.TestCase):
    def test_final_alias_is_not_mistaken_for_a_healthy_direct_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            alias_root = fixture.root / "source-alias"
            create_real_directory_link(fixture.skills, alias_root)
            consumer = fixture.mounts["agents-shared"] / "alpha"
            consumer.parent.mkdir(parents=True)
            create_real_directory_link(alias_root / "alpha", consumer)

            with fixture.context():
                self.assertEqual(manager.inspect_path(consumer).final, source.resolve())
                self.assertFalse(manager.direct_link_to(consumer, source))
                plan = manager.build_plan(fixture.options(["codex"]))

            kinds = [action.kind for action in plan.actions if action.path == consumer]
            self.assertNotIn("OK_LINK", kinds)
            self.assertTrue(
                any(action.blocking for action in plan.actions if action.path == consumer),
                kinds,
            )

    def test_multihop_link_is_not_mistaken_for_healthy_same_final_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            middle = fixture.root / "middle-alpha"
            create_real_directory_link(source, middle)
            consumer = fixture.mounts["agents-shared"] / "alpha"
            consumer.parent.mkdir(parents=True)
            create_real_directory_link(middle, consumer)

            with fixture.context():
                observed = manager.inspect_path(consumer)
                self.assertEqual(observed.final, source.resolve())
                self.assertFalse(observed.direct)
                plan = manager.build_plan(fixture.options(["codex"]))

            self.assertIn(
                "MULTI_HOP_LINK",
                [action.kind for action in plan.actions if action.path == consumer],
            )

    def _assert_replacement_failure_restores_raw_link(self, mode: str) -> None:
        class InjectedFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            alpha = fixture.add_skill("alpha")
            beta = fixture.add_skill("beta")
            consumer = fixture.mounts["agents-shared"] / "alpha"
            consumer.parent.mkdir(parents=True)
            if mode == "wrong":
                create_real_directory_link(beta, consumer)
            elif mode == "multihop":
                middle = fixture.root / "middle"
                create_real_directory_link(alpha, middle)
                create_real_directory_link(middle, consumer)
            elif mode == "broken":
                create_real_directory_link(fixture.root / "missing-alpha", consumer)
            else:  # pragma: no cover - helper misuse
                raise AssertionError(mode)
            before = manager.snapshot_link(consumer)
            options = fixture.options(["codex"], yes=True, replace_wrong_links=True)

            with fixture.context():
                plan = manager.build_plan(options)
                replacement = [
                    action
                    for action in plan.actions
                    if action.kind == "REPLACE_WRONG_LINK" and action.path == consumer
                ]
                self.assertEqual(len(replacement), 1, [action.line() for action in plan.actions])

                real_create = manager.create_link

                def fail_replacement(source: Path, target: Path) -> None:
                    if target == consumer:
                        raise InjectedFailure(f"injected {mode} replacement failure")
                    real_create(source, target)

                with mock.patch.object(manager, "create_link", side_effect=fail_replacement):
                    with self.assertRaises((InjectedFailure, manager.TransactionError)):
                        with redirect_stdout(io.StringIO()):
                            manager.apply_plan(plan, options)

            after = manager.snapshot_link(consumer)
            self.assertEqual(after, before)
            self.assertEqual(manager.inspect_path(consumer).lstat_kind, before.kind)

    def test_wrong_link_rollback_restores_original_primitive_and_raw_target(self):
        self._assert_replacement_failure_restores_raw_link("wrong")

    def test_multihop_rollback_restores_original_primitive_and_raw_target(self):
        self._assert_replacement_failure_restores_raw_link("multihop")

    def test_broken_link_rollback_restores_original_primitive_and_raw_target(self):
        self._assert_replacement_failure_restores_raw_link("broken")


class RootFileReplacementTests(unittest.TestCase):
    def test_target_root_file_replacement_succeeds_transactionally(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            root = fixture.mounts["agents-shared"]
            root.parent.mkdir(parents=True)
            root.write_text("legacy root file\n", encoding="utf-8")
            options = fixture.options(["codex"], yes=True, replace_files=True)

            with fixture.context():
                plan = manager.build_plan(options)
                kinds = [action.kind for action in plan.actions]
                self.assertIn("MOVE_FILE_TO_BACKUP", kinds)
                self.assertTrue(
                    any(
                        action.kind == "CREATE_TARGET_ROOT"
                        and action.data.get("postRootFileReplacement") is True
                        for action in plan.actions
                    )
                )
                with redirect_stdout(io.StringIO()):
                    manager.apply_plan(plan, options)

            self.assertTrue(root.is_dir())
            self.assertTrue(manager.direct_link_to(root / "alpha", source))
            backups = list((fixture.root / "backups" / "agents-shared").glob("agents-shared.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "legacy root file\n")

    def test_target_root_file_replacement_failure_restores_file_and_state(self):
        class InjectedFailure(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            root = fixture.mounts["agents-shared"]
            root.parent.mkdir(parents=True)
            root.write_text("restore exact file\n", encoding="utf-8")
            options = fixture.options(["codex"], yes=True, replace_files=True)

            with fixture.context():
                plan = manager.build_plan(options)
                with mock.patch.object(
                    manager,
                    "create_link",
                    side_effect=InjectedFailure("fail after root-file backup"),
                ):
                    with self.assertRaises((InjectedFailure, manager.TransactionError)):
                        with redirect_stdout(io.StringIO()):
                            manager.apply_plan(plan, options)
                manifest = manager.load_manifest(fixture.root)

            self.assertTrue(root.is_file())
            self.assertEqual(root.read_text(encoding="utf-8"), "restore exact file\n")
            self.assertFalse((fixture.root / "backups" / "agents-shared").exists())
            self.assertEqual(manifest.get("consumers", {}), {})


class SourceAndImportSafetyTests(unittest.TestCase):
    def test_shared_document_cannot_be_redirected_by_directory_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = ClientRegistryFixture(base / "repo")
            borrower = fixture.add_skill("steel-contracts")
            entry = borrower / "SKILL.md"
            entry.write_text(
                entry.read_text(encoding="utf-8")
                + "\nRead [research](../lc-workflow/references/professional-research.md).\n",
                encoding="utf-8",
            )
            owner = fixture.add_skill("lc-workflow")
            outside = base / "outside-references"
            outside.mkdir()
            (outside / "professional-research.md").write_text("# Outside\n", encoding="utf-8")
            create_real_directory_link(outside, owner / "references")

            with fixture.context():
                errors = manager.run_content_audit(fixture.root).errors

            self.assertTrue(any(
                issue.skill == "steel-contracts" and issue.code == "REFERENCE_ESCAPE"
                for issue in errors
            ))

    def test_skills_root_lexical_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = ClientRegistryFixture(base / "repo")
            outside = base / "outside-skills"
            outside.mkdir()
            fixture.mapping["repo"]["skillsRoot"] = "../outside-skills"
            fixture.write_config()

            with fixture.context(), self.assertRaises(manager.ConfigError):
                manager.build_plan(fixture.options(["codex"]))

    def test_skills_root_final_escape_through_reparse_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = ClientRegistryFixture(base / "repo")
            outside = base / "outside-skills"
            outside.mkdir()
            fixture.skills.rmdir()
            create_real_directory_link(outside, fixture.skills)

            with fixture.context(), self.assertRaises(manager.ConfigError):
                manager.build_plan(fixture.options(["codex"]))

    def test_linked_source_skill_is_a_blocker_not_a_silent_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = ClientRegistryFixture(base / "repo")
            external = base / "external-alpha"
            external.mkdir()
            (external / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: external fixture\n---\n",
                encoding="utf-8",
            )
            create_real_directory_link(external, fixture.skills / "alpha")
            # Keep one ordinary source so discovery cannot reject merely because
            # the linked entry happened to be the only candidate.
            fixture.add_skill("beta")

            with fixture.context(), self.assertRaises(manager.ConfigError) as raised:
                manager.build_plan(fixture.options(["codex"]))

            self.assertRegex(str(raised.exception).lower(), r"source|linked|reparse|junction")
            self.assertFalse(fixture.mounts["agents-shared"].exists())

    def test_forbidden_instruction_in_nested_non_skill_file_blocks_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill("beta")
            local = fixture.add_local_skill("alpha")
            references = local / "references"
            references.mkdir()
            (references / "notes.toml").write_text(
                'advice = "use --dangerously-skip-permissions"\n', encoding="utf-8"
            )
            fixture.mapping["targets"]["agents-shared"]["importLocal"] = True
            fixture.write_config()
            options = fixture.options(["codex"], yes=True)

            with fixture.context():
                plan = manager.build_plan(options)
                self.assertIn("IMPORT_LOCAL_SKILL", [action.kind for action in plan.actions])
                with self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.apply_plan(plan, options)

            self.assertTrue(local.is_dir())
            self.assertFalse((fixture.skills / "alpha").exists())
            self.assertEqual(
                manager.inspect_path(fixture.mounts["agents-shared"] / "beta").lstat_kind,
                "missing",
            )

    def test_python_ast_dangerous_calls_and_absolute_paths_are_rejected(self):
        payloads = {
            "dangerous-call": (
                "import os\n"
                "def main():\n"
                "    os.system('rm -rf /')\n"
            ),
            "absolute-path": (
                "from pathlib import Path\n"
                "def read_secret():\n"
                "    return Path('/etc/shadow').read_text()\n"
            ),
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                fixture = ClientRegistryFixture(Path(tmp))
                local = fixture.add_local_skill("alpha")
                scripts = local / "scripts"
                scripts.mkdir()
                (scripts / "unsafe.py").write_text(payload, encoding="utf-8")

                with self.assertRaises(manager.BlockedError):
                    manager.validate_import_tree(local, "alpha")

    def test_hard_linked_import_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            local = fixture.add_local_skill("alpha")
            original = fixture.root / "original.txt"
            original.write_text("same inode\n", encoding="utf-8")
            linked = local / "linked.txt"
            try:
                os.link(original, linked)
            except OSError as exc:
                self.skipTest(f"hard links unavailable on this filesystem: {exc}")

            with self.assertRaises(manager.BlockedError):
                manager.validate_import_tree(local, "alpha")

    def test_nested_markdown_missing_resource_blocks_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            local = fixture.add_local_skill("alpha")
            references = local / "references"
            references.mkdir()
            guide = references / "guide.md"
            guide.write_text("# Guide\n\nUse `assets/missing.txt`.\n", encoding="utf-8")

            with self.assertRaisesRegex(manager.BlockedError, "guide.md"):
                manager.validate_import_tree(local, "alpha")


class NativeVerificationAndSchemaTests(unittest.TestCase):
    def test_only_matching_structured_native_evidence_can_make_doctor_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            options = fixture.options(["codex"], yes=True)

            with fixture.context():
                plan = manager.build_plan(options)
                target = plan.targets[0]
                with redirect_stdout(io.StringIO()):
                    manager.apply_plan(plan, options)
                    self.assertEqual(_doctor(options), 3)

                baseline = fixture.manifest()
                invalid_records = {
                    "bare-bool": True,
                    "bare-string": "verified",
                    "mismatched-adapter": {
                        **_matching_native_record(target),
                        "adapterIds": ["some-other-adapter"],
                    },
                    "mismatched-consumer": {
                        **_matching_native_record(target),
                        "consumerIds": ["some-other-consumer"],
                    },
                    "missing-evidence": {
                        **_matching_native_record(target),
                        "evidence": None,
                    },
                    "unrecognized-method": {
                        **_matching_native_record(target),
                        "evidence": {
                            "method": "trust-me",
                            "verifiedVersion": "fixture-v1",
                            "verifiedOn": "2026-08-03",
                        },
                    },
                }
                for label, record in invalid_records.items():
                    with self.subTest(record=label):
                        manifest = json.loads(json.dumps(baseline))
                        manifest["consumers"]["agents-shared"]["nativeVerified"] = True
                        manifest["consumers"]["agents-shared"]["nativeVerification"] = record
                        fixture.write_manifest(manifest)
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(_doctor(options), 3)

                manifest = json.loads(json.dumps(baseline))
                manifest["consumers"]["agents-shared"]["nativeVerification"] = (
                    _matching_native_record(target)
                )
                fixture.write_manifest(manifest)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(_doctor(options), 0)

    def test_schema_checker_fails_a_real_invalid_instance(self):
        try:
            import jsonschema  # noqa: F401
            import yaml  # noqa: F401
        except (ImportError, OSError) as exc:
            self.skipTest(f"schema checker dependencies unavailable: {exc}")

        checker_path = REPO_ROOT / "clients" / "check_client_model.py"
        spec = importlib.util.spec_from_file_location(
            "work_skills_schema_checker_under_test", checker_path
        )
        checker = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = checker
        spec.loader.exec_module(checker)

        with tempfile.TemporaryDirectory() as tmp:
            invalid_config = Path(tmp) / "invalid.yaml"
            invalid_config.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "mountPoints": {},
                        "consumers": {},
                        "adapters": {},
                    }
                ),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with mock.patch.object(checker, "CONFIG", invalid_config):
                with redirect_stderr(stderr):
                    self.assertEqual(checker.main(), 1)

        self.assertIn("does not match JSON Schema", stderr.getvalue())


class ClientsVerifyBlackBoxTests(unittest.TestCase):
    def test_required_verify_arguments_are_rejected_without_writing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            with fixture.context():
                _apply_v3_mount(fixture, ["codex"])
                baseline = fixture.manifest()
                cases = (
                    (
                        "missing-confirmation",
                        _clients_verify_argv(fixture, ["codex"], confirm=False),
                        manager.BlockedError,
                    ),
                    (
                        "missing-version",
                        _clients_verify_argv(
                            fixture, ["codex"], verified_version=None
                        ),
                        manager.ConfigError,
                    ),
                    (
                        "missing-target",
                        _clients_verify_argv(fixture, []),
                        manager.ConfigError,
                    ),
                )
                for label, argv, error_type in cases:
                    with self.subTest(case=label):
                        fixture.write_manifest(json.loads(json.dumps(baseline)))
                        with self.assertRaises(error_type):
                            with redirect_stdout(io.StringIO()):
                                manager.main(argv)
                        self.assertEqual(fixture.manifest(), baseline)

    def test_verify_rejects_mount_aliases_and_multiple_raw_adapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            with fixture.context():
                _apply_v3_mount(fixture, ["codex", "pi"])
                baseline = fixture.manifest()
                cases = (
                    ("shared-mount-alias", ["agents-shared"]),
                    ("compatibility-mount-alias", ["codex-compat"]),
                    ("multiple-raw-adapters", ["codex", "pi"]),
                )
                for label, targets in cases:
                    with self.subTest(case=label):
                        fixture.write_manifest(json.loads(json.dumps(baseline)))
                        with self.assertRaises(manager.ConfigError):
                            with redirect_stdout(io.StringIO()):
                                manager.main(_clients_verify_argv(fixture, targets))
                        self.assertEqual(fixture.manifest(), baseline)

    def test_absent_and_ambiguous_consumers_cannot_be_verified(self):
        cases = ("absent", "ambiguous")
        for adapter_id in cases:
            with self.subTest(adapter=adapter_id), tempfile.TemporaryDirectory() as tmp:
                fixture = ClientRegistryFixture(Path(tmp))
                fixture.add_skill()
                manifest_path = manager.state_dir(fixture.root) / "manifest.json"
                with fixture.context(), self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.main(_clients_verify_argv(fixture, [adapter_id]))

                self.assertFalse(manifest_path.exists())
                self.assertFalse(fixture.mounts[f"{adapter_id}-root"].exists())

    def test_installed_consumer_without_manifest_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            manifest_path = manager.state_dir(fixture.root) / "manifest.json"

            with fixture.context(), self.assertRaises(manager.BlockedError):
                with redirect_stdout(io.StringIO()):
                    manager.main(_clients_verify_argv(fixture, ["codex"]))

            self.assertFalse(manifest_path.exists())
            self.assertFalse(fixture.mounts["agents-shared"].exists())

    def test_mount_without_owned_deployment_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            with fixture.context():
                _apply_v3_mount(fixture, ["codex"])
                manifest = fixture.manifest()
                entry = manifest["consumers"]["agents-shared"]["skills"]["alpha"]
                entry["deploymentOwned"] = False
                fixture.write_manifest(manifest)
                baseline = fixture.manifest()

                with self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.main(_clients_verify_argv(fixture, ["codex"]))

                self.assertEqual(fixture.manifest(), baseline)

    def test_unhealthy_owned_link_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            consumer = fixture.mounts["agents-shared"] / "alpha"
            with fixture.context():
                _apply_v3_mount(fixture, ["codex"])
                baseline = fixture.manifest()
                manager.remove_link(consumer)

                with self.assertRaises(manager.BlockedError):
                    with redirect_stdout(io.StringIO()):
                        manager.main(_clients_verify_argv(fixture, ["codex"]))

                self.assertEqual(fixture.manifest(), baseline)
                record = fixture.manifest()["consumers"]["agents-shared"]
                self.assertNotIn("codex", record.get("nativeVerifiedAdapters", []))

    def test_verify_writes_adapter_evidence_and_makes_selected_doctor_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            consumer = fixture.mounts["agents-shared"] / "alpha"
            with fixture.context():
                plan, options = _apply_v3_mount(fixture, ["codex"])
                target = plan.targets[0]
                before_link = manager.snapshot_link(consumer)
                transaction_dir = manager.state_dir(fixture.root) / "transactions"
                before_transactions = sorted(transaction_dir.glob("*.json"))
                output = io.StringIO()

                with redirect_stdout(output):
                    self.assertEqual(
                        manager.main(
                            _clients_verify_argv(
                                fixture,
                                ["codex"],
                                verified_version="codex-native-v7",
                            )
                        ),
                        0,
                    )

                payload = json.loads(output.getvalue())
                self.assertEqual(
                    payload["verified"],
                    [
                        {
                            "adapter": "codex",
                            "mountPoint": "agents-shared",
                            "root": str(fixture.mounts["agents-shared"]),
                            "verifiedVersion": "codex-native-v7",
                        }
                    ],
                )
                state = fixture.manifest()["consumers"]["agents-shared"]
                record = state["nativeVerifications"]["codex"]
                self.assertEqual(record["status"], "verified")
                self.assertEqual(record["adapterId"], "codex")
                self.assertEqual(record["consumerIds"], ["codex"])
                self.assertEqual(record["mountPointId"], "agents-shared")
                self.assertEqual(record["root"], str(target.root))
                self.assertEqual(record["evidence"]["method"], "manual-native-check")
                self.assertEqual(record["evidence"]["verifiedVersion"], "codex-native-v7")
                self.assertEqual(
                    record["evidence"]["verifiedOn"],
                    manager.datetime.date.today().isoformat(),
                )
                self.assertIs(record["evidence"]["instructionsAcknowledged"], True)
                self.assertEqual(state["nativeVerifiedAdapters"], ["codex"])
                self.assertEqual(manager.snapshot_link(consumer), before_link)
                self.assertTrue(manager.direct_link_to(consumer, source))
                self.assertEqual(sorted(transaction_dir.glob("*.json")), before_transactions)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(_doctor(options), 0)

    def test_shared_mount_verification_is_independent_per_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            source = fixture.add_skill()
            consumer = fixture.mounts["agents-shared"] / "alpha"
            with fixture.context():
                _apply_v3_mount(fixture, ["codex", "pi", "cindy"])
                before_link = manager.snapshot_link(consumer)
                doctors = {
                    adapter_id: fixture.options([adapter_id], yes=True)
                    for adapter_id in ("codex", "pi", "cindy")
                }

                with redirect_stdout(io.StringIO()):
                    manager.main(
                        _clients_verify_argv(
                            fixture, ["codex"], verified_version="codex-v1"
                        )
                    )
                    self.assertEqual(_doctor(doctors["codex"]), 0)
                    self.assertEqual(_doctor(doctors["pi"]), 3)
                    self.assertEqual(_doctor(doctors["cindy"]), 3)

                state = fixture.manifest()["consumers"]["agents-shared"]
                self.assertEqual(state["nativeVerifiedAdapters"], ["codex"])
                self.assertEqual(state["nativeVerifications"]["codex"]["status"], "verified")
                self.assertEqual(state["nativeVerifications"]["pi"]["status"], "unverified")
                self.assertEqual(state["nativeVerifications"]["cindy"]["status"], "unverified")
                self.assertEqual(manager.snapshot_link(consumer), before_link)

                with redirect_stdout(io.StringIO()):
                    manager.main(
                        _clients_verify_argv(
                            fixture, ["pi"], verified_version="pi-v2"
                        )
                    )
                    self.assertEqual(_doctor(doctors["pi"]), 0)
                    self.assertEqual(_doctor(doctors["cindy"]), 3)
                    manager.main(
                        _clients_verify_argv(
                            fixture, ["cindy"], verified_version="cindy-v3"
                        )
                    )
                    self.assertEqual(_doctor(doctors["cindy"]), 0)
                    combined = fixture.options(["codex", "pi", "cindy"], yes=True)
                    self.assertEqual(_doctor(combined), 0)

                state = fixture.manifest()["consumers"]["agents-shared"]
                self.assertEqual(
                    state["nativeVerifiedAdapters"], ["cindy", "codex", "pi"]
                )
                self.assertEqual(
                    {
                        key: value["evidence"]["verifiedVersion"]
                        for key, value in state["nativeVerifications"].items()
                    },
                    {"codex": "codex-v1", "pi": "pi-v2", "cindy": "cindy-v3"},
                )
                self.assertEqual(manager.snapshot_link(consumer), before_link)
                self.assertTrue(manager.direct_link_to(consumer, source))
                self.assertFalse(fixture.mounts["pi-private"].exists())

    def test_future_mismatched_and_bare_adapter_evidence_never_make_doctor_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ClientRegistryFixture(Path(tmp))
            fixture.add_skill()
            options = fixture.options(["codex"], yes=True)
            with fixture.context():
                _apply_v3_mount(fixture, ["codex"])
                with redirect_stdout(io.StringIO()):
                    manager.main(_clients_verify_argv(fixture, ["codex"]))
                    self.assertEqual(_doctor(options), 0)

                baseline = fixture.manifest()
                valid = baseline["consumers"]["agents-shared"]["nativeVerifications"]["codex"]
                future = (
                    manager.datetime.date.today() + manager.datetime.timedelta(days=1)
                ).isoformat()
                invalid = {
                    "bare-bool": True,
                    "future-evidence": {
                        **valid,
                        "evidence": {**valid["evidence"], "verifiedOn": future},
                    },
                    "wrong-adapter": {**valid, "adapterId": "pi"},
                    "wrong-consumer": {**valid, "consumerIds": ["pi"]},
                    "wrong-mount": {**valid, "mountPointId": "pi-private"},
                    "wrong-root": {**valid, "root": str(fixture.root / "elsewhere")},
                }
                for label, record in invalid.items():
                    with self.subTest(case=label):
                        manifest = json.loads(json.dumps(baseline))
                        target_state = manifest["consumers"]["agents-shared"]
                        target_state["nativeVerified"] = True
                        target_state.pop("nativeVerification", None)
                        target_state["nativeVerifications"]["codex"] = record
                        fixture.write_manifest(manifest)
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(_doctor(options), 3)

                fixture.write_manifest(baseline)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(_doctor(options), 0)


if __name__ == "__main__":
    unittest.main()
