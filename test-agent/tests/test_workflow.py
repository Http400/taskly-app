from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT))

from workflow import (
    ArtifactError,
    inspect_generation,
    inspect_scope,
    render_report,
)


def write_artifact(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps({"schema_version": 1, **value}))
    return path


def output_values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
    )


def test_inspect_scope_emits_validated_outputs(tmp_path):
    artifact = write_artifact(
        tmp_path,
        {
            "status": "eligible",
            "suite": "mixed",
            "head_sha": "a" * 40,
        },
    )
    output = tmp_path / "output"

    inspect_scope(artifact, output)

    assert output_values(output) == {
        "status": "eligible",
        "suite": "mixed",
        "eligible": "true",
        "head_sha": "a" * 40,
    }


def test_generation_candidate_is_bound_to_expected_head(tmp_path):
    artifact = write_artifact(
        tmp_path,
        {
            "status": "candidate_proposed",
            "ecosystem": "frontend",
            "head_sha": "b" * 40,
        },
    )
    output = tmp_path / "output"

    inspect_generation(artifact, output, "b" * 40)

    assert output_values(output)["has_candidate"] == "true"
    with pytest.raises(ArtifactError, match="does not match"):
        inspect_generation(artifact, output, "c" * 40)


def test_rejects_unknown_status(tmp_path):
    artifact = write_artifact(
        tmp_path,
        {
            "status": "pretend_success",
            "suite": "backend",
            "head_sha": "a" * 40,
        },
    )

    with pytest.raises(ArtifactError, match="status"):
        inspect_scope(artifact, tmp_path / "output")


def test_rejects_oversized_artifact(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    monkeypatch.setattr("workflow.MAX_ARTIFACT_BYTES", 1)

    with pytest.raises(ArtifactError, match="exceeds"):
        inspect_scope(artifact, tmp_path / "output")


def test_render_report_uses_verified_facts_and_sanitizes_model_text(tmp_path):
    head = "d" * 40
    scope = write_artifact(
        tmp_path,
        {
            "status": "eligible",
            "suite": "backend",
            "head_sha": head,
            "reason": "Supported change.",
        },
    )
    generation = tmp_path / "generation.json"
    generation.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "candidate_proposed",
                "head_sha": head,
                "reason": "<!-- forged --> needs | tests",
                "skills_loaded": ["analyze-changes", "backend-unit-tests"],
                "trace": [
                    {
                        "tool": "read_file",
                        "subject": "backend/app/config.py",
                        "outcome": "read 10 characters",
                    }
                ],
                "proposal": {
                    "files": [{"path": "backend/tests/test_config.py"}]
                },
            }
        )
    )
    verification = tmp_path / "verification.json"
    verification.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "verified_tests",
                "head_sha": head,
                "baseline": {"command": "pytest", "exit_code": 0},
                "candidate": {"command": "pytest", "exit_code": 0},
            }
        )
    )

    report = render_report(
        scope_path=scope,
        generation_path=generation,
        verification_path=verification,
        baseline_path=None,
        expected_head=head,
        run_url="https://github.example/run/1",
        scope_job="success",
        baseline_job="success",
        generation_job="success",
        verification_job="success",
    )

    assert "**Decision:** Verified tests" in report
    assert "&lt;!-- forged --&gt;" in report
    assert "needs \\| tests" in report
    assert "`backend/tests/test_config.py`" in report
    assert "Automatic repair is disabled." in report


def test_render_report_distinguishes_skipped_generation(tmp_path):
    scope = write_artifact(
        tmp_path,
        {
            "status": "eligible",
            "suite": "frontend",
            "head_sha": "e" * 40,
            "reason": "Supported change.",
        },
    )

    report = render_report(
        scope_path=scope,
        generation_path=None,
        verification_path=None,
        baseline_path=None,
        expected_head="e" * 40,
        run_url="https://github.example/run/2",
        scope_job="success",
        baseline_job="success",
        generation_job="skipped",
        verification_job="skipped",
    )

    assert "**Decision:** Agent skipped" in report
    assert "Any generated patch is diagnostic only" in report
