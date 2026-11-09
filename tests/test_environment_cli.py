from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.cli import main


INVENTORY_COLLECTOR = (
    "local_agent_record_janitor.environment_inventory.collect_environment_inventory"
)


def _report() -> dict:
    return {
        "schema_version": "larj.environment-inventory.v1",
        "observed_at": "2026-11-07T00:00:00+00:00",
        "roots": {"user_home": "/home/测试", "appdata": None, "local_appdata": None},
        "runtimes": [
            {
                "id": "standalone",
                "client": "antigravity-cli",
                "owner": "antigravity-cli",
                "engine": "antigravity",
                "path": "/agent/bin/agy",
                "path_status": "present",
                "version": "1.2.3",
                "version_source": "package metadata",
                "discovered_via": ["path"],
                "selection": "unknown",
            },
            {
                "id": "frontend-backend",
                "client": "t3code",
                "owner": "t3code",
                "engine": "antigravity-acp",
                "path": "/frontend/bin/agy",
                "path_status": "present",
                "version": None,
                "version_source": None,
                "discovered_via": ["layout"],
                "selection": "unknown",
            },
        ],
        "bindings": [
            {
                "id": "frontend-binding",
                "client": "t3code",
                "engine": "antigravity-acp",
                "executable_id": "frontend-backend",
                "store_path": "/frontend/data",
                "store_status": "present",
                "evidence": ["layout"],
                "selection": "layout_hint",
                "configuration": [],
            }
        ],
        "findings": [],
        "coverage": {
            "status": "bounded",
            "clients": ["antigravity-cli", "t3code"],
            "probes": [],
            "issues": [],
            "limitations": ["Running processes were outside this probe."],
        },
    }


class EnvironmentCliTests(unittest.TestCase):
    def test_inventory_passes_overrides_and_preserves_evidence(self) -> None:
        report = _report()
        home = Path("fake home").absolute()
        appdata = Path("fake roaming").absolute()
        local_appdata = Path("fake local").absolute()
        output, errors = StringIO(), StringIO()
        with (
            patch(INVENTORY_COLLECTOR, return_value=report) as collect,
            patch("local_agent_record_janitor.cli.create_default_adapters") as adapters,
        ):
            code = main(
                [
                    "environment", "inventory", "--json",
                    "--home", str(home),
                    "--appdata", str(appdata),
                    "--local-appdata", str(local_appdata),
                    "--path-value", "",
                    "--client", "antigravity-cli",
                    "--client", "t3code",
                ],
                stdout=output,
                stderr=errors,
            )

        collect.assert_called_once_with(
            user_home=home,
            appdata=appdata,
            local_appdata=local_appdata,
            path_value="",
            clients=["antigravity-cli", "t3code"],
        )
        adapters.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_doctor_keeps_unknown_coverage_distinct_from_proven_findings(self) -> None:
        cases = (
            ("bounded", "info", "no_issues_observed", 0),
            ("partial", "info", "inconclusive", 0),
            ("partial", "warning", "attention", 0),
            ("partial", "error", "error", 1),
        )
        for coverage, severity, status, exit_code in cases:
            with self.subTest(coverage=coverage, severity=severity):
                report = _report()
                report["coverage"]["status"] = coverage
                report["findings"] = [{
                    "code": "diagnostic",
                    "severity": severity,
                    "message": "Observed diagnostic.",
                    "affected_ids": ["frontend-binding"],
                    "suggestion": "Check frontend launch settings.",
                }]
                output = StringIO()
                with patch(INVENTORY_COLLECTOR, return_value=report):
                    code = main(
                        ["environment", "doctor", "--json"],
                        stdout=output,
                        stderr=StringIO(),
                    )
                self.assertEqual(code, exit_code)
                self.assertEqual(json.loads(output.getvalue()), {
                    **report,
                    "schema_version": "larj.environment-doctor.v1",
                    "status": status,
                })
                self.assertEqual(report["schema_version"], "larj.environment-inventory.v1")

    def test_human_output_distinguishes_cli_and_acp_and_escapes_paths(self) -> None:
        report = _report()
        report["runtimes"][1]["path"] += "\x1b[1m\nextra"
        output = StringIO()
        with patch(INVENTORY_COLLECTOR, return_value=report):
            code = main(
                ["environment", "doctor"],
                stdout=output,
                stderr=StringIO(),
            )

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("antigravity-cli / antigravity", rendered)
        self.assertIn("t3code / antigravity-acp", rendered)
        self.assertIn("layout_hint", rendered)
        self.assertIn("1.2.3 [package metadata]", rendered)
        self.assertIn(r"/frontend/bin/agy\x1b[1m\nextra", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_help_and_invalid_arguments_do_not_probe(self) -> None:
        with patch(INVENTORY_COLLECTOR) as collect:
            output = StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main(["environment", "inventory", "--help"], stdout=output)
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("--client", output.getvalue())
            self.assertIn("antigravity-cli", output.getvalue())

            for options, error in (
                (["--client", "unknown-client"], "invalid choice"),
                (["--home", "  "], "请输入有效的绝对目录路径"),
                (["--appdata", "relative"], "请输入有效的绝对目录路径"),
                (["--path-value", "\0"], "NUL"),
            ):
                with self.subTest(options=options):
                    errors = StringIO()
                    with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
                        main(["environment", "doctor", *options], stderr=errors)
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn(error, errors.getvalue())
            collect.assert_not_called()

    def test_probe_failure_has_a_structured_error_in_json_mode(self) -> None:
        output, errors = StringIO(), StringIO()
        with patch(INVENTORY_COLLECTOR, side_effect=PermissionError("probe denied")):
            code = main(
                ["environment", "inventory", "--json"],
                stdout=output,
                stderr=errors,
            )

        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue()), {
            "schema_version": "larj.environment-error.v1",
            "status": "error",
            "code": "environment_probe_failed",
            "message": "probe denied",
        })


if __name__ == "__main__":
    unittest.main()
