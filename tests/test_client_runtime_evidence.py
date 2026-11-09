from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
if str(CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTS_DIR))

from work_skills_clients import (  # noqa: E402
    PythonRuntimeVerification,
    RuntimeEvidenceError,
    capture_python_runtime,
    detect_consumers,
    load_client_model,
    validate_python_runtime_evidence_for_policy,
    validate_python_runtime_evidence_freshness,
)
from local_agent_manager.skills.probes.capture_runtime_evidence import write_evidence_exclusive  # noqa: E402


SYSTEM_PYTHON = "C:/Users/test/AppData/Local/Programs/Python/Python312/python.exe"
SYSTEM_PREFIX = "C:/Users/test/AppData/Local/Programs/Python/Python312"
STANDALONE_HOME = "C:/Users/test/.codex"
CINDY_HOME = "C:/Users/test/AppData/Roaming/CindyGlobal/codex-home"
CAPTURED_AT = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)


def policy(
    *, context_id: str = "standalone-codex", expected: str = STANDALONE_HOME,
    allow_unset: bool = True,
) -> PythonRuntimeVerification:
    return PythonRuntimeVerification(
        status="required",
        context_id=context_id,
        capture_command="python clients/capture_runtime_evidence.py",
        python_command="python",
        required_version="3.12",
        context_environment="CODEX_HOME",
        expected_context=Path(expected),
        allow_unset_context=allow_unset,
    )


def capture(
    adapter_id: str = "codex",
    consumer_ids: tuple[str, ...] = ("codex",),
    runtime_policy: PythonRuntimeVerification | None = None,
    **overrides,
) -> dict:
    kwargs = {
        "environ": {"CODEX_HOME": STANDALONE_HOME, "PATH": "C:/unused"},
        "executable": SYSTEM_PYTHON,
        "resolved_python": SYSTEM_PYTHON,
        "version_info": (3, 12, 10),
        "prefix": SYSTEM_PREFIX,
        "base_prefix": SYSTEM_PREFIX,
        "captured_at": CAPTURED_AT,
    }
    kwargs.update(overrides)
    return capture_python_runtime(
        adapter_id,
        consumer_ids,
        runtime_policy or policy(),
        **kwargs,
    )


class ClientRuntimeEvidenceTests(unittest.TestCase):
    def test_valid_evidence_records_actual_system_python_and_context(self):
        evidence = capture()

        self.assertEqual(evidence["adapterId"], "codex")
        self.assertEqual(evidence["consumerIds"], ["codex"])
        self.assertEqual(evidence["contextId"], "standalone-codex")
        self.assertEqual(evidence["python"]["executable"], SYSTEM_PYTHON)
        self.assertEqual(evidence["python"]["resolvedExecutable"], SYSTEM_PYTHON)
        self.assertEqual(evidence["python"]["versionInfo"], [3, 12, 10])
        self.assertEqual(evidence["python"]["prefix"], SYSTEM_PREFIX)
        self.assertEqual(evidence["python"]["basePrefix"], SYSTEM_PREFIX)
        self.assertFalse(evidence["python"]["isVirtualEnvironment"])
        self.assertEqual(
            validate_python_runtime_evidence_for_policy(
                "codex", ("codex",), policy(), evidence
            ),
            (),
        )

    def test_evidence_cannot_cross_adapters_or_client_contexts(self):
        standalone = capture()
        cindy_policy = policy(
            context_id="cindy-codex", expected=CINDY_HOME, allow_unset=False
        )

        errors = validate_python_runtime_evidence_for_policy(
            "cindy-codex", ("cindy-codex",), cindy_policy, standalone
        )

        message = "\n".join(errors)
        self.assertIn("belongs to adapter", message)
        self.assertIn("consumerIds", message)
        self.assertIn("contextId", message)
        self.assertIn("policyFingerprint", message)
        self.assertIn("effectiveValue", message)

    def test_wrong_python_series_is_rejected(self):
        with self.assertRaises(RuntimeEvidenceError) as caught:
            capture(version_info=(3, 14, 6))

        self.assertIn("Python 3.12.x is required", str(caught.exception))

    def test_virtual_environment_is_rejected(self):
        with self.assertRaises(RuntimeEvidenceError) as caught:
            capture(
                executable="C:/repo/.venv/Scripts/python.exe",
                resolved_python="C:/repo/.venv/Scripts/python.exe",
                prefix="C:/repo/.venv",
                base_prefix=SYSTEM_PREFIX,
            )

        self.assertIn("virtual environments are not allowed", str(caught.exception))

    def test_private_runtime_paths_are_rejected(self):
        private_paths = (
            "C:/Users/test/.codex/runtime/python.exe",
            "C:/Users/test/workbuddy/runtime/python.exe",
            "C:/Users/test/.cache/codex-runtimes/python.exe",
            "C:/Users/test/codex-runtimes/python.exe",
            "C:/Users/test/AppData/Roaming/CindyGlobal/codex/0.145.0/python.exe",
            "C:/Users/test/AppData/Roaming/CindyGlobal/codex-home/python.exe",
            "C:/Users/test/.workbuddy/runtime/python.exe",
            "C:/repo/.work-skills-runtime/venv/Scripts/python.exe",
        )
        for private_python in private_paths:
            with self.subTest(private_python=private_python):
                private_prefix = str(Path(private_python).parent)
                with self.assertRaises(RuntimeEvidenceError) as caught:
                    capture(
                        executable=private_python,
                        resolved_python=private_python,
                        prefix=private_prefix,
                        base_prefix=private_prefix,
                    )
                self.assertIn("forbidden private runtime", str(caught.exception))

    def test_cindy_context_marker_cannot_capture_standalone_evidence(self):
        with self.assertRaises(RuntimeEvidenceError) as caught:
            capture(
                environ={"CODEX_HOME": CINDY_HOME, "PATH": "C:/unused"},
            )

        self.assertIn("identifies a different client context", str(caught.exception))

    def test_required_cindy_context_marker_cannot_be_omitted(self):
        cindy_policy = policy(
            context_id="cindy-codex", expected=CINDY_HOME, allow_unset=False
        )
        with self.assertRaises(RuntimeEvidenceError) as caught:
            capture(
                "cindy-codex",
                ("cindy-codex",),
                cindy_policy,
                environ={"PATH": "C:/unused"},
            )

        self.assertIn("must be set by the cindy-codex client context", str(caught.exception))

    def test_running_python_must_match_path_or_explicit_system_override(self):
        with self.assertRaises(RuntimeEvidenceError) as caught:
            capture(resolved_python="C:/Python312/python.exe")

        self.assertIn("running interpreter does not match", str(caught.exception))

    def test_freshness_is_a_verify_time_gate_not_structural_expiry(self):
        evidence = capture()
        runtime_policy = policy()

        self.assertEqual(
            validate_python_runtime_evidence_for_policy(
                "codex", ("codex",), runtime_policy, evidence
            ),
            (),
        )
        self.assertEqual(
            validate_python_runtime_evidence_freshness(evidence, now=CAPTURED_AT),
            (),
        )

        future = {**evidence, "capturedAt": "2026-08-06T08:31:00Z"}
        stale_now = datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc)
        self.assertIn(
            "future",
            "\n".join(
                validate_python_runtime_evidence_freshness(
                    future, now=CAPTURED_AT
                )
            ),
        )
        self.assertIn(
            "stale",
            "\n".join(
                validate_python_runtime_evidence_freshness(
                    evidence, now=stale_now
                )
            ),
        )
        # The same old record remains structurally/policy valid in a manifest.
        self.assertEqual(
            validate_python_runtime_evidence_for_policy(
                "codex", ("codex",), runtime_policy, evidence
            ),
            (),
        )

    def test_low_level_writer_never_overwrites_files_or_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "evidence.json"
            existing.write_text("original\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                write_evidence_exclusive(existing, "replacement\n")
            self.assertEqual(existing.read_text(encoding="utf-8"), "original\n")

            target = root / "target.json"
            target.write_text("target\n", encoding="utf-8")
            link = root / "evidence-link.json"
            try:
                link.symlink_to(target)
            except OSError:
                return
            with self.assertRaises(FileExistsError):
                write_evidence_exclusive(link, "replacement\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "target\n")




if __name__ == "__main__":
    unittest.main()
