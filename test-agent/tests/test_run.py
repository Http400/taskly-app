from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT))

import run as agent_run


def function_call(name: str, arguments: object, call_id: str = "call-1"):
    serialized = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(
        type="function_call",
        name=name,
        arguments=serialized,
        call_id=call_id,
    )


def response(*items):
    return SimpleNamespace(output=list(items))


class FakeResponses:
    def __init__(self, queued):
        self.queued = list(queued)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if not self.queued:
            raise AssertionError("unexpected model request")
        return self.queued.pop(0)


class FakeClient:
    def __init__(self, queued):
        self.responses = FakeResponses(queued)


@pytest.fixture
def workspace(tmp_path):
    target = tmp_path / "backend/app/core/config.py"
    target.parent.mkdir(parents=True)
    target.write_text("def resolve_database_url():\n    return 'database'\n")
    tests = tmp_path / "backend/tests/core/test_existing.py"
    tests.parent.mkdir(parents=True)
    tests.write_text("def test_existing():\n    assert True\n")
    (tmp_path / "backend/pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    return tmp_path


def config(workspace: Path, **overrides):
    values = {
        "workspace": workspace,
        "target": "backend/app/core/config.py",
        "agent_root": AGENT_ROOT,
    }
    values.update(overrides)
    return agent_run.RunnerConfig(**values)


def valid_proposal():
    return {
        "decision": "tests_needed",
        "reason": "The database URL branches have no focused coverage.",
        "files": [
            {
                "path": "backend/tests/core/test_config.py",
                "content": (
                    "from app.core.config import resolve_database_url\n\n"
                    "def test_explicit_url():\n"
                    "    assert resolve_database_url('sqlite://') == 'sqlite://'\n"
                ),
            }
        ],
        "intended_cases": ["An explicit URL is returned unchanged."],
    }


def test_iterative_loop_returns_validated_candidate_and_records_skills(workspace):
    client = FakeClient(
        [
            response(
                function_call(
                    "load_skill", {"name": "analyze-changes"}, "skill-1"
                ),
                function_call(
                    "load_skill", {"name": "backend-unit-tests"}, "skill-2"
                ),
            ),
            response(
                function_call(
                    "read_file",
                    {"path": "backend/app/core/config.py"},
                    "read-1",
                )
            ),
            response(function_call("propose_tests", valid_proposal(), "proposal-1")),
        ]
    )

    result = agent_run.AgentRunner(config(workspace), client).run()

    assert result["status"] == "candidate_proposed"
    assert result["skills_loaded"] == ["analyze-changes", "backend-unit-tests"]
    assert result["request_count"] == 3
    assert result["tool_execution_count"] == 4
    assert [entry["tool"] for entry in result["trace"]] == [
        "load_skill",
        "load_skill",
        "read_file",
        "propose_tests",
    ]
    second_request_input = client.responses.requests[1]["input"]
    outputs = [
        item for item in second_request_input if isinstance(item, dict)
        and item.get("type") == "function_call_output"
    ]
    assert {item["call_id"] for item in outputs} == {"skill-1", "skill-2"}


def test_evidence_based_no_op_is_terminal(workspace):
    no_op = {
        "decision": "no_tests_needed",
        "reason": "The existing test covers the only observable branch.",
        "files": [],
        "intended_cases": [],
    }
    client = FakeClient(
        [
            response(
                function_call(
                    "load_skill", {"name": "analyze-changes"}, "skill-1"
                )
            ),
            response(function_call("propose_tests", no_op, "proposal-1")),
        ]
    )

    result = agent_run.AgentRunner(config(workspace), client).run()

    assert result["status"] == "no_tests_needed"
    assert result["proposal"]["files"] == []


def test_missing_approved_file_is_returned_as_recoverable_tool_error(workspace):
    client = FakeClient(
        [
            response(
                function_call(
                    "read_file",
                    {"path": "backend/tests/core/test_missing.py"},
                    "read-missing",
                )
            ),
            response(
                function_call(
                    "load_skill", {"name": "analyze-changes"}, "skill-1"
                ),
                function_call(
                    "load_skill", {"name": "backend-unit-tests"}, "skill-2"
                ),
            ),
            response(function_call("propose_tests", valid_proposal(), "proposal-1")),
        ]
    )

    result = agent_run.AgentRunner(config(workspace), client).run()

    assert result["status"] == "candidate_proposed"
    second_request_input = client.responses.requests[1]["input"]
    tool_error = next(
        item
        for item in second_request_input
        if isinstance(item, dict) and item.get("call_id") == "read-missing"
    )
    assert json.loads(tool_error["output"]) == {
        "ok": False,
        "error": "file does not exist: backend/tests/core/test_missing.py",
    }
    assert result["trace"][0]["outcome"] == "not found"


@pytest.mark.parametrize(
    "path",
    [
        "../../some-file",
        "/tmp/test_config.py",
        "backend/app/test_config.py",
        "backend/tests/core/config.py",
    ],
)
def test_rejects_unsafe_or_non_test_proposal_paths(workspace, path):
    proposal = valid_proposal()
    proposal["files"][0]["path"] = path

    with pytest.raises(agent_run.PolicyError):
        agent_run.validate_proposal(
            workspace,
            proposal,
            {"analyze-changes", "backend-unit-tests"},
        )


def test_rejects_symlink_that_escapes_workspace(workspace, tmp_path):
    outside = tmp_path.parent / "outside-tests"
    outside.mkdir(exist_ok=True)
    link = workspace / "backend/tests/linked"
    link.symlink_to(outside, target_is_directory=True)
    proposal = valid_proposal()
    proposal["files"][0]["path"] = "backend/tests/linked/test_config.py"

    with pytest.raises(agent_run.PolicyError, match="symlinks"):
        agent_run.validate_proposal(
            workspace,
            proposal,
            {"analyze-changes", "backend-unit-tests"},
        )


@pytest.mark.parametrize(
    "marker",
    ["pytest.mark.skip", "pytest.mark.xfail", "pytest.skip(", "@unittest.skip"],
)
def test_rejects_disabled_tests(workspace, marker):
    proposal = valid_proposal()
    proposal["files"][0]["content"] += f"\n# {marker}\n"

    with pytest.raises(agent_run.PolicyError, match="disabled tests"):
        agent_run.validate_proposal(
            workspace,
            proposal,
            {"analyze-changes", "backend-unit-tests"},
        )


def test_rejects_proposal_before_required_skills_are_loaded(workspace):
    with pytest.raises(agent_run.PolicyError, match="required skills"):
        agent_run.validate_proposal(workspace, valid_proposal(), {"analyze-changes"})


def test_existing_tests_can_only_receive_append_only_additions(workspace):
    proposal = valid_proposal()
    proposal["files"][0] = {
        "path": "backend/tests/core/test_existing.py",
        "content": "def test_replacement():\n    assert False\n",
    }

    with pytest.raises(agent_run.PolicyError, match="append-only"):
        agent_run.validate_proposal(
            workspace,
            proposal,
            {"analyze-changes", "backend-unit-tests"},
        )


def test_accepts_frontend_test_with_frontend_skill(workspace):
    target = workspace / "frontend/src/lib/http.ts"
    target.parent.mkdir(parents=True)
    target.write_text("export const request = () => fetch('/api')\n")
    proposal = {
        "decision": "tests_needed",
        "reason": "HTTP success behavior has no focused coverage.",
        "files": [
            {
                "path": "frontend/src/lib/http.test.ts",
                "content": (
                    "import { expect, test } from 'vitest'\n\n"
                    "test('requests the API', () => {\n"
                    "  expect(true).toBe(true)\n"
                    "})\n"
                ),
            }
        ],
        "intended_cases": ["Successful JSON responses are returned."],
    }

    result = agent_run.validate_proposal(
        workspace,
        proposal,
        {"analyze-changes", "frontend-unit-tests"},
        "frontend",
    )

    assert result["files"][0]["path"] == "frontend/src/lib/http.test.ts"


def test_mixed_run_requires_both_testing_skills(workspace):
    frontend_target = workspace / "frontend/src/lib/http.ts"
    frontend_target.parent.mkdir(parents=True)
    frontend_target.write_text("export const request = () => fetch('/api')\n")
    proposal = valid_proposal()
    proposal["files"].append(
        {
            "path": "frontend/src/lib/http.test.ts",
            "content": (
                "import { expect, test } from 'vitest'\n\n"
                "test('request', () => {\n"
                "  expect(true).toBe(true)\n"
                "})\n"
            ),
        }
    )
    proposal["intended_cases"].append("Frontend requests return JSON.")
    client = FakeClient(
        [
            response(
                function_call(
                    "load_skill", {"name": "analyze-changes"}, "skill-1"
                ),
                function_call(
                    "load_skill", {"name": "backend-unit-tests"}, "skill-2"
                ),
                function_call(
                    "load_skill", {"name": "frontend-unit-tests"}, "skill-3"
                ),
            ),
            response(function_call("propose_tests", proposal, "proposal-1")),
        ]
    )
    runner_config = config(
        workspace,
        targets=(
            "backend/app/core/config.py",
            "frontend/src/lib/http.ts",
        ),
    )

    result = agent_run.AgentRunner(runner_config, client).run()

    assert result["ecosystem"] == "mixed"
    assert result["target"] is None
    assert result["targets"] == [
        "backend/app/core/config.py",
        "frontend/src/lib/http.ts",
    ]
    assert result["skills_loaded"] == [
        "analyze-changes",
        "backend-unit-tests",
        "frontend-unit-tests",
    ]


def test_rejects_malformed_tool_arguments(workspace):
    client = FakeClient(
        [response(function_call("read_file", "{not-json", "read-1"))]
    )

    with pytest.raises(agent_run.PolicyError, match="malformed arguments"):
        agent_run.AgentRunner(config(workspace), client).run()


def test_rejects_unknown_tools(workspace):
    client = FakeClient(
        [response(function_call("run_shell", {"command": "pytest"}, "shell-1"))]
    )

    with pytest.raises(agent_run.PolicyError, match="unknown tool"):
        agent_run.AgentRunner(config(workspace), client).run()


def test_counts_batched_calls_against_tool_limit(workspace):
    client = FakeClient(
        [
            response(
                function_call(
                    "load_skill", {"name": "analyze-changes"}, "skill-1"
                ),
                function_call(
                    "load_skill", {"name": "backend-unit-tests"}, "skill-2"
                ),
            )
        ]
    )

    with pytest.raises(agent_run.LimitError, match="tool execution"):
        agent_run.AgentRunner(config(workspace, max_tools=1), client).run()


def test_enforces_model_request_limit_independently(workspace):
    repeated_read = response(
        function_call(
            "read_file",
            {"path": "backend/app/core/config.py"},
            "read-1",
        )
    )
    client = FakeClient([repeated_read, repeated_read])

    with pytest.raises(agent_run.LimitError, match="model request"):
        agent_run.AgentRunner(config(workspace, max_requests=2), client).run()
