from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
APPROVED_SNAPSHOT_PATHS = (
    "backend/app",
    "backend/tests",
    "backend/pyproject.toml",
    "pytest.ini",
    "frontend/src",
    "frontend/.storybook",
    "frontend/.prettierignore",
    "frontend/.prettierrc.json",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
)
PRIVILEGED_EXACT_PATHS = {
    "backend/pyproject.toml",
    "backend/requirements.in",
    "backend/requirements.txt",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
    "pytest.ini",
}
PRIVILEGED_PREFIXES = (
    ".github/",
    "test-agent/",
)


class GitScopeError(Exception):
    pass


@dataclass(frozen=True)
class ChangedPath:
    status: str
    path: str
    previous_path: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {"status": self.status, "path": self.path}
        if self.previous_path:
            result["previous_path"] = self.previous_path
        return result


@dataclass(frozen=True)
class DiffScope:
    status: str
    reason: str
    base_sha: str
    head_sha: str
    merge_base_sha: str
    changed_paths: tuple[ChangedPath, ...]
    targets: tuple[str, ...]
    diff: str
    diff_characters: int

    def facts(self) -> dict[str, object]:
        return {
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "merge_base_sha": self.merge_base_sha,
            "changed_paths": [path.as_dict() for path in self.changed_paths],
            "targets": list(self.targets),
            "changed_application_file_count": len(self.targets),
            "diff_characters": self.diff_characters,
        }

    def result(self) -> dict[str, object]:
        ecosystems = {
            "backend" if target.startswith("backend/") else "frontend"
            for target in self.targets
        }
        suite = (
            next(iter(ecosystems))
            if len(ecosystems) == 1
            else "mixed" if ecosystems else "none"
        )
        return {
            "schema_version": 1,
            "status": self.status,
            "reason": self.reason,
            "suite": suite,
            **self.facts(),
        }


def run_git(repository: Path, *arguments: str, text: bool = True):
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode(
            "utf-8", errors="replace"
        )
        raise GitScopeError(stderr.strip() or f"git {arguments[0]} failed")
    return completed.stdout


def resolve_commit(repository: Path, revision: str) -> str:
    if not SHA_PATTERN.fullmatch(revision):
        raise GitScopeError("base and head must be explicit hexadecimal commit SHAs")
    return run_git(
        repository,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
    ).strip()


def parse_name_status(raw: bytes) -> tuple[ChangedPath, ...]:
    fields = raw.decode("utf-8", errors="surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    changed: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(fields):
                raise GitScopeError("malformed rename/copy record from git diff")
            previous_path = fields[index]
            path = fields[index + 1]
            index += 2
            changed.append(ChangedPath(status, path, previous_path))
        else:
            if index >= len(fields):
                raise GitScopeError("malformed path record from git diff")
            changed.append(ChangedPath(status, fields[index]))
            index += 1
    return tuple(changed)


def is_application_path(path: str) -> bool:
    pure = PurePosixPath(path)
    if path.startswith("backend/app/"):
        return pure.suffix == ".py"
    if path.startswith("frontend/src/"):
        return (
            pure.suffix in {".ts", ".tsx"}
            and not pure.name.endswith((".test.ts", ".test.tsx"))
        )
    return False


def is_privileged_path(path: str) -> bool:
    if path in PRIVILEGED_EXACT_PATHS:
        return True
    if path.startswith(PRIVILEGED_PREFIXES):
        return True
    name = PurePosixPath(path).name
    return name.startswith("Dockerfile") or name.endswith((".lock", ".toml"))


def analyze_diff(
    repository: Path,
    base_revision: str,
    head_revision: str,
    *,
    max_files: int = 10,
    max_diff_characters: int = 30_000,
) -> DiffScope:
    repository = repository.resolve(strict=True)
    base_sha = resolve_commit(repository, base_revision)
    head_sha = resolve_commit(repository, head_revision)
    merge_base = run_git(repository, "merge-base", base_sha, head_sha).strip()
    raw_status = run_git(
        repository,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        merge_base,
        head_sha,
        "--",
        text=False,
    )
    changed = parse_name_status(raw_status)

    unsupported = [
        item
        for item in changed
        if item.status.startswith(("R", "D"))
    ]
    if unsupported:
        names = ", ".join(item.path for item in unsupported)
        return DiffScope(
            "skipped_ineligible",
            f"renames and deletions require manual review: {names}",
            base_sha,
            head_sha,
            merge_base,
            changed,
            (),
            "",
            0,
        )

    privileged = [
        item.path
        for item in changed
        if is_privileged_path(item.path)
        or (
            item.previous_path is not None
            and is_privileged_path(item.previous_path)
        )
    ]
    if privileged:
        return DiffScope(
            "skipped_ineligible",
            "workflow, agent, dependency, or test-configuration changes "
            f"require manual review: {', '.join(privileged)}",
            base_sha,
            head_sha,
            merge_base,
            changed,
            (),
            "",
            0,
        )

    targets = tuple(
        sorted(
            item.path
            for item in changed
            if item.status[:1] in {"A", "M"} and is_application_path(item.path)
        )
    )
    if not targets:
        return DiffScope(
            "no_tests_needed",
            "No supported backend or frontend application files changed.",
            base_sha,
            head_sha,
            merge_base,
            changed,
            (),
            "",
            0,
        )
    if len(targets) > max_files:
        return DiffScope(
            "skipped_ineligible",
            f"{len(targets)} changed application files exceed the limit of "
            f"{max_files}.",
            base_sha,
            head_sha,
            merge_base,
            changed,
            targets,
            "",
            0,
        )

    diff = run_git(
        repository,
        "diff",
        "--no-ext-diff",
        "--unified=80",
        merge_base,
        head_sha,
        "--",
        *targets,
    )
    diff_characters = len(diff)
    if diff_characters > max_diff_characters:
        return DiffScope(
            "skipped_ineligible",
            f"Relevant diff has {diff_characters} characters, exceeding the "
            f"limit of {max_diff_characters}; it was not truncated.",
            base_sha,
            head_sha,
            merge_base,
            changed,
            targets,
            "",
            diff_characters,
        )
    return DiffScope(
        "eligible",
        "Supported application changes are within configured limits.",
        base_sha,
        head_sha,
        merge_base,
        changed,
        targets,
        diff,
        diff_characters,
    )


def materialize_snapshot(repository: Path, head_sha: str, destination: Path) -> None:
    existing_paths: list[str] = []
    for path in APPROVED_SNAPSHOT_PATHS:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{head_sha}:{path}"],
            cwd=repository,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            existing_paths.append(path)
    if not existing_paths:
        raise GitScopeError("the head contains no approved application paths")

    archive = run_git(
        repository,
        "archive",
        "--format=tar",
        head_sha,
        "--",
        *existing_paths,
        text=False,
    )
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise GitScopeError("git archive produced an unsafe path")
            source = tar.extractfile(member)
            if source is None:
                raise GitScopeError(f"could not read archived file: {member.name}")
            output = destination.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(source.read())
    (destination / ".test-agent-snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "head_sha": head_sha,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a commit diff or materialize an approved snapshot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze")
    analyze.add_argument("--repository", type=Path, default=Path.cwd())
    analyze.add_argument("--base", required=True)
    analyze.add_argument("--head", required=True)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--max-files", type=int, default=10)
    analyze.add_argument("--max-diff-characters", type=int, default=30_000)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--repository", type=Path, default=Path.cwd())
    snapshot.add_argument("--head", required=True)
    snapshot.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "analyze":
            scope = analyze_diff(
                args.repository,
                args.base,
                args.head,
                max_files=args.max_files,
                max_diff_characters=args.max_diff_characters,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(scope.result(), indent=2) + "\n",
                encoding="utf-8",
            )
            return 0
        head_sha = resolve_commit(args.repository, args.head)
        materialize_snapshot(args.repository, head_sha, args.destination)
    except (GitScopeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(head_sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
