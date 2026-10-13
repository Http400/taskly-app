from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


MAX_LOG_CHARS = 30_000
MAX_PROPOSAL_CHARS = 100_000
FORBIDDEN_TEST_MARKERS = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "pytest.skip(",
    "@unittest.skip",
    "unittest.skip(",
    ".skip(",
    ".only(",
)


def load_candidate(
    path: Path,
) -> tuple[str, list[dict[str, str]], str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("status") != "candidate_proposed":
        raise ValueError("input is not a version 1 candidate proposal")
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        target = data.get("target")
        raw_targets = [target] if isinstance(target, str) else []
    target_suites: set[str] = set()
    for target in raw_targets:
        if isinstance(target, str) and target.startswith("backend/app/"):
            target_suites.add("backend")
        elif isinstance(target, str) and target.startswith("frontend/src/"):
            target_suites.add("frontend")
        else:
            raise ValueError("candidate target is not supported")
    if not target_suites:
        raise ValueError("candidate target is not supported")
    suite = next(iter(target_suites)) if len(target_suites) == 1 else "mixed"
    if data.get("ecosystem", suite) != suite:
        raise ValueError("candidate ecosystem does not match its target")
    proposal = data.get("proposal")
    if (
        not isinstance(proposal, dict)
        or set(proposal) != {"decision", "reason", "files", "intended_cases"}
        or proposal.get("decision") != "tests_needed"
        or not isinstance(proposal.get("reason"), str)
        or not proposal["reason"].strip()
        or not isinstance(proposal.get("intended_cases"), list)
        or not proposal["intended_cases"]
        or not all(
            isinstance(case, str) and case.strip()
            for case in proposal["intended_cases"]
        )
    ):
        raise ValueError("candidate does not contain a tests_needed proposal")
    files = proposal.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 5:
        raise ValueError("candidate must contain 1-5 test files")

    validated: list[dict[str, str]] = []
    seen: set[str] = set()
    total_chars = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise ValueError("candidate file shape is invalid")
        raw_path = item["path"]
        content = item["content"]
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\x00" in raw_path
            or "\\" in raw_path
        ):
            raise ValueError("candidate path must be a repository-relative POSIX path")
        relative = PurePosixPath(raw_path)
        value = relative.as_posix()
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or value in seen
            or not isinstance(content, str)
            or not content.strip()
        ):
            raise ValueError(f"candidate path or content is invalid: {value}")
        if suite == "backend":
            allowed_path = (
                value.startswith("backend/tests/")
                and relative.suffix == ".py"
                and relative.name.startswith("test_")
            )
        else:
            frontend_allowed = value.startswith("frontend/src/") and (
                relative.name.endswith(".test.ts")
                or relative.name.endswith(".test.tsx")
            )
            if suite == "mixed":
                backend_allowed = (
                    value.startswith("backend/tests/")
                    and relative.suffix == ".py"
                    and relative.name.startswith("test_")
                )
                allowed_path = backend_allowed or frontend_allowed
            else:
                allowed_path = frontend_allowed
        if not allowed_path:
            raise ValueError(f"candidate path is not an approved {suite} test: {value}")
        if any(marker in content for marker in FORBIDDEN_TEST_MARKERS):
            raise ValueError(f"candidate disables tests: {value}")
        total_chars += len(content)
        if total_chars > MAX_PROPOSAL_CHARS:
            raise ValueError("candidate test content exceeds the size limit")
        seen.add(value)
        validated.append({"path": value, "content": content})
    expected_head = data.get("head_sha")
    if expected_head is not None and (
        not isinstance(expected_head, str)
        or len(expected_head) != 40
        or any(character not in "0123456789abcdef" for character in expected_head)
    ):
        raise ValueError("candidate head SHA is invalid")
    return suite, validated, expected_head


def load_snapshot_head(workspace: Path) -> str | None:
    manifest_path = workspace / ".test-agent-snapshot.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("exact-head snapshot manifest is missing or invalid") from error
    head_sha = manifest.get("head_sha") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(head_sha, str)
        or len(head_sha) != 40
    ):
        raise ValueError("exact-head snapshot manifest is invalid")
    return head_sha


def validate_snapshot_identity(workspace: Path, expected_head: str | None) -> None:
    actual_head = load_snapshot_head(workspace)
    if expected_head is None:
        return
    if actual_head != expected_head:
        raise ValueError("candidate head does not match the mounted snapshot")


def copy_workspace(source: Path, destination: Path) -> None:
    destination.mkdir()
    shutil.copytree(source / "backend/app", destination / "backend/app")
    shutil.copytree(source / "backend/tests", destination / "backend/tests")
    (destination / "backend").mkdir(exist_ok=True)
    shutil.copy2(
        source / "backend/pyproject.toml",
        destination / "backend/pyproject.toml",
    )
    shutil.copy2(source / "pytest.ini", destination / "pytest.ini")
    shutil.copytree(source / "frontend/src", destination / "frontend/src")
    shutil.copytree(
        source / "frontend/.storybook",
        destination / "frontend/.storybook",
    )
    for name in (
        ".prettierignore",
        ".prettierrc.json",
        "index.html",
        "package.json",
        "tsconfig.json",
        "vite.config.ts",
    ):
        shutil.copy2(
            source / "frontend" / name,
            destination / "frontend" / name,
        )
    dependencies = Path("/opt/frontend/node_modules")
    if dependencies.exists():
        dependency_links = destination / "frontend/node_modules"
        dependency_links.mkdir()
        for dependency in dependencies.iterdir():
            (dependency_links / dependency.name).symlink_to(
                dependency.resolve(strict=True),
                target_is_directory=dependency.is_dir(),
            )


def validate_against_workspace(
    workspace: Path,
    files: list[dict[str, str]],
) -> None:
    root = workspace.resolve(strict=True)
    for item in files:
        relative = PurePosixPath(item["path"])
        candidate = workspace.joinpath(*relative.parts)
        current = workspace
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"candidate path contains a symlink: {relative}")
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError as error:
            raise ValueError(f"candidate path escapes workspace: {relative}") from error
        if candidate.exists():
            existing = candidate.read_text(encoding="utf-8")
            if item["content"] == existing or not item["content"].startswith(
                existing
            ):
                raise ValueError(
                    "existing tests may only receive append-only additions: "
                    f"{relative}"
                )


def run_backend(workspace: Path, timeout: int) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--cov=app",
        "--cov-report=term-missing",
        "--cov-report=json",
        "-q",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=workspace / "backend",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return {
            "command": " ".join(command),
            "exit_code": completed.returncode,
            "timed_out": False,
            "output": completed.stdout[-MAX_LOG_CHARS:],
        }
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {
            "command": " ".join(command),
            "exit_code": None,
            "timed_out": True,
            "output": output[-MAX_LOG_CHARS:],
        }


def run_frontend(workspace: Path, timeout: int) -> dict[str, Any]:
    commands = [
        ["npm", "run", "test:coverage", "--", "--reporter=dot"],
        ["npm", "run", "typecheck"],
        ["npm", "run", "format:check"],
    ]
    outputs: list[str] = []
    command_text = " && ".join(" ".join(command) for command in commands)
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=workspace / "frontend",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            outputs.append(output)
            return {
                "command": command_text,
                "exit_code": None,
                "timed_out": True,
                "output": "\n".join(outputs)[-MAX_LOG_CHARS:],
            }
        outputs.append(completed.stdout)
        if completed.returncode != 0:
            return {
                "command": command_text,
                "exit_code": completed.returncode,
                "timed_out": False,
                "output": "\n".join(outputs)[-MAX_LOG_CHARS:],
            }
    return {
        "command": command_text,
        "exit_code": 0,
        "timed_out": False,
        "output": "\n".join(outputs)[-MAX_LOG_CHARS:],
    }


def run_suite(suite: str, workspace: Path, timeout: int) -> dict[str, Any]:
    if suite == "backend":
        return run_backend(workspace, timeout)
    if suite == "frontend":
        return run_frontend(workspace, timeout)
    backend = run_backend(workspace, timeout)
    if backend["exit_code"] != 0:
        return {
            **backend,
            "command": f"{backend['command']} && frontend verification",
            "output": f"--- backend ---\n{backend['output']}",
        }
    frontend = run_frontend(workspace, timeout)
    return {
        "command": f"{backend['command']} && {frontend['command']}",
        "exit_code": frontend["exit_code"],
        "timed_out": frontend["timed_out"],
        "output": (
            f"--- backend ---\n{backend['output']}\n"
            f"--- frontend ---\n{frontend['output']}"
        )[-MAX_LOG_CHARS:],
    }


def make_patch(workspace: Path, files: list[dict[str, str]]) -> str:
    chunks: list[str] = []
    for item in files:
        path = item["path"]
        original_path = workspace / path
        original = (
            original_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if original_path.exists()
            else []
        )
        proposed = item["content"].splitlines(keepends=True)
        if proposed and not proposed[-1].endswith("\n"):
            proposed[-1] += "\n"
        chunks.extend(
            difflib.unified_diff(
                original,
                proposed,
                fromfile=f"a/{path}" if original else "/dev/null",
                tofile=f"b/{path}",
            )
        )
    return "".join(chunks)


def classify_candidate_failure(output: str) -> str:
    generated_error_markers = (
        "SyntaxError",
        "ImportError",
        "ModuleNotFoundError",
        "fixture ",
        "ERROR collecting",
    )
    if any(marker in output for marker in generated_error_markers):
        return "generated_test_failure"
    return "possible_application_bug_or_unclassified_failure"


def classify_baseline_failure(output: str) -> str:
    infrastructure_markers = (
        "Startup Error",
        "No module named pytest",
        "npm error",
        "command not found",
    )
    if any(marker in output for marker in infrastructure_markers):
        return "infrastructure_failure"
    return "baseline_failure"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a generated Taskly backend test candidate."
    )
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--suite", choices=["backend", "frontend", "mixed"])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_head = load_snapshot_head(args.workspace)
        if args.baseline_only:
            if args.candidate or not args.suite:
                raise ValueError(
                    "--baseline-only requires --suite and cannot use --candidate"
                )
            suite = args.suite
            files: list[dict[str, str]] = []
        else:
            if not args.candidate or args.suite:
                raise ValueError(
                    "candidate verification requires --candidate without --suite"
                )
            suite, files, expected_head = load_candidate(args.candidate)
            validate_snapshot_identity(args.workspace, expected_head)
        with tempfile.TemporaryDirectory(prefix="taskly-verifier-") as temporary:
            clean_workspace = Path(temporary) / "workspace"
            copy_workspace(args.workspace.resolve(strict=True), clean_workspace)
            if files:
                validate_against_workspace(clean_workspace, files)
            baseline = run_suite(suite, clean_workspace, args.timeout)
            result: dict[str, Any] = {
                "schema_version": 1,
                "suite": suite,
                "head_sha": snapshot_head,
                "baseline": baseline,
                "candidate": None,
            }
            if baseline["exit_code"] != 0:
                result["status"] = (
                    "infrastructure_failure"
                    if baseline["timed_out"]
                    else classify_baseline_failure(baseline["output"])
                )
            elif args.baseline_only:
                result["status"] = "verified_baseline"
            else:
                patch = make_patch(clean_workspace, files)
                (args.output_dir / "candidate.patch").write_text(
                    patch, encoding="utf-8"
                )
                for item in files:
                    destination = clean_workspace / item["path"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(item["content"], encoding="utf-8")
                candidate = run_suite(suite, clean_workspace, args.timeout)
                result["candidate"] = candidate
                if candidate["exit_code"] == 0:
                    result["status"] = "verified_tests"
                elif candidate["timed_out"]:
                    result["status"] = "infrastructure_failure"
                else:
                    result["status"] = classify_candidate_failure(
                        candidate["output"]
                    )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result = {
            "schema_version": 1,
            "status": "invalid_proposal",
            "reason": str(error),
        }

    (args.output_dir / "verification.json").write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    return (
        0
        if result["status"] in {"verified_tests", "verified_baseline"}
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
