from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT))

from git_scope import GitScopeError, analyze_diff, materialize_snapshot


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()


def commit(repository: Path, message: str) -> str:
    git(repository, "add", "-A")
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test Agent")
    git(tmp_path, "config", "user.email", "agent@example.test")
    files = {
        "backend/app/config.py": "VALUE = 'base'\n",
        "backend/tests/test_config.py": "def test_value():\n    assert True\n",
        "backend/pyproject.toml": "[tool.pytest.ini_options]\n",
        "pytest.ini": "[pytest]\n",
        "frontend/src/lib/http.ts": "export const value = 'base'\n",
        "frontend/src/App.test.tsx": "test('app', () => {})\n",
        "frontend/package.json": "{}\n",
        "frontend/tsconfig.json": "{}\n",
        "frontend/vite.config.ts": "export default {}\n",
        "README.md": "# Test\n",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    commit(tmp_path, "base")
    return tmp_path


def test_analyzes_merge_base_to_exact_head(repository):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "backend/app/config.py").write_text("VALUE = 'head'\n")
    head = commit(repository, "change backend")

    scope = analyze_diff(repository, base, head)

    assert scope.status == "eligible"
    assert scope.targets == ("backend/app/config.py",)
    assert scope.base_sha == base
    assert scope.head_sha == head
    assert scope.merge_base_sha == base
    assert "VALUE = 'head'" in scope.diff
    assert scope.diff_characters == len(scope.diff)


def test_mixed_application_change_is_eligible(repository):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "backend/app/config.py").write_text("VALUE = 'head'\n")
    (repository / "frontend/src/lib/http.ts").write_text(
        "export const value = 'head'\n"
    )
    head = commit(repository, "change both")

    scope = analyze_diff(repository, base, head)

    assert scope.targets == (
        "backend/app/config.py",
        "frontend/src/lib/http.ts",
    )


def test_docs_only_change_is_normal_no_op(repository):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("# Updated\n")
    head = commit(repository, "docs")

    scope = analyze_diff(repository, base, head)

    assert scope.status == "no_tests_needed"
    assert scope.targets == ()


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_renames_and_deletions_require_manual_review(repository, operation):
    base = git(repository, "rev-parse", "HEAD")
    source = repository / "backend/app/config.py"
    if operation == "rename":
        source.rename(repository / "backend/app/settings.py")
    else:
        source.unlink()
    head = commit(repository, operation)

    scope = analyze_diff(repository, base, head)

    assert scope.status == "skipped_ineligible"
    assert "manual review" in scope.reason


def test_privileged_change_requires_manual_review(repository):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "frontend/package.json").write_text('{"private": true}\n')
    head = commit(repository, "dependency metadata")

    scope = analyze_diff(repository, base, head)

    assert scope.status == "skipped_ineligible"
    assert "test-configuration" in scope.reason


def test_limits_are_reported_without_truncation(repository):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "backend/app/config.py").write_text("VALUE = '" + "x" * 200 + "'\n")
    head = commit(repository, "large change")

    scope = analyze_diff(repository, base, head, max_diff_characters=50)

    assert scope.status == "skipped_ineligible"
    assert "was not truncated" in scope.reason
    assert scope.diff == ""
    assert scope.diff_characters > 50


def test_changed_application_file_limit_is_enforced(repository):
    base = git(repository, "rev-parse", "HEAD")
    second = repository / "backend/app/second.py"
    second.write_text("VALUE = 2\n")
    (repository / "backend/app/config.py").write_text("VALUE = 'head'\n")
    head = commit(repository, "two files")

    scope = analyze_diff(repository, base, head, max_files=1)

    assert scope.status == "skipped_ineligible"
    assert "exceed the limit of 1" in scope.reason


def test_materializes_committed_head_instead_of_dirty_worktree(repository, tmp_path):
    head = git(repository, "rev-parse", "HEAD")
    (repository / "backend/app/config.py").write_text("VALUE = 'dirty'\n")
    destination = tmp_path / "snapshot"

    materialize_snapshot(repository, head, destination)

    assert (destination / "backend/app/config.py").read_text() == "VALUE = 'base'\n"
    assert not (destination / "README.md").exists()
    manifest = json.loads(
        (destination / ".test-agent-snapshot.json").read_text()
    )
    assert manifest == {"schema_version": 1, "head_sha": head}


def test_rejects_non_sha_revisions(repository):
    with pytest.raises(GitScopeError, match="explicit hexadecimal"):
        analyze_diff(repository, "main", "HEAD")
