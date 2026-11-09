import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.action_registry import action_capability
from local_agent_record_janitor.agent_cli import _load_authorized_plan
from local_agent_record_janitor.cleanup_service import CleanupService, selected_platforms
from local_agent_record_janitor.cli import build_parser
from local_agent_record_janitor.office_store import PROFILES, OfficeAdapter
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_store import plan_sha256
from local_agent_record_janitor.record_identity import capability_for
from local_agent_record_janitor.retired_clients import require_active_plan


class RetiredClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "legacy-plan.json"
        self.fixtures = json.loads(
            (Path(__file__).parent / "fixtures" / "operation_v1.json").read_text(encoding="utf-8")
        )

    def freeze(self, document):
        document["plan_sha256"] = plan_sha256(document)
        self.path.write_text(json.dumps(document), encoding="utf-8")
        return self.path.read_bytes()

    def test_removed_public_selectors_and_paths_are_rejected(self):
        parser = build_parser()
        for option, value in (
            ("--client", "aionui"), ("--client", "qoderwork"),
            ("--platform", "aionui"), ("--platform", "qoderwork"),
            ("--aionui-db", "old.db"), ("--aionui-codex-home", "old-home"),
            ("--qoderwork-root", "old-profile"), ("--qoderwork-sdk-root", "old-sdk"),
        ):
            with self.subTest(option=option, value=value), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    parser.parse_args(["records", option, value])
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(selected_platforms(None), {"native", "cindy"})
        self.assertEqual(selected_platforms(["all"]), {"native", "cindy"})
        for client in ("aionui", "aion-ui", "qoderwork", "qoderwork-cn"):
            with self.subTest(client=client):
                with self.assertRaisesRegex(ValueError, "client_support_removed"):
                    selected_platforms([client])
                self.assertEqual(capability_for(client, "codex").mode, "unsupported")
        self.assertEqual(set(PROFILES), {"qwenwork"})
        with self.assertRaises(ValueError):
            OfficeAdapter(client="qoderwork", profile_root=self.root)
        self.assertFalse(action_capability("delete_project_item").implemented)

    def test_new_operation_scopes_cannot_reenable_removed_clients(self):
        for client in ("aionui", "qoderwork"):
            with self.subTest(client=client), patch.object(OperationCoordinator, "_build_context") as inventory:
                result = OperationCoordinator(CleanupService()).plan_operation(client=client, record_ids=("test",))
                self.assertFalse(result["modified"])
                self.assertFalse(result["mutation_started"])
                self.assertIn("client_support_removed", json.dumps(result))
                inventory.assert_not_called()

    def test_hashed_legacy_operations_stop_before_guard_reads_or_recovery(self):
        for client in ("aionui", "qoderwork"):
            for nested in (False, True):
                document = copy.deepcopy(self.fixtures["operation_plan"])
                if nested:
                    # Native-scoped approvals may contain a retired frontend
                    # closure even though their top-level client is supported.
                    document["scope"]["client"] = "native"
                    document["actions"][0]["impact"]["frontend_reference_evidence"] = [{"platform": client}]
                else:
                    document["scope"]["client"] = client
                before = self.freeze(document)
                for operation in ("apply_operation", "status_operation", "verify_operation"):
                    with self.subTest(client=client, nested=nested, operation=operation), patch(
                        "local_agent_record_janitor.operation_coordinator.validate_guard_sources"
                    ) as guards:
                        result = getattr(OperationCoordinator(CleanupService()), operation)(
                            plan_path=self.path, clients_closed=True,
                            plan_sha256=document["plan_sha256"],
                        )
                        self.assertFalse(result["modified"])
                        self.assertFalse(result["mutation_started"])
                        self.assertIn("client_support_removed", json.dumps(result))
                        guards.assert_not_called()
                        self.assertEqual(self.path.read_bytes(), before)
                        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_hashed_agent_plans_reject_retired_scopes_and_nested_references(self):
        for client in ("aionui", "qoderwork"):
            for nested in (False, True):
                with self.subTest(client=client, nested=nested):
                    document = copy.deepcopy(self.fixtures["agent_plan"])
                    if nested:
                        action = document["authorization"]["root_actions"][0]
                        action["impact"]["frontend_reference_evidence"] = [{"owner_client": client}]
                    else:
                        document["scan_options"]["platforms"] = [client]
                    before = self.freeze(document)
                    accepted, result = _load_authorized_plan(self.path, document["plan_sha256"])
                    self.assertIsNone(accepted)
                    self.assertFalse(result["modified"])
                    self.assertFalse(result["mutation_started"])
                    self.assertIn("client_support_removed", json.dumps(result))
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_retired_words_in_paths_and_display_metadata_are_not_identities(self):
        require_active_plan({"scope": {"client": "native"},
            "path": "D:/work/aionui", "title": "qoderwork", "engines": ["codex"]})
        for identity in ("AIONUI", "aion_ui", "QoderWork", "qoderwork_cn"):
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "client_support_removed"):
                require_active_plan({"guard_sources": [{"bindings": [{"owner_client": identity}]}]})
