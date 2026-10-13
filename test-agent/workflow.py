from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any


MAX_ARTIFACT_BYTES = 1_000_000
SCOPE_STATUSES = {"eligible", "no_tests_needed", "skipped_ineligible"}
GENERATION_STATUSES = {
    "candidate_proposed",
    "no_tests_needed",
    "skipped_ineligible",
    "invalid_proposal",
    "limit_exhausted",
    "model_infrastructure_failure",
}
SUITES = {"backend", "frontend", "mixed", "none"}
JOB_RESULTS = {"success", "failure", "skipped", "cancelled"}
VERIFICATION_STATUSES = {
    "verified_tests",
    "verified_baseline",
    "baseline_failure",
    "generated_test_failure",
    "possible_application_bug_or_unclassified_failure",
    "infrastructure_failure",
    "invalid_proposal",
}


class ArtifactError(Exception):
    pass


def load_object(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ArtifactError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError(f"could not read valid JSON artifact: {path}") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ArtifactError("artifact must be a version 1 JSON object")
    return value


def write_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ArtifactError(f"multiline workflow output is forbidden: {key}")
            output.write(f"{key}={value}\n")


def inspect_scope(path: Path, github_output: Path) -> None:
    result = load_object(path)
    status = result.get("status")
    suite = result.get("suite")
    head_sha = result.get("head_sha")
    if status not in SCOPE_STATUSES or suite not in SUITES:
        raise ArtifactError("scope status or suite is invalid")
    if not isinstance(head_sha, str) or len(head_sha) != 40:
        raise ArtifactError("scope head SHA is invalid")
    write_outputs(
        github_output,
        {
            "status": status,
            "suite": suite,
            "eligible": str(status == "eligible").lower(),
            "head_sha": head_sha,
        },
    )


def inspect_generation(
    path: Path,
    github_output: Path,
    expected_head: str,
) -> None:
    result = load_object(path)
    status = result.get("status")
    if status not in GENERATION_STATUSES:
        raise ArtifactError("generation status is invalid")
    actual_head = result.get("head_sha")
    if actual_head is not None and actual_head != expected_head:
        raise ArtifactError("generation artifact head does not match the workflow")
    suite = result.get("ecosystem", "none")
    if suite not in SUITES:
        raise ArtifactError("generation ecosystem is invalid")
    write_outputs(
        github_output,
        {
            "status": status,
            "suite": suite,
            "has_candidate": str(status == "candidate_proposed").lower(),
        },
    )


def safe_text(value: Any, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        character if character >= " " else " "
        for character in value
    )
    return html.escape(cleaned[:limit]).replace("|", "\\|")


def load_optional(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return load_object(path)


def validated_job_result(value: str) -> str:
    if value not in JOB_RESULTS:
        raise ArtifactError(f"invalid job result: {value}")
    return value


def validate_head(result: dict[str, Any], expected_head: str) -> None:
    actual_head = result.get("head_sha")
    if actual_head is not None and actual_head != expected_head:
        raise ArtifactError("artifact head does not match the report head")


def render_report(
    *,
    scope_path: Path,
    generation_path: Path | None,
    verification_path: Path | None,
    baseline_path: Path | None,
    expected_head: str,
    run_url: str,
    scope_job: str,
    baseline_job: str,
    generation_job: str,
    verification_job: str,
) -> str:
    job_results = {
        "Scope": validated_job_result(scope_job),
        "Baseline": validated_job_result(baseline_job),
        "Generation": validated_job_result(generation_job),
        "Verification": validated_job_result(verification_job),
    }
    scope = load_object(scope_path)
    validate_head(scope, expected_head)
    if scope.get("status") not in SCOPE_STATUSES:
        raise ArtifactError("scope artifact has an invalid status")
    generation = load_optional(generation_path)
    verification = load_optional(verification_path)
    baseline = load_optional(baseline_path)
    if generation:
        validate_head(generation, expected_head)
        if generation.get("status") not in GENERATION_STATUSES:
            raise ArtifactError("generation artifact has an invalid status")
    if verification:
        validate_head(verification, expected_head)
        if verification.get("status") not in VERIFICATION_STATUSES:
            raise ArtifactError("verification artifact has an invalid status")
    if baseline:
        validate_head(baseline, expected_head)
        if baseline.get("status") not in VERIFICATION_STATUSES:
            raise ArtifactError("baseline artifact has an invalid status")

    verification_status = verification.get("status") if verification else None
    generation_status = generation.get("status") if generation else None
    scope_status = scope["status"]
    if verification_status == "verified_tests":
        decision = "Verified tests"
    elif verification_status:
        decision = safe_text(verification_status.replace("_", " ").title())
    elif generation_status == "no_tests_needed" or scope_status == "no_tests_needed":
        decision = "No tests needed"
    elif scope_status == "skipped_ineligible":
        decision = "Skipped / manual review"
    elif generation_job == "skipped":
        decision = "Agent skipped"
    elif generation_status:
        decision = safe_text(generation_status.replace("_", " ").title())
    else:
        decision = "No generation result"

    generation_reason = generation.get("reason") if generation else None
    if not generation_reason and generation:
        proposal = generation.get("proposal")
        if isinstance(proposal, dict):
            generation_reason = proposal.get("reason")
    reason = generation_reason or scope.get("reason", "")
    lines = [
        "<!-- taskly-unit-test-agent -->",
        "## Unit test agent",
        "",
        f"**Decision:** {decision}",
        f"**Analyzed head:** `{expected_head}`",
        f"**Reason:** {safe_text(reason)}",
        "",
        "| Stage | Job result | Artifact status |",
        "|---|---|---|",
    ]
    artifact_statuses = {
        "Scope": scope_status,
        "Baseline": baseline.get("status") if baseline else "not available",
        "Generation": generation_status or "not available",
        "Verification": verification_status or "not available",
    }
    for stage, job_result in job_results.items():
        lines.append(
            f"| {stage} | {job_result} | "
            f"{safe_text(artifact_statuses[stage])} |"
        )

    if generation:
        skills = generation.get("skills_loaded")
        if isinstance(skills, list):
            validated_skills = [
                safe_text(skill, 100)
                for skill in skills
                if isinstance(skill, str)
            ]
            lines.extend(
                [
                    "",
                    "**Skills actually loaded:** "
                    + (", ".join(f"`{skill}`" for skill in validated_skills) or "None"),
                ]
            )
        trace = generation.get("trace")
        if isinstance(trace, list) and trace:
            lines.extend(["", "**Sanitized tool trace:**"])
            for index, entry in enumerate(trace[:30], start=1):
                if not isinstance(entry, dict):
                    continue
                lines.append(
                    f"{index}. `{safe_text(entry.get('tool'), 100)}` — "
                    f"`{safe_text(entry.get('subject'), 300)}` — "
                    f"{safe_text(entry.get('outcome'), 300)}"
                )
        proposal = generation.get("proposal")
        files = proposal.get("files") if isinstance(proposal, dict) else None
        if isinstance(files, list) and files:
            lines.extend(["", "**Changed test paths:**"])
            for item in files[:10]:
                if isinstance(item, dict):
                    lines.append(f"- `{safe_text(item.get('path'), 500)}`")

    evidence = verification or baseline
    if evidence:
        lines.extend(["", "**Execution evidence:**"])
        for label in ("baseline", "candidate"):
            execution = evidence.get(label)
            if isinstance(execution, dict):
                lines.append(
                    f"- {label.title()}: `{safe_text(execution.get('command'), 1_000)}` "
                    f"(exit `{safe_text(str(execution.get('exit_code')), 20)}`)"
                )

    patch_label = (
        "The downloadable patch was verified against this exact head."
        if verification_status == "verified_tests"
        else "Any generated patch is diagnostic only and is not verified."
    )
    lines.extend(
        [
            "",
            patch_label,
            "Automatic repair is disabled.",
            f"[Workflow run and artifacts]({safe_text(run_url, 1_000)})",
        ]
    )
    report = "\n".join(lines) + "\n"
    if len(report) > 60_000:
        raise ArtifactError("rendered report exceeds 60,000 characters")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate test-agent artifacts for GitHub Actions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    scope = subparsers.add_parser("inspect-scope")
    scope.add_argument("--input", type=Path, required=True)
    scope.add_argument("--github-output", type=Path, required=True)
    generation = subparsers.add_parser("inspect-generation")
    generation.add_argument("--input", type=Path, required=True)
    generation.add_argument("--github-output", type=Path, required=True)
    generation.add_argument("--expected-head", required=True)
    report = subparsers.add_parser("render-report")
    report.add_argument("--scope", type=Path, required=True)
    report.add_argument("--generation", type=Path)
    report.add_argument("--verification", type=Path)
    report.add_argument("--baseline", type=Path)
    report.add_argument("--expected-head", required=True)
    report.add_argument("--run-url", required=True)
    report.add_argument("--scope-job", required=True)
    report.add_argument("--baseline-job", required=True)
    report.add_argument("--generation-job", required=True)
    report.add_argument("--verification-job", required=True)
    report.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "inspect-scope":
            inspect_scope(args.input, args.github_output)
        elif args.command == "inspect-generation":
            inspect_generation(
                args.input,
                args.github_output,
                args.expected_head,
            )
        else:
            rendered = render_report(
                scope_path=args.scope,
                generation_path=args.generation,
                verification_path=args.verification,
                baseline_path=args.baseline,
                expected_head=args.expected_head,
                run_url=args.run_url,
                scope_job=args.scope_job,
                baseline_job=args.baseline_job,
                generation_job=args.generation_job,
                verification_job=args.verification_job,
            )
            args.output.write_text(rendered, encoding="utf-8")
    except (ArtifactError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
