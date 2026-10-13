from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from openai import OpenAI
from dotenv import load_dotenv

from git_scope import GitScopeError, analyze_diff, materialize_snapshot


DEFAULT_MODEL = "gpt-6-astra"
MAX_FILE_CHARS = 12_000
MAX_TOTAL_READ_CHARS = 60_000
MAX_PROPOSAL_CHARS = 100_000
REQUIRED_SKILLS = {
    "backend": {"analyze-changes", "backend-unit-tests"},
    "frontend": {"analyze-changes", "frontend-unit-tests"},
}
DENIED_NAMES = {
    ".git",
    ".env",
    "assignment.md",
    "PLAN.md",
    "review_unit_test_agent_plan.md",
    "unit_test_agent_plan_second_review.md",
}
FORBIDDEN_TEST_MARKERS = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "pytest.skip(",
    "@unittest.skip",
    "unittest.skip(",
    ".skip(",
    ".only(",
)


class AgentError(Exception):
    status = "invalid_proposal"


class PolicyError(AgentError):
    pass


class LimitError(AgentError):
    status = "limit_exhausted"


class ModelError(AgentError):
    status = "model_infrastructure_failure"


class ToolExecutionError(Exception):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    workspace: Path
    target: str
    agent_root: Path
    targets: tuple[str, ...] = ()
    change_summary: str = ""
    model: str = DEFAULT_MODEL
    max_requests: int = 12
    max_tools: int = 30


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read one approved repository source or test file.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative POSIX path.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "load_skill",
        "description": "Load one reusable testing procedure from the catalogue.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [
                        "analyze-changes",
                        "backend-unit-tests",
                        "frontend-unit-tests",
                    ],
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "propose_tests",
        "description": "Finish with a test proposal or an evidence-based no-op.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["tests_needed", "no_tests_needed"],
                },
                "reason": {"type": "string"},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                },
                "intended_cases": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["decision", "reason", "files", "intended_cases"],
            "additionalProperties": False,
        },
    },
]


def normalize_path(raw_path: Any) -> PurePosixPath:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise PolicyError("path must be a non-empty string")
    if "\\" in raw_path:
        raise PolicyError("paths must use POSIX separators")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PolicyError(f"unsafe path: {raw_path}")
    if any(part in DENIED_NAMES or part.startswith(".env") for part in path.parts):
        raise PolicyError(f"denied path: {raw_path}")
    return path


def ensure_inside_workspace(workspace: Path, relative: PurePosixPath) -> Path:
    root = workspace.resolve(strict=True)
    candidate = workspace.joinpath(*relative.parts)
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PolicyError(f"symlinks are not allowed: {relative}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise PolicyError(f"path escapes workspace: {relative}") from error
    return candidate


def target_ecosystem(path: PurePosixPath) -> str:
    value = path.as_posix()
    if value.startswith("backend/app/") and value.endswith(".py"):
        return "backend"
    if value.startswith("frontend/src/") and path.suffix in {".ts", ".tsx"}:
        return "frontend"
    raise PolicyError("target must be backend Python or frontend TypeScript source")


def is_allowed_read(path: PurePosixPath, ecosystem: str) -> bool:
    value = path.as_posix()
    if ecosystem == "mixed":
        return is_allowed_read(path, "backend") or is_allowed_read(
            path, "frontend"
        )
    if ecosystem == "backend":
        return (
            value == "backend/pyproject.toml"
            or value == "pytest.ini"
            or (
                value.startswith("backend/app/")
                and value.endswith(".py")
            )
            or (
                value.startswith("backend/tests/")
                and value.endswith(".py")
            )
        )
    if ecosystem == "frontend":
        return (
            value
            in {
                "frontend/package.json",
                "frontend/tsconfig.json",
                "frontend/vite.config.ts",
            }
            or (
                value.startswith("frontend/src/")
                and path.suffix in {".ts", ".tsx"}
            )
        )
    return False


def validate_test_path(
    workspace: Path,
    raw_path: Any,
    ecosystem: str = "backend",
) -> str:
    path = normalize_path(raw_path)
    value = path.as_posix()
    if ecosystem == "backend":
        allowed = (
            value.startswith("backend/tests/")
            and path.suffix == ".py"
            and path.name.startswith("test_")
        )
    else:
        frontend_allowed = value.startswith("frontend/src/") and (
            path.name.endswith(".test.ts") or path.name.endswith(".test.tsx")
        )
        if ecosystem == "mixed":
            backend_allowed = (
                value.startswith("backend/tests/")
                and path.suffix == ".py"
                and path.name.startswith("test_")
            )
            allowed = backend_allowed or frontend_allowed
        else:
            allowed = frontend_allowed
    if not allowed:
        raise PolicyError(
            f"proposed path is not an approved {ecosystem} test: {value}"
        )
    ensure_inside_workspace(workspace, path)
    return value


def validate_proposal(
    workspace: Path,
    arguments: dict[str, Any],
    loaded_skills: set[str],
    ecosystem: str = "backend",
) -> dict[str, Any]:
    expected = {"decision", "reason", "files", "intended_cases"}
    if set(arguments) != expected:
        raise PolicyError("propose_tests arguments do not match the contract")

    decision = arguments["decision"]
    reason = arguments["reason"]
    files = arguments["files"]
    cases = arguments["intended_cases"]
    if decision not in {"tests_needed", "no_tests_needed"}:
        raise PolicyError("invalid proposal decision")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2_000:
        raise PolicyError("proposal reason must be between 1 and 2,000 characters")
    if not isinstance(files, list) or not isinstance(cases, list):
        raise PolicyError("proposal files and intended_cases must be arrays")
    if not all(isinstance(case, str) and case.strip() for case in cases):
        raise PolicyError("intended cases must be non-empty strings")

    if decision == "no_tests_needed":
        if files or cases:
            raise PolicyError("no_tests_needed cannot include files or intended cases")
        if "analyze-changes" not in loaded_skills:
            raise PolicyError("analyze-changes must be loaded before a no-op decision")
        return {
            "decision": decision,
            "reason": reason.strip(),
            "files": [],
            "intended_cases": [],
        }

    required_skills = (
        REQUIRED_SKILLS["backend"] | REQUIRED_SKILLS["frontend"]
        if ecosystem == "mixed"
        else REQUIRED_SKILLS[ecosystem]
    )
    missing_skills = required_skills - loaded_skills
    if missing_skills:
        raise PolicyError(
            "required skills were not loaded: " + ", ".join(sorted(missing_skills))
        )
    if not 1 <= len(files) <= 5 or not cases:
        raise PolicyError("tests_needed requires 1-5 files and intended cases")

    validated_files: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    total_chars = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise PolicyError("each proposed file requires only path and content")
        path = validate_test_path(workspace, item["path"], ecosystem)
        content = item["content"]
        if path in seen_paths:
            raise PolicyError(f"duplicate proposed path: {path}")
        if not isinstance(content, str) or not content.strip():
            raise PolicyError(f"empty proposed test: {path}")
        if any(marker in content for marker in FORBIDDEN_TEST_MARKERS):
            raise PolicyError(f"disabled tests are forbidden: {path}")
        existing_path = workspace / path
        if existing_path.exists():
            existing = existing_path.read_text(encoding="utf-8")
            if content == existing or not content.startswith(existing):
                raise PolicyError(
                    f"existing tests may only receive append-only additions: {path}"
                )
        total_chars += len(content)
        if total_chars > MAX_PROPOSAL_CHARS:
            raise PolicyError("proposed test content exceeds the size limit")
        seen_paths.add(path)
        validated_files.append({"path": path, "content": content})

    return {
        "decision": decision,
        "reason": reason.strip(),
        "files": validated_files,
        "intended_cases": cases,
    }


class AgentRunner:
    def __init__(self, config: RunnerConfig, client: Any) -> None:
        self.config = config
        self.client = client
        self.trace: list[dict[str, str]] = []
        self.loaded_skills: set[str] = set()
        self.requests = 0
        self.tool_executions = 0
        self.total_read_chars = 0

    def _record(self, tool: str, subject: str, outcome: str) -> None:
        self.trace.append({"tool": tool, "subject": subject, "outcome": outcome})

    def _read_file(self, raw_path: Any, ecosystem: str) -> str:
        path = normalize_path(raw_path)
        if not is_allowed_read(path, ecosystem):
            raise PolicyError(f"file is outside the approved read scope: {path}")
        candidate = ensure_inside_workspace(self.config.workspace, path)
        if not candidate.is_file():
            self._record("read_file", path.as_posix(), "not found")
            raise ToolExecutionError(f"file does not exist: {path}")
        content = candidate.read_text(encoding="utf-8")
        if len(content) > MAX_FILE_CHARS:
            raise PolicyError(f"file exceeds the per-file read limit: {path}")
        if self.total_read_chars + len(content) > MAX_TOTAL_READ_CHARS:
            raise LimitError("cumulative file read limit exhausted")
        self.total_read_chars += len(content)
        self._record("read_file", path.as_posix(), f"read {len(content)} characters")
        return content

    def _load_skill(self, raw_name: Any) -> str:
        if not isinstance(raw_name, str) or raw_name not in {
            "analyze-changes",
            "backend-unit-tests",
            "frontend-unit-tests",
        }:
            raise PolicyError(f"unknown skill: {raw_name}")
        skill_path = (
            self.config.agent_root / "skills" / raw_name / "SKILL.md"
        )
        content = skill_path.read_text(encoding="utf-8")
        self.loaded_skills.add(raw_name)
        self._record("load_skill", raw_name, "loaded")
        return content

    def _parse_arguments(self, call: Any) -> dict[str, Any]:
        try:
            arguments = json.loads(call.arguments)
        except (TypeError, json.JSONDecodeError) as error:
            raise PolicyError(f"malformed arguments for {call.name}") from error
        if not isinstance(arguments, dict):
            raise PolicyError(f"arguments for {call.name} must be an object")
        return arguments

    def _tool_output(self, call_id: str, value: Any) -> dict[str, str]:
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"ok": True, "result": value}),
        }

    def _tool_error(self, call_id: str, error: ToolExecutionError) -> dict[str, str]:
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"ok": False, "error": str(error)}),
        }

    def run(self) -> dict[str, Any]:
        raw_targets = self.config.targets or (self.config.target,)
        if not raw_targets or any(not target for target in raw_targets):
            raise PolicyError("at least one application target is required")
        targets = tuple(normalize_path(target) for target in raw_targets)
        ecosystems = {target_ecosystem(target) for target in targets}
        ecosystem = next(iter(ecosystems)) if len(ecosystems) == 1 else "mixed"
        for target in targets:
            if not is_allowed_read(target, ecosystem):
                raise PolicyError(
                    "an initial target is outside the approved read scope"
                )
            target_path = ensure_inside_workspace(self.config.workspace, target)
            if not target_path.is_file():
                raise PolicyError(f"target does not exist at the analyzed head: {target}")

        instructions = (self.config.agent_root / "agent.md").read_text(
            encoding="utf-8"
        )
        target_list = ", ".join(target.as_posix() for target in targets)
        change_context = (
            f"\n\nDeterministic merge-base diff:\n{self.config.change_summary}"
            if self.config.change_summary
            else ""
        )
        input_items: list[Any] = [
            {
                "role": "user",
                "content": (
                    f"Analyze these explicit {ecosystem} application targets and "
                    f"propose focused unit tests when needed: {target_list}. "
                    "Use repository-relative paths and the required skills."
                    f"{change_context}"
                ),
            }
        ]

        while True:
            if self.requests >= self.config.max_requests:
                raise LimitError("model request limit exhausted")
            self.requests += 1
            try:
                response = self.client.responses.create(
                    model=self.config.model,
                    instructions=instructions,
                    tools=TOOLS,
                    input=input_items,
                )
            except Exception as error:
                raise ModelError(f"OpenAI request failed: {type(error).__name__}") from error

            output = list(response.output)
            input_items.extend(output)
            calls = [item for item in output if item.type == "function_call"]
            if not calls:
                raise PolicyError("model response did not call an approved tool")
            if any(call.name == "propose_tests" for call in calls) and len(calls) != 1:
                raise PolicyError("propose_tests must be the only call in its response")
            if self.tool_executions + len(calls) > self.config.max_tools:
                raise LimitError("tool execution limit exhausted")

            for call in calls:
                self.tool_executions += 1
                arguments = self._parse_arguments(call)
                if call.name == "read_file":
                    if set(arguments) != {"path"}:
                        raise PolicyError("read_file requires only path")
                    try:
                        value = self._read_file(arguments["path"], ecosystem)
                    except ToolExecutionError as error:
                        input_items.append(self._tool_error(call.call_id, error))
                    else:
                        input_items.append(self._tool_output(call.call_id, value))
                elif call.name == "load_skill":
                    if set(arguments) != {"name"}:
                        raise PolicyError("load_skill requires only name")
                    value = self._load_skill(arguments["name"])
                    input_items.append(self._tool_output(call.call_id, value))
                elif call.name == "propose_tests":
                    proposal = validate_proposal(
                        self.config.workspace,
                        arguments,
                        self.loaded_skills,
                        ecosystem,
                    )
                    self._record(
                        "propose_tests",
                        proposal["decision"],
                        f"validated {len(proposal['files'])} file(s)",
                    )
                    return {
                        "schema_version": 1,
                        "status": (
                            "candidate_proposed"
                            if proposal["decision"] == "tests_needed"
                            else "no_tests_needed"
                        ),
                        "target": (
                            targets[0].as_posix() if len(targets) == 1 else None
                        ),
                        "targets": [target.as_posix() for target in targets],
                        "ecosystem": ecosystem,
                        "model": self.config.model,
                        "request_count": self.requests,
                        "tool_execution_count": self.tool_executions,
                        "skills_loaded": sorted(self.loaded_skills),
                        "trace": self.trace,
                        "proposal": proposal,
                    }
                else:
                    raise PolicyError(f"unknown tool: {call.name}")


def write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate focused Taskly tests.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--target")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--model",
        default=None,
    )
    parser.add_argument("--max-requests", type=int, default=12)
    parser.add_argument("--max-tools", type=int, default=30)
    parser.add_argument("--max-files", type=int, default=10)
    parser.add_argument("--max-diff-characters", type=int, default=30_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(args.workspace / ".env", override=False)
    model = args.model or os.getenv("TEST_AGENT_MODEL", DEFAULT_MODEL)
    diff_mode = args.base is not None or args.head is not None
    if bool(args.base) != bool(args.head) or (diff_mode and args.target) or (
        not diff_mode and not args.target
    ):
        write_result(
            args.output,
            {
                "schema_version": 1,
                "status": "invalid_proposal",
                "reason": "use either --target or both --base and --head",
            },
        )
        return 1

    scope = None
    generation_workspace = args.workspace
    temporary: tempfile.TemporaryDirectory[str] | None = None
    targets: tuple[str, ...] = ()
    change_summary = ""
    if diff_mode:
        try:
            scope = analyze_diff(
                args.workspace,
                args.base,
                args.head,
                max_files=args.max_files,
                max_diff_characters=args.max_diff_characters,
            )
        except (GitScopeError, OSError) as error:
            write_result(
                args.output,
                {
                    "schema_version": 1,
                    "status": "skipped_ineligible",
                    "reason": str(error),
                },
            )
            return 1
        if scope.status != "eligible":
            write_result(
                args.output,
                {
                    "schema_version": 1,
                    "status": scope.status,
                    "reason": scope.reason,
                    **scope.facts(),
                },
            )
            return 0 if scope.status == "no_tests_needed" else 1
        temporary = tempfile.TemporaryDirectory(prefix="taskly-head-snapshot-")
        generation_workspace = Path(temporary.name) / "workspace"
        try:
            materialize_snapshot(
                args.workspace,
                scope.head_sha,
                generation_workspace,
            )
        except (GitScopeError, OSError) as error:
            temporary.cleanup()
            write_result(
                args.output,
                {
                    "schema_version": 1,
                    "status": "skipped_ineligible",
                    "reason": f"could not materialize exact head: {error}",
                    **scope.facts(),
                },
            )
            return 1
        targets = scope.targets
        change_summary = scope.diff
    else:
        targets = (args.target,)

    if not os.getenv("OPENAI_API_KEY"):
        result = {
            "schema_version": 1,
            "status": "model_infrastructure_failure",
            "reason": "OPENAI_API_KEY is not set",
            "targets": list(targets),
            "model": model,
        }
        if scope:
            result.update(scope.facts())
        write_result(args.output, result)
        if temporary:
            temporary.cleanup()
        return 2

    config = RunnerConfig(
        workspace=generation_workspace,
        target=targets[0],
        targets=targets,
        change_summary=change_summary,
        agent_root=Path(__file__).resolve().parent,
        model=model,
        max_requests=args.max_requests,
        max_tools=args.max_tools,
    )
    runner = AgentRunner(config, OpenAI())
    try:
        result = runner.run()
    except AgentError as error:
        result = {
            "schema_version": 1,
            "status": error.status,
            "reason": str(error),
            "targets": list(targets),
            "model": model,
            "request_count": runner.requests,
            "tool_execution_count": runner.tool_executions,
            "skills_loaded": sorted(runner.loaded_skills),
            "trace": runner.trace,
        }
        exit_code = 1
    else:
        exit_code = 0
    if scope:
        result.update(scope.facts())
    write_result(args.output, result)
    if temporary:
        temporary.cleanup()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
