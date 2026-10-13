from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


VERIFY_PATH = Path(__file__).resolve().parents[1] / "verify.py"
SPEC = importlib.util.spec_from_file_location("agent_verify", VERIFY_PATH)
assert SPEC and SPEC.loader
agent_verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_verify)


def write_candidate(
    tmp_path: Path,
    path: str,
    content: str,
    target: str = "backend/app/core/config.py",
) -> Path:
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "candidate_proposed",
                "target": target,
                "proposal": {
                    "decision": "tests_needed",
                    "reason": "A focused case is missing.",
                    "files": [{"path": path, "content": content}],
                    "intended_cases": ["Exercise the missing behavior."],
                },
            }
        ),
        encoding="utf-8",
    )
    return candidate


def test_load_candidate_revalidates_new_test_file(tmp_path):
    candidate = write_candidate(
        tmp_path,
        "backend/tests/core/test_config.py",
        "def test_config():\n    assert True\n",
    )

    assert agent_verify.load_candidate(candidate) == (
        "backend",
        [
            {
                "path": "backend/tests/core/test_config.py",
                "content": "def test_config():\n    assert True\n",
            }
        ],
        None,
    )


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("../../test_escape.py", "def test_escape():\n    assert True\n"),
        ("backend/app/test_config.py", "def test_config():\n    assert True\n"),
        (
            "backend/tests/test_disabled.py",
            "@pytest.mark.skip\ndef test_disabled():\n    assert True\n",
        ),
    ],
)
def test_load_candidate_rejects_policy_violations(tmp_path, path, content):
    candidate = write_candidate(tmp_path, path, content)

    with pytest.raises(ValueError):
        agent_verify.load_candidate(candidate)


def test_make_patch_includes_untracked_test_file(tmp_path):
    content = "def test_new_case():\n    assert True\n"

    patch = agent_verify.make_patch(
        tmp_path,
        [{"path": "backend/tests/test_new_case.py", "content": content}],
    )

    assert "--- /dev/null" in patch
    assert "+++ b/backend/tests/test_new_case.py" in patch
    assert "+def test_new_case():" in patch


def test_load_candidate_accepts_frontend_test(tmp_path):
    candidate = write_candidate(
        tmp_path,
        "frontend/src/lib/http.test.ts",
        "import { test } from 'vitest'\ntest('request', () => {})\n",
        target="frontend/src/lib/http.ts",
    )

    suite, files, expected_head = agent_verify.load_candidate(candidate)

    assert suite == "frontend"
    assert files[0]["path"] == "frontend/src/lib/http.test.ts"
    assert expected_head is None


def test_load_candidate_accepts_mixed_test_files(tmp_path):
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "candidate_proposed",
                "target": None,
                "targets": [
                    "backend/app/core/config.py",
                    "frontend/src/lib/http.ts",
                ],
                "ecosystem": "mixed",
                "proposal": {
                    "decision": "tests_needed",
                    "reason": "Both changed behaviors need focused tests.",
                    "files": [
                        {
                            "path": "backend/tests/test_config.py",
                            "content": "def test_config():\n    assert True\n",
                        },
                        {
                            "path": "frontend/src/lib/http.test.ts",
                            "content": "test('request', () => {})\n",
                        },
                    ],
                    "intended_cases": ["Backend behavior.", "Frontend behavior."],
                },
            }
        )
    )

    suite, files, expected_head = agent_verify.load_candidate(candidate)

    assert suite == "mixed"
    assert len(files) == 2
    assert expected_head is None


def test_snapshot_identity_must_match_candidate_head(tmp_path):
    (tmp_path / ".test-agent-snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "head_sha": "a" * 40,
            }
        )
    )

    agent_verify.validate_snapshot_identity(tmp_path, "a" * 40)
    with pytest.raises(ValueError, match="does not match"):
        agent_verify.validate_snapshot_identity(tmp_path, "b" * 40)
    assert agent_verify.load_snapshot_head(tmp_path) == "a" * 40


def test_validate_against_workspace_rejects_replacing_existing_tests(tmp_path):
    existing = tmp_path / "backend/tests/test_existing.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("def test_existing():\n    assert True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="append-only"):
        agent_verify.validate_against_workspace(
            tmp_path,
            [
                {
                    "path": "backend/tests/test_existing.py",
                    "content": "def test_replacement():\n    assert False\n",
                }
            ],
        )


def test_failure_classification_keeps_assertion_failures_ambiguous():
    assert (
        agent_verify.classify_candidate_failure("AssertionError: expected value")
        == "possible_application_bug_or_unclassified_failure"
    )
    assert (
        agent_verify.classify_candidate_failure("SyntaxError: invalid syntax")
        == "generated_test_failure"
    )
    assert (
        agent_verify.classify_baseline_failure("Vite Startup Error")
        == "infrastructure_failure"
    )
    assert (
        agent_verify.classify_baseline_failure("FAILED tests/test_existing.py")
        == "baseline_failure"
    )


def test_baseline_only_requires_an_explicit_suite(monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify.py",
            "--baseline-only",
            "--workspace",
            str(tmp_path),
            "--output-dir",
            str(output),
        ],
    )

    assert agent_verify.main() == 1
    result = json.loads((output / "verification.json").read_text())
    assert result["status"] == "invalid_proposal"
    assert "--suite" in result["reason"]
