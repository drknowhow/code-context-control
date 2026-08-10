# AgentCI
## Local CI Execution Platform for AI Coding Agents

**Document type:** Product + Architecture Specification  
**Status:** Draft for agent review  
**Primary goal:** Allow AI coding agents to execute the repository's required CI validation outside GitHub Actions while preserving GitHub Actions workflow files as the source of truth.

---

## 1. Executive Summary

AgentCI is a local-first CI execution and orchestration platform designed specifically for AI coding agents.

The system allows agents such as Codex, Claude, Gemini, or other autonomous coding tools to:

1. inspect the CI requirements of a repository,
2. determine which CI jobs are relevant to a code change,
3. execute those jobs locally or on user-controlled infrastructure,
4. receive structured failures instead of raw terminal output,
5. fix code and rerun only failed or affected validation,
6. perform a final full CI verification before push or merge,
7. optionally publish external check results back to GitHub.

The key architectural principle is:

> **Do not create a second CI configuration language.**

AgentCI should use existing `.github/workflows/*.yml` files as the canonical source of truth wherever possible.

GitHub may remain the source-code host and PR interface, but GitHub Actions should not need to execute the test workload.

---

# 2. Problem Statement

AI coding agents increasingly make substantial repository changes, but CI validation is still commonly deferred until code is pushed to GitHub.

This creates several problems:

- agents discover failures too late,
- GitHub Actions minutes and compute are consumed unnecessarily,
- CI feedback loops are slower,
- agents often do not understand which tests are required,
- raw CI logs are difficult for agents to interpret,
- repeated runs duplicate work that could have been cached locally,
- local validation frequently differs from CI validation,
- developers maintain separate local scripts that drift from actual CI,
- agents may push multiple speculative commits simply to obtain CI feedback.

The desired system should let an AI agent complete the entire validation loop before pushing code.

---

# 3. Product Vision

AgentCI should make CI a callable capability for an AI agent.

The target interaction is:

```text
AI modifies code
      │
      ▼
AgentCI inspects repository
      │
      ▼
AgentCI determines required validation
      │
      ▼
CI jobs execute locally / remotely
      │
      ├── PASS ─────► continue
      │
      └── FAIL
             │
             ▼
      structured failure
             │
             ▼
        AI fixes code
             │
             ▼
      rerun failed jobs
             │
             ▼
       final full CI
             │
             ▼
         push / PR
```

AgentCI should become a stable interface between coding agents and CI systems.

---

# 4. Product Goals

## 4.1 Primary Goals

AgentCI must:

- execute CI outside GitHub Actions,
- use GitHub Actions workflows as the initial source of truth,
- provide a CLI usable by humans and agents,
- expose an agent-friendly API or MCP server,
- generate a normalized CI execution graph,
- execute jobs in isolated environments,
- capture logs, artifacts, metadata, and results,
- return failures in structured machine-readable form,
- support selective reruns,
- support a final full CI verification mode,
- cache dependencies and reusable work,
- prevent "partial CI" from being represented as "full CI",
- optionally report check status back to GitHub.

## 4.2 Secondary Goals

Later versions should:

- perform impact analysis,
- identify the cheapest safe validation path,
- run jobs across multiple local or remote workers,
- support non-GitHub CI formats,
- maintain historical test and failure data,
- support reproducible CI attestations,
- optimize execution through caching and fingerprinting.

---

# 5. Non-Goals

The initial product should **not** attempt to:

- replace GitHub itself,
- replace Git as a version-control system,
- perfectly emulate every GitHub Actions feature in V1,
- create a new CI YAML format,
- implement a complete clone of the GitHub Actions runner from scratch,
- provide production deployment orchestration,
- expose repository secrets to arbitrary agent commands,
- infer that a reduced test set is equivalent to full CI.

---

# 6. Core Design Principles

## 6.1 Existing CI Is the Contract

The repository's CI definitions should remain authoritative.

Preferred input:

```text
.github/workflows/*.yml
```

Avoid:

```text
agentci.yml
```

unless additional AgentCI-specific configuration is truly necessary.

AgentCI-specific settings should extend the repository's existing CI rather than duplicate it.

---

## 6.2 CI Must Be Machine-Readable

Agents should not need to parse thousands of lines of terminal output.

Every CI execution should produce a normalized result such as:

```json
{
  "run_id": "ci_8da311",
  "commit": "ad92ef1",
  "mode": "full",
  "status": "failed",
  "jobs": {
    "lint": {
      "status": "passed",
      "duration_ms": 4210
    },
    "unit-tests": {
      "status": "failed",
      "failed_tests": [
        "tests/test_auth.py::test_expired_token"
      ]
    },
    "build": {
      "status": "blocked",
      "reason": "depends_on: unit-tests"
    }
  }
}
```

---

## 6.3 Local-First but Distributed-Capable

The MVP should run on a developer workstation.

The architecture must also support later execution on:

- a second local machine,
- a Linux build server,
- Kubernetes,
- GPU workers,
- Windows hosts,
- macOS hosts,
- dedicated remote runner pools.

---

## 6.4 Progressive Validation

AgentCI should eventually support:

```text
affected tests
      ↓
package / module tests
      ↓
integration tests
      ↓
full CI
```

A fast partial run is useful during iteration.

A full run is required when full CI assurance is needed.

The system must keep those states distinct.

---

# 7. High-Level Architecture

```text
                  ┌─────────────────────────┐
                  │       AI Agent          │
                  │ Codex / Claude / etc.   │
                  └────────────┬────────────┘
                               │
                         CLI / MCP / API
                               │
                               ▼
                  ┌─────────────────────────┐
                  │        AgentCI          │
                  │                         │
                  │ inspect                 │
                  │ plan                    │
                  │ run                     │
                  │ retry                   │
                  │ explain                 │
                  │ report                  │
                  └────────────┬────────────┘
                               │
                         Workflow Compiler
                               │
                   .github/workflows/*.yml
                               │
                               ▼
                  ┌─────────────────────────┐
                  │   Normalized CI DAG     │
                  └────────────┬────────────┘
                               │
                           Scheduler
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        ┌───────────┐    ┌───────────┐    ┌───────────┐
        │ Executor  │    │ Executor  │    │ Executor  │
        │  Docker   │    │  Remote   │    │  Native   │
        └─────┬─────┘    └─────┬─────┘    └─────┬─────┘
              │                │                │
              └────────────────┼────────────────┘
                               ▼
                  ┌─────────────────────────┐
                  │ Results / Cache / Logs  │
                  │ Artifacts / History     │
                  └────────────┬────────────┘
                               │
                         optional bridge
                               │
                               ▼
                            GitHub
```

---

# 8. Major Components

## 8.1 Repository Inspector

Responsibilities:

- locate CI workflows,
- identify workflow triggers,
- enumerate jobs,
- detect job dependencies,
- detect matrix strategies,
- discover services and containers,
- resolve reusable workflows,
- inventory referenced actions,
- report unsupported features.

Example:

```bash
agentci inspect
```

Example output:

```text
Workflow: CI
Trigger: pull_request

Jobs:
  lint
  test-python
  test-node
  build
  integration

Dependencies:

lint ─────────┐
test-python ──┼──► build ───► integration
test-node ────┘
```

---

## 8.2 Workflow Parser

The parser translates GitHub Actions YAML into an internal AST.

It should understand, over time:

- workflows,
- jobs,
- steps,
- `uses`,
- `run`,
- `needs`,
- `if`,
- `env`,
- `with`,
- matrix strategies,
- services,
- job containers,
- reusable workflows,
- composite actions,
- outputs,
- artifacts,
- secrets references,
- GitHub expression syntax.

---

## 8.3 Normalized CI Intermediate Representation

Do not couple the scheduler directly to GitHub YAML.

Compile CI into a normalized representation.

Example:

```json
{
  "workflow": "ci.yml",
  "jobs": {
    "test": {
      "runner": "ubuntu-latest",
      "needs": [],
      "steps": [
        {
          "type": "action",
          "uses": "actions/checkout@v4"
        },
        {
          "type": "shell",
          "command": "pytest"
        }
      ]
    }
  }
}
```

Benefits:

- easier testing,
- easier planning,
- easier scheduler implementation,
- easier agent inspection,
- future support for GitLab CI, Jenkins, CircleCI, and Azure Pipelines.

---

## 8.4 Planner

The planner converts the normalized graph into an execution plan.

It should support:

```bash
agentci plan
agentci plan --full
agentci plan --required
agentci plan --job unit-tests
```

The planner determines:

- runnable jobs,
- dependency order,
- jobs blocked by dependencies,
- parallelizable jobs,
- unsupported jobs,
- cache reuse,
- runner requirements,
- relevant jobs for changed files.

---

## 8.5 Scheduler

The scheduler is responsible for:

- DAG execution,
- concurrency,
- dependency tracking,
- job retries,
- cancellation,
- timeout enforcement,
- worker allocation,
- resource constraints,
- reporting job state.

Core states:

```text
pending
ready
running
passed
failed
skipped
blocked
cancelled
unsupported
```

---

## 8.6 Executor Interface

Executors should be pluggable.

Initial interface:

```text
prepare(job)
execute(job)
stream_logs(job)
collect_artifacts(job)
cleanup(job)
```

Possible implementations:

```text
ActExecutor
DockerExecutor
NativeExecutor
RemoteExecutor
KubernetesExecutor
WindowsExecutor
MacOSExecutor
```

---

# 9. MVP Execution Strategy

The MVP should avoid implementing full GitHub Actions execution from scratch.

Recommended initial approach:

```text
AgentCI
   │
   ├── custom workflow inspection
   ├── custom normalized DAG
   ├── custom planner
   ├── custom result store
   └── executor adapter
          │
          ▼
         act
```

`act` provides an initial compatibility layer for executing many GitHub Actions workflows using Docker.

AgentCI remains responsible for:

- orchestration,
- agent interface,
- results,
- planning,
- caching strategy,
- failure interpretation,
- compatibility reporting,
- future executor replacement.

This allows the product to become useful before full GitHub Actions compatibility is implemented.

---

# 10. Runner Model

Map workflow runner labels onto AgentCI runner definitions.

Example:

```yaml
runner_mapping:
  ubuntu-latest:
    backend: docker
    image: agentci/ubuntu:latest

  windows-latest:
    backend: remote
    pool: windows

  macos-latest:
    backend: remote
    pool: macos

  gpu:
    backend: remote
    pool: gpu-workers
```

Runner capabilities should include:

```json
{
  "os": "linux",
  "arch": "x86_64",
  "docker": true,
  "gpu": false,
  "memory_gb": 32,
  "labels": [
    "ubuntu-latest",
    "linux"
  ]
}
```

---

# 11. Agent Interface

AgentCI should expose both a CLI and an agent-oriented API.

## 11.1 CLI

Proposed commands:

```bash
agentci inspect
agentci plan
agentci plan --required
agentci plan --full

agentci run
agentci run --required
agentci run --full
agentci run --job unit-tests

agentci status
agentci logs <job>
agentci failures
agentci explain <run-id>

agentci rerun --failed
agentci rerun --job <job>

agentci cache status
agentci cache clean

agentci doctor
```

---

## 11.2 MCP / Agent Tools

Conceptual MCP surface:

```text
ci_inspect()
ci_plan(changes?, mode?)
ci_run(plan_id?)
ci_get_status(run_id)
ci_get_failures(run_id)
ci_get_logs(run_id, job)
ci_get_artifacts(run_id, job)
ci_rerun_failed(run_id)
ci_cancel(run_id)
ci_explain_failure(run_id, job)
```

The agent should not need shell access to use AgentCI.

---

# 12. Agent Execution Flow

Example:

```text
AI edits files
    │
    ▼
ci_plan()
    │
    ▼
Required:
- lint
- unit-python
- integration-db
    │
    ▼
ci_run()
    │
    ▼
FAILED: unit-python
    │
    ▼
ci_get_failures()
    │
    ▼
test_user_creation
expected status=201
received status=409
    │
    ▼
AI fixes code
    │
    ▼
ci_rerun_failed()
    │
    ▼
PASS
    │
    ▼
ci_run(mode="full")
    │
    ▼
FULL CI PASS
```

---

# 13. CI Modes

AgentCI should expose explicit validation modes.

## 13.1 Full Mode

```bash
agentci run --full
```

Meaning:

> Execute the complete supported CI workflow required for the selected event or repository state.

Result:

```text
FULL CI PASS
FULL CI FAIL
```

---

## 13.2 Required / Agent Mode

```bash
agentci run --required
```

Meaning:

> Execute the minimum validation AgentCI believes is relevant to the current change.

Result:

```text
PARTIAL PASS
PARTIAL FAIL
```

A partial pass must never automatically become a full pass.

---

## 13.3 Job Mode

```bash
agentci run --job unit-tests
```

Useful for debugging and targeted iteration.

---

# 14. Impact Analysis

Impact analysis should initially be conservative.

Inputs may include:

```text
changed files
workflow path filters
package ownership
module dependencies
test locations
historical test mapping
coverage data
build graph
previous failures
```

Example:

```text
Changed:

src/ui/button.tsx
src/ui/modal.tsx
```

Possible plan:

```text
RUN
  eslint
  typescript
  ui-unit-tests

SKIP
  database-integration
  backend-unit-tests
  docker-build
```

The planner should provide a reason for every selected or omitted job.

Example:

```json
{
  "job": "backend-unit-tests",
  "decision": "skip",
  "reason": "No changed files intersect backend dependency graph."
}
```

---

# 15. Caching

Caching should be a first-class system.

Suggested layout:

```text
~/.agentci/

cache/
  npm/
  pip/
  cargo/
  gradle/
  actions/
  docker/

artifacts/
runs/
logs/
workspaces/
metadata/
```

Cache classes:

1. dependency cache,
2. action cache,
3. runner image cache,
4. Docker layer cache,
5. test result cache,
6. workflow compilation cache,
7. artifact cache.

---

# 16. Run Fingerprints

Every run or job should have a reproducibility fingerprint.

Conceptual fingerprint:

```text
SHA256(
    git_commit
  + working_tree_hash
  + workflow_hash
  + job_definition_hash
  + runner_image_hash
  + environment_hash
  + dependency_lock_hash
  + secret_version_refs
)
```

Example:

```text
ci:e8c1c718a210...
```

Stored record:

```json
{
  "fingerprint": "ci:e8c1c718a210",
  "commit": "8fd0212",
  "workflow_hash": "4f8291",
  "runner_hash": "921ab",
  "status": "passed",
  "mode": "full",
  "tests": 1284
}
```

This enables:

- exact result reuse,
- avoidance of duplicate CI,
- confidence that cached results apply to the current state,
- CI attestations,
- provenance reporting.

---

# 17. Result Storage

Recommended logical schema:

```text
runs
jobs
steps
logs
artifacts
test_results
cache_entries
workflow_versions
runner_versions
attestations
```

Example `runs` fields:

```text
run_id
repository
commit_sha
working_tree_hash
workflow_hash
mode
started_at
finished_at
status
initiator
agent_id
```

Example `jobs` fields:

```text
job_id
run_id
name
runner
status
started_at
finished_at
exit_code
fingerprint
cache_hit
```

SQLite is sufficient for an MVP.

A server deployment can later use PostgreSQL.

---

# 18. Failure Normalization

A central feature of AgentCI should be converting raw logs into structured failures.

Example:

```json
{
  "job": "unit-python",
  "category": "test_failure",
  "framework": "pytest",
  "failures": [
    {
      "test": "tests/test_auth.py::test_expired_token",
      "file": "tests/test_auth.py",
      "line": 89,
      "message": "Expected 401 but received 200"
    }
  ]
}
```

Potential parsers:

- pytest,
- Jest,
- Vitest,
- Go test,
- Cargo test,
- Maven,
- Gradle,
- ESLint,
- TypeScript,
- Ruff,
- MyPy,
- GCC/Clang,
- CMake,
- Docker build.

Fallback:

```json
{
  "category": "command_failure",
  "exit_code": 1,
  "stderr_tail": "..."
}
```

---

# 19. GitHub Integration

GitHub integration is optional for the core product.

When enabled, AgentCI should be able to publish:

```text
AgentCI / lint
AgentCI / unit
AgentCI / integration
AgentCI / build
```

Possible mechanisms:

- commit statuses,
- GitHub Checks API,
- GitHub App.

The workload still executes outside GitHub Actions.

GitHub is only used as:

- source host,
- PR interface,
- status display,
- merge gate.

---

# 20. Security Model

AgentCI must assume:

```text
repository code = untrusted
workflow code = potentially untrusted
PR metadata = untrusted
AI output = untrusted
secrets = trusted
host machine = protected
```

## 20.1 Isolation

CI jobs should run inside isolated environments wherever possible.

Example:

```text
AgentCI Controller
      │
      │ limited execution contract
      ▼
Ephemeral Job Environment
  - repository checkout
  - temporary filesystem
  - scoped environment variables
  - restricted credentials
  - optional network limits
      │
      ▼
destroy after job
```

---

## 20.2 Secrets

Secrets must:

- be stored outside the repository,
- be explicitly mapped to jobs,
- never be returned to the AI agent,
- be redacted from logs,
- support version identifiers for fingerprinting,
- use least-privilege credentials.

---

## 20.3 Network Policy

Future support should include:

```text
network: none
network: package-registries
network: allowlist
network: unrestricted
```

Default should be as restrictive as practical.

---

## 20.4 Agent Permissions

The AI should have explicit permission scopes.

Example:

```text
ci.inspect
ci.plan
ci.run
ci.read_logs
ci.read_artifacts
ci.cancel
```

Separate elevated permissions:

```text
ci.manage_secrets
ci.manage_runners
ci.publish_github_status
ci.clear_global_cache
```

---

# 21. Configuration

AgentCI-specific configuration should be minimal.

Possible location:

```text
.agentci/config.yml
```

Example:

```yaml
version: 1

execution:
  default_mode: required
  max_parallel_jobs: 4

runners:
  ubuntu-latest:
    backend: docker
    image: agentci/ubuntu:latest

security:
  network_default: restricted

github:
  report_status: false

impact_analysis:
  enabled: true
  conservative: true
```

This file augments workflow behavior.

It should not duplicate workflow jobs or steps.

---

# 22. Compatibility Strategy

AgentCI should maintain an explicit compatibility report.

Example:

```bash
agentci doctor
```

Output:

```text
GitHub Actions compatibility

✓ shell steps
✓ actions/checkout
✓ job dependencies
✓ basic matrices
✓ environment variables
✓ Docker services

PARTIAL
△ reusable workflows
△ artifacts

UNSUPPORTED
✗ macOS hosted runner emulation
✗ selected marketplace action X
```

AgentCI should fail clearly rather than silently behave differently from GitHub Actions.

---

# 23. Observability

Every run should expose:

```text
run status
job status
step status
duration
cache hits
resource usage
exit code
logs
artifacts
failure summary
runner identity
fingerprint
```

Optional later metrics:

```text
CI time saved
cache hit rate
average agent fix iterations
most flaky tests
test failure frequency
most expensive jobs
parallelization efficiency
```

---

# 24. Product Requirements Documents

---

# PRD 1 — AgentCI Core

## Objective

Build the minimum local CI platform capable of discovering and executing CI workflows and returning reliable structured results.

## User

Primary user:

> AI coding agent operating inside a Git repository.

Secondary user:

> Developer manually invoking CI.

## User Story

> As an AI coding agent, I want to run the repository's CI locally so that I can verify and repair my changes before pushing them to GitHub.

## Functional Requirements

### Repository discovery

AgentCI must:

- locate `.github/workflows`,
- enumerate workflows,
- parse valid workflow YAML,
- report parse failures.

### Job discovery

AgentCI must extract:

- job name,
- dependencies,
- runner label,
- steps,
- environment,
- job conditions,
- services,
- containers.

### Execution

AgentCI must support:

- running one workflow,
- running one job,
- running dependency chains,
- cancelling a run,
- non-zero exit propagation.

### Output

AgentCI must produce:

- human-readable terminal output,
- JSON result output,
- stored logs,
- job status,
- step status,
- duration,
- exit code.

### Persistence

MVP may use SQLite.

## CLI Acceptance Criteria

The following must work:

```bash
agentci inspect
agentci plan
agentci run
agentci run --job <job>
agentci status
agentci logs <job>
agentci failures
```

## Success Criteria

The MVP is successful when:

1. a repository with a normal GitHub Actions CI workflow can be inspected,
2. AgentCI shows the job DAG,
3. CI jobs can execute locally,
4. failures are visible without GitHub,
5. the AI can rerun failed validation after changing code.

---

# PRD 2 — Agent Integration / MCP

## Objective

Expose AgentCI as a safe, structured tool surface for coding agents.

## User Story

> As an autonomous coding agent, I want callable CI tools so that I can validate, diagnose, fix, and revalidate code without relying on shell-output interpretation.

## Required Tools

```text
ci_inspect
ci_plan
ci_run
ci_get_status
ci_get_failures
ci_get_logs
ci_rerun_failed
ci_cancel
```

## Requirements

### Structured output

All tools must return JSON-compatible objects.

### Stable schemas

Tool output must be versioned.

Example:

```json
{
  "schema_version": "1",
  "run_id": "ci_123",
  "status": "failed"
}
```

### Bounded logs

Agents should receive:

- failure summaries by default,
- targeted log ranges on request,
- full logs only when explicitly requested.

This reduces token usage.

### Failure extraction

Known test frameworks should return individual test failures.

### Safety

The agent interface must not expose CI secrets.

## Success Criteria

An AI agent can perform the following loop without human intervention:

```text
inspect
→ plan
→ run
→ detect failure
→ inspect failure
→ modify code
→ rerun failed job
→ full verification
```

---

# PRD 3 — Full vs Required Validation

## Objective

Allow fast agent iteration while maintaining clear assurance boundaries.

## User Story

> As an AI coding agent, I want to run only the likely affected validation during development but still perform complete CI before declaring the change fully verified.

## Modes

### Required

```text
PARTIAL PASS
PARTIAL FAIL
```

### Full

```text
FULL CI PASS
FULL CI FAIL
```

## Requirements

Required-mode decisions must include reasoning.

Example:

```json
{
  "job": "database-tests",
  "decision": "skip",
  "reason": "No changed module reaches database package."
}
```

A partial pass must never satisfy a request for full verification.

## Initial Selection Rules

V1 required-mode selection can use:

- changed file paths,
- workflow path filters,
- explicit repository mapping rules.

Later versions add dependency graphs and historical coverage.

## Success Criteria

AgentCI can reduce iterative validation cost without confusing reduced validation with complete CI.

---

# PRD 4 — Caching and Reproducibility

## Objective

Make repeated AI validation fast and avoid redundant CI execution.

## User Story

> As an AI coding agent, I want unchanged work to be reused safely so that I can validate code repeatedly without paying full CI cost each time.

## Required Cache Types

- dependency cache,
- actions cache,
- runner image cache,
- build cache where supported.

## Run Fingerprints

Each job receives a deterministic fingerprint.

A cached pass may be reused only when all required fingerprint inputs match.

## Requirements

AgentCI must report:

```text
cache hit
cache miss
cached result reused
cache invalidated
```

## Success Criteria

A rerun after a small source edit should avoid reinstalling unchanged dependencies.

---

# PRD 5 — GitHub Status Bridge

## Objective

Allow AgentCI results to participate in GitHub merge protection without executing the workload in GitHub Actions.

## User Story

> As a repository owner, I want GitHub PRs to require AgentCI checks even though the tests execute on my own infrastructure.

## Requirements

AgentCI should be able to report:

```text
pending
success
failure
error
```

Checks should be tied to a commit SHA.

Example check names:

```text
AgentCI / lint
AgentCI / unit-tests
AgentCI / build
AgentCI / full-ci
```

## Security

GitHub credentials must not be exposed to the coding agent.

The AgentCI controller performs status publication.

## Success Criteria

A GitHub branch rule can require an AgentCI check before merge.

---

# PRD 6 — Remote Runner Pool

## Objective

Allow CI workloads to execute outside the developer workstation.

## User Story

> As a user, I want AgentCI to dispatch jobs to machines with the required operating system or hardware while preserving the same agent interface.

## Runner Capabilities

Workers advertise:

```text
OS
architecture
CPU
memory
GPU
labels
Docker capability
available tools
```

## Requirements

The scheduler must:

- select compatible workers,
- transfer repository state,
- stream logs,
- collect artifacts,
- report worker failures,
- clean worker state.

## Success Criteria

A workflow containing Linux and GPU jobs can dispatch those jobs to separate machines automatically.

---

# PRD 7 — CI Intelligence

## Objective

Make AgentCI capable of reasoning about validation cost, failures, and historical behavior.

## User Stories

> Which tests should I run?

> Why is this job blocked?

> What changed since the last passing run?

> Is this test flaky?

> What is the cheapest safe validation plan?

## Inputs

- historical AgentCI runs,
- Git diff,
- repository dependency graph,
- test mapping,
- coverage information,
- workflow dependencies,
- previous failures.

## Outputs

Example:

```json
{
  "recommendation": [
    "lint",
    "unit-auth",
    "integration-api"
  ],
  "confidence": 0.93,
  "reasoning": [
    "auth package changed",
    "api imports auth package",
    "integration-api covers modified path"
  ]
}
```

## Requirement

Recommendations must remain advisory unless policy explicitly allows automated selection.

---

# 25. Roadmap

The roadmap is organized into implementation phases rather than calendar dates.

---

## Phase 0 — Technical Spike

### Goal

Validate the feasibility of the architecture.

### Deliverables

- repository workflow discovery,
- experiment using `act`,
- run one CI job locally,
- capture exit status,
- capture logs,
- document unsupported workflow features.

### Exit Criteria

At least one representative real repository can execute its CI workflow locally without GitHub Actions.

---

## Phase 1 — AgentCI Core MVP

### Scope

Build:

```text
agentci inspect
agentci plan
agentci run
agentci status
agentci logs
agentci failures
agentci rerun --failed
```

### Components

- CLI,
- workflow parser,
- normalized IR,
- DAG builder,
- scheduler,
- Act executor adapter,
- SQLite run store,
- log persistence.

### Exit Criteria

An AI agent can edit code and run CI locally through a stable command interface.

---

## Phase 2 — Structured Agent Interface

### Scope

Build MCP/API support.

### Deliverables

- `ci_inspect`,
- `ci_plan`,
- `ci_run`,
- `ci_get_status`,
- `ci_get_failures`,
- `ci_get_logs`,
- `ci_rerun_failed`,
- versioned schemas.

### Exit Criteria

The complete failure → fix → rerun loop works without direct terminal parsing.

---

## Phase 3 — Compatibility Expansion

### Scope

Increase GitHub Actions compatibility.

Priority:

1. expressions,
2. matrices,
3. `needs`,
4. conditional jobs,
5. service containers,
6. composite actions,
7. reusable workflows,
8. artifacts,
9. caches,
10. environment behavior.

### Deliverable

Compatibility test suite containing sample workflows.

### Exit Criteria

AgentCI can run the majority of workflows used by target repositories.

---

## Phase 4 — Caching + Fingerprints

### Scope

Build:

- dependency cache,
- actions cache,
- image cache,
- workflow compilation cache,
- deterministic job fingerprints,
- cached result reuse.

### Exit Criteria

Repeated agent runs are substantially faster while maintaining reproducibility guarantees.

---

## Phase 5 — Agent-Optimized CI

### Scope

Implement:

```text
agentci plan --required
agentci run --required
```

Inputs:

- Git diff,
- path ownership,
- workflow filters,
- package dependency graph.

Later:

- test dependency graph,
- coverage mapping,
- historical failures.

### Exit Criteria

AgentCI can safely reduce common iterative validation runs while clearly reporting `PARTIAL PASS`.

---

## Phase 6 — Native Execution Engine

### Scope

Begin replacing `act` where compatibility or performance requires it.

Build:

- Docker executor,
- step environment manager,
- action resolver,
- services manager,
- artifact manager,
- expression evaluator.

### Exit Criteria

Selected workflows can execute without the `act` dependency.

---

## Phase 7 — Remote Execution

### Scope

Add controller + worker architecture.

Components:

```text
AgentCI Controller
Runner Registry
Job Queue
Worker Agent
Artifact Transfer
Log Streaming
```

### Exit Criteria

Jobs can be scheduled transparently across multiple machines.

---

## Phase 8 — GitHub Integration

### Scope

Build external status reporting.

Options:

- commit-status integration first,
- GitHub App / Checks integration later.

### Exit Criteria

GitHub branch protection can require successful AgentCI results.

---

## Phase 9 — CI Intelligence

### Scope

Add higher-level CI reasoning.

Features:

- impacted-test prediction,
- flaky-test detection,
- historical comparisons,
- expensive-job detection,
- test prioritization,
- failure clustering,
- confidence scores,
- optimal CI planning.

### Exit Criteria

AgentCI provides useful validation recommendations beyond static workflow interpretation.

---

# 26. Suggested Repository Structure

```text
agentci/

  cmd/
    agentci/

  internal/

    workflows/
      parser/
      expressions/
      github/
      ir/

    planner/
      dag/
      impact/

    scheduler/

    executors/
      act/
      docker/
      native/
      remote/

    runners/

    results/
      store/
      parsers/

    cache/

    artifacts/

    security/
      secrets/
      permissions/
      network/

    github/
      status/
      checks/

    agent/
      mcp/
      api/

  schemas/

  tests/
    fixtures/
    workflows/
    integration/

  docs/

  .github/
    workflows/
```

Go is a strong implementation candidate because it is suitable for:

- CLIs,
- concurrency,
- static binaries,
- container orchestration,
- servers,
- worker agents,
- cross-platform tooling.

Rust would also be appropriate if stronger memory-safety and systems-level control are preferred.

Python can work for the MVP but may become less convenient for a highly concurrent multi-runner system.

---

# 27. Suggested Core Interfaces

## Workflow compiler

```text
ParseWorkflow(path) -> WorkflowAST

CompileWorkflow(
    WorkflowAST,
    EventContext
) -> WorkflowGraph
```

## Planner

```text
CreatePlan(
    WorkflowGraph,
    RepositoryState,
    ValidationMode
) -> ExecutionPlan
```

## Executor

```text
Executor.prepare(job)
Executor.run(job)
Executor.cancel(job)
Executor.collect(job)
Executor.cleanup(job)
```

## Result parser

```text
ParseFailure(
    command,
    stdout,
    stderr,
    exitCode
) -> FailureReport
```

---

# 28. Event Context

GitHub workflows depend on events.

AgentCI should model event context explicitly.

Example:

```bash
agentci run --event pull_request
```

Event context might include:

```json
{
  "event": "pull_request",
  "base_branch": "main",
  "head_branch": "feature/auth",
  "base_sha": "abc123",
  "head_sha": "def456"
}
```

The event simulator should eventually cover:

```text
push
pull_request
workflow_dispatch
schedule
release
```

For local agent usage, `pull_request` is likely the most important starting event.

---

# 29. Working Tree Handling

AI agents often run CI before committing.

Therefore AgentCI must support both:

```text
committed SHA
```

and:

```text
dirty working tree
```

Fingerprinting should include uncommitted changes.

Example:

```text
commit SHA
+
git diff
+
untracked relevant files
```

This is important because the agent should not need to create fake commits solely to run CI.

---

# 30. Concurrency

V1:

```text
max_parallel_jobs: 2-4
```

The scheduler should respect DAG dependencies.

Example:

```text
lint ───────────┐
unit-python ────┼──► build
unit-node ──────┘
```

The three independent jobs can run concurrently.

`build` starts only after required dependencies pass.

---

# 31. Failure Policies

Supported policies:

```text
fail-fast
continue-on-error
always-run
dependency-blocked
```

The normalized IR should explicitly encode these semantics.

---

# 32. Artifacts

AgentCI should support local artifacts.

Example:

```text
.agentci/artifacts/<run-id>/<job>/
```

Artifact metadata:

```json
{
  "name": "coverage-report",
  "path": "...",
  "size": 821221,
  "sha256": "..."
}
```

Future remote storage:

```text
S3
MinIO
Azure Blob
Google Cloud Storage
```

---

# 33. Testing Strategy for AgentCI

AgentCI itself requires an extensive compatibility test suite.

## Unit Tests

Test:

- YAML parsing,
- expressions,
- DAG construction,
- matrix expansion,
- fingerprints,
- cache lookup,
- result parsing.

## Workflow Fixtures

Create test repositories/workflows such as:

```text
simple-shell.yml
matrix.yml
needs.yml
conditional.yml
services-postgres.yml
docker-job.yml
composite-action.yml
reusable-workflow.yml
cache.yml
artifacts.yml
```

## Golden Tests

Expected normalized workflow graphs should be stored and diffed.

## Integration Tests

Execute real sample jobs inside containers.

## Compatibility Tests

Compare selected AgentCI behavior against known GitHub Actions behavior.

---

# 34. MVP Definition

The project should resist scope expansion until this workflow works:

```text
1. Agent edits repository.
2. Agent calls `agentci inspect`.
3. AgentCI discovers GitHub workflow.
4. Agent calls `agentci run`.
5. CI executes outside GitHub.
6. A test fails.
7. Agent receives structured failure.
8. Agent modifies code.
9. Agent calls `agentci rerun --failed`.
10. Test passes.
11. Agent calls `agentci run --full`.
12. AgentCI reports FULL CI PASS.
```

Everything else should be considered optimization or expansion.

---

# 35. Recommended First Milestone

The first implementation milestone should support this repository:

```yaml
name: CI

on:
  pull_request:

jobs:

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm test

  build:
    needs: [lint, test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run build
```

Required output:

```text
$ agentci inspect

Workflow: CI

Jobs:
  lint
  test
  build

Dependencies:
  lint ─┐
        ├── build
  test ─┘
```

Then:

```text
$ agentci run

✓ lint
✗ test
⊘ build

1 failed job
```

Then:

```text
$ agentci failures

test:
  src/auth.test.ts
  expected 401
  received 200
```

That single flow validates the central product idea.

---

# 36. Key Engineering Decisions

The implementation agent should explicitly review and decide the following before major development begins.

## Decision 1

**Language**

Recommended default:

```text
Go
```

Alternatives:

```text
Rust
Python
```

---

## Decision 2

**MVP execution backend**

Recommended:

```text
act adapter
```

Do not implement a complete Actions runtime initially.

---

## Decision 3

**State database**

Recommended MVP:

```text
SQLite
```

Later:

```text
PostgreSQL
```

---

## Decision 4

**Isolation**

Recommended:

```text
Docker containers
```

---

## Decision 5

**Agent protocol**

Recommended:

```text
CLI + MCP
```

Both should use the same internal API.

---

## Decision 6

**Configuration philosophy**

Recommended:

```text
.github/workflows = CI source of truth

.agentci/config.yml = optional execution extensions
```

---

# 37. Risks

## GitHub Actions Compatibility

Risk:

GitHub Actions contains substantial behavior beyond simple YAML execution.

Mitigation:

- use `act` initially,
- maintain compatibility tests,
- explicitly report unsupported features,
- implement native execution incrementally.

---

## Unsafe Workflow Execution

Risk:

Repository workflows may execute dangerous commands.

Mitigation:

- isolated execution,
- restricted secrets,
- restricted network,
- explicit host mounts,
- disposable workspaces.

---

## Incorrect Impact Analysis

Risk:

AgentCI skips a necessary test.

Mitigation:

- conservative defaults,
- explain every skip,
- never label partial validation as full validation,
- require full CI for strong assurance policies.

---

## Cache Poisoning

Risk:

An invalid cached result is reused.

Mitigation:

- deterministic fingerprints,
- content-addressed caches,
- environment hashes,
- cache provenance.

---

## Agent Token Explosion

Risk:

Huge CI logs overwhelm the coding agent.

Mitigation:

- normalized failure parsers,
- targeted logs,
- bounded log retrieval,
- failure-first API.

---

# 38. Long-Term Product Direction

The strongest long-term version of AgentCI is not merely:

> "GitHub Actions on my computer."

It is:

> **A CI operating layer for autonomous software agents.**

In that model the agent asks AgentCI questions such as:

```text
What validation does this change require?

Which CI jobs are likely affected?

Which test failed?

Show me only the relevant failure context.

What is blocked by this failure?

Which failed jobs should I rerun?

Has this exact build already passed?

What is the cheapest next validation step?

Is the current code fully CI-verified?

What changed since the last passing run?
```

AgentCI becomes the system responsible for answering those questions reliably.

---

# 39. Definition of Done for V1

V1 is complete when all of the following are true:

- GitHub workflow files are discovered automatically.
- A normalized CI job graph can be generated.
- A Linux-based workflow can run without GitHub Actions.
- Job dependencies are respected.
- Logs and statuses are persisted.
- Failed jobs can be rerun independently.
- Results are available as structured JSON.
- A coding agent can invoke AgentCI without parsing raw terminal output.
- Dirty working-tree changes are supported.
- A final run can be explicitly labeled `FULL CI PASS` or `FULL CI FAIL`.
- No repository secret is directly exposed through the agent API.

---

# 40. Immediate Implementation Backlog

Suggested first backlog:

```text
[ ] Initialize AgentCI repository
[ ] Select implementation language
[ ] Build CLI skeleton
[ ] Implement repository detection
[ ] Discover .github/workflows
[ ] Parse workflow YAML
[ ] Define normalized workflow IR
[ ] Build DAG representation
[ ] Render `agentci inspect`
[ ] Integrate act executor
[ ] Execute one selected job
[ ] Execute dependency graph
[ ] Store run metadata in SQLite
[ ] Store stdout/stderr logs
[ ] Build `agentci status`
[ ] Build `agentci logs`
[ ] Build generic failure object
[ ] Add Jest failure parser
[ ] Add pytest failure parser
[ ] Build `agentci failures`
[ ] Build `agentci rerun --failed`
[ ] Add dirty working-tree fingerprint
[ ] Define FULL vs PARTIAL result enums
[ ] Add JSON output mode
[ ] Define MCP tool schemas
[ ] Implement MCP server
[ ] Create workflow compatibility fixtures
[ ] Add `agentci doctor`
[ ] Document unsupported GitHub Actions features
```

---

# 41. Final Recommendation

Start with a deliberately narrow system:

```text
GitHub Actions YAML
        │
        ▼
AgentCI parser
        │
        ▼
Normalized DAG
        │
        ▼
AgentCI planner
        │
        ▼
act / Docker execution
        │
        ▼
Structured results
        │
        ▼
AI agent
```

Do not begin with:

- distributed runners,
- sophisticated test prediction,
- GitHub App integration,
- complete Actions emulation,
- multi-CI support.

The initial product should prove one thing extremely well:

> **An AI coding agent can make a code change, execute the repository's real CI logic locally, understand failures programmatically, repair the code, rerun validation, and obtain a reliable full-CI result before pushing to GitHub.**

Once that loop is dependable, the rest of the roadmap becomes incremental rather than speculative.
