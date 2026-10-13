# Taskly unit-test agent

This repository contains one custom OpenAI-powered agent that analyzes backend
and frontend changes, loads reusable testing skills, proposes focused tests, and
hands them to a separate deterministic verifier. It does not commit generated
code or automatically repair failures.

## Architecture

```text
explicit target or base/head SHAs
  -> deterministic scope and exact-head snapshot
  -> OpenAI Responses API tool loop
       read_file / load_skill / propose_tests
  -> validated JSON candidate
  -> secret-free Python 3.12 + Node 22 container
       baseline -> apply candidate -> candidate suite
  -> SHA-bound patch, verification artifact, job summary, and PR comment
```

`test-agent/run.py` owns the iterative model loop and limits. The model can only
read approved application/test files, load a known skill, and finish with a
structured proposal. It has no shell, filesystem write, GitHub write, or test
execution tool.

`test-agent/git_scope.py` computes the merge-base diff, enforces change limits,
and materializes an approved snapshot from an immutable commit. It excludes
environment files, Git metadata, assignment material, agent instructions, and
unrelated repository content.

`test-agent/verify.py` revalidates candidate paths, content policy, and head
identity. It runs baseline and candidate suites in disposable scratch space.
The verifier image contains trusted pinned dependencies but no application
snapshot; the exact head is mounted read-only at runtime.

`test-agent/workflow.py` validates bounded JSON artifacts and renders fixed,
escaped Markdown. The reporting job does not check out or execute application
code.

## Agent and reusable skills

- `test-agent/agent.md`: role, process, decision rules, and output policy.
- `test-agent/skills/analyze-changes/SKILL.md`: behavior and coverage-gap
  analysis.
- `test-agent/skills/backend-unit-tests/SKILL.md`: isolated FastAPI/pytest
  procedures.
- `test-agent/skills/frontend-unit-tests/SKILL.md`: Vitest, Testing Library,
  fetch mocking, and error-path procedures.

The runner exposes the skill catalogue through `load_skill`. Successful loads
are recorded from actual tool execution; the report never trusts a model's
unsupported claim that it used a skill.

## Local setup

Requirements:

- Python 3.12
- Node.js 22.12 or newer
- Docker or a compatible container runtime
- `OPENAI_API_KEY`

The runner loads the repository's ignored `.env` without overriding variables
already present in the process:

```dotenv
OPENAI_API_KEY=your-key
TEST_AGENT_MODEL=gpt-6-astra
```

Install the isolated agent dependencies:

```bash
uv venv test-agent/.venv --python 3.12
uv pip install --python test-agent/.venv/bin/python \
  --requirement test-agent/requirements.txt
```

Generate tests for one explicit target:

```bash
test-agent/.venv/bin/python test-agent/run.py \
  --target backend/app/core/config.py \
  --output /tmp/taskly-candidate.json
```

Analyze an immutable diff:

```bash
test-agent/.venv/bin/python test-agent/run.py \
  --base <base-commit-sha> \
  --head <head-commit-sha> \
  --output /tmp/taskly-candidate.json
```

The defaults allow 10 changed application files, 30,000 relevant diff
characters, 12 model requests, and 30 tool executions. Every batched function
call counts as a separate tool execution.

## Isolated local verification

Materialize the exact head without executing it:

```bash
test-agent/.venv/bin/python test-agent/git_scope.py snapshot \
  --repository . \
  --head <head-commit-sha> \
  --destination /tmp/taskly-head
```

Build trusted dependencies, then run with no network, credentials, writable
root filesystem, or sensitive mounts:

```bash
docker build \
  --file test-agent/Dockerfile.verify \
  --tag taskly-test-verifier .

mkdir -p /tmp/taskly-verification
chmod 0777 /tmp/taskly-verification

docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --memory 2g \
  --cpus 2 \
  --pids-limit 256 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -v /tmp/taskly-head:/workspace:ro \
  -v /tmp/taskly-candidate.json:/input/candidate.json:ro \
  -v /tmp/taskly-verification:/output:rw \
  taskly-test-verifier \
  --candidate /input/candidate.json \
  --output-dir /output
```

Containers reduce exposure but are not a perfect sandbox. Generated patches
still require human review.

## GitHub pull requests

`.github/workflows/unit-test-agent.yml` runs for opened, synchronized, and
reopened pull requests. Per-PR concurrency cancels stale runs.

The jobs have separate responsibilities:

| Job | Credentials | Behavior |
| --- | --- | --- |
| Scope | Read-only repository access | Computes deterministic eligibility and suite |
| Baseline | No API secrets | Runs exact-head tests in the isolated container |
| Generate | `OPENAI_API_KEY` only in the model step | Uses trusted-base agent code and treats the PR head as data |
| Verify | No API secrets | Revalidates and executes the candidate in a fresh container |
| Report | Narrow PR-comment permission | Validates artifacts, writes the summary, and updates one marked comment |

Paid generation is restricted to same-repository, non-bot pull requests from an
`OWNER`, `MEMBER`, or `COLLABORATOR`. Forks can receive ordinary baseline
results, but generation is explicitly skipped. Dependency, workflow, agent,
test-configuration, rename, deletion, and oversized changes require manual
review.

The workflow uses `pull_request`, not `pull_request_target`. GitHub evaluates a
`pull_request` workflow from the PR merge ref, so a contributor trusted for paid
generation must also be trusted to modify workflow definitions. Loading agent
code from the base revision does not independently protect the workflow YAML.
Repositories with different contributor trust levels should use
environment-protected secrets with required reviewers.

Set the GitHub Actions repository secret `OPENAI_API_KEY`. Optionally set the
repository variable `TEST_AGENT_MODEL`; it defaults to `gpt-6-astra`. Provider
spending controls remain necessary because request limits are not a precise
currency cap.

## Applying a reported patch

The report identifies the analyzed head SHA. Download
`test-agent-verification-<head-sha>` from the linked workflow run, then:

```bash
git checkout <reported-head-sha>
git apply --check candidate.patch
git apply candidate.patch
```

Rerun the affected suites. A patch is labeled verified only when the exact-head
baseline and candidate commands both exited successfully. Otherwise it is
diagnostic only.

## Measured local evidence

The first genuine local runs used `gpt-6-astra` and loaded the expected skills
through real tool calls.

| Area | Before | With generated tests | Target |
| --- | ---: | ---: | ---: |
| Backend tests | 21 | 37 | `app/core/config.py`: 0% -> 100% |
| Backend total coverage | 42% | 50% | unchanged application source |
| Frontend tests | 18 | 31 | `src/lib/http.ts`: 0% -> 100% |
| Frontend line coverage | 47.68% | 54.3% | unchanged application source |
| Frontend branch coverage | 43.79% | 51.09% | unchanged application source |

The generated tests are `backend/tests/test_config.py` and
`frontend/src/lib/http.test.ts`. The frontend snapshot also passes TypeScript
typechecking and the repository's Prettier check.

A separate exact-head diff demonstration used base `9addcc7d` and synthetic
head `f30e3a4`. The agent made 8 model requests and 9 tool calls, loaded
`analyze-changes` and `backend-unit-tests`, and produced a patch that passed a
green 21-test baseline and 25-test candidate run in the isolated verifier.

## Bootstrap and current limitation

The workflow must exist on the trusted base branch before a demonstration pull
request can use it. Bootstrap this implementation first, then create a branch
such as `feature/demo` with:

```text
Base repository: Http400/taskly-app
Base branch:     main
Head repository: Http400/taskly-app
```

When using GitHub CLI, pass `--repo Http400/taskly-app`. The real GitHub Actions
run link is pending that bootstrap and must be added here after the first
same-repository PR run.

The minimum version intentionally has no automatic repair, arbitrary fork
generation, production-code edits, auto-commits, multi-agent orchestration,
coverage gate, or dashboard.
