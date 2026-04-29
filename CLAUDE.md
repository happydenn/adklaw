# Coding Agent Guide

## Prerequisites

Install the CLI (one-time):
```bash
uv tool install google-agents-cli
```

---

## Development Phases

### Phase 1: Understand Requirements
Before writing any code, understand the project's requirements, constraints, and success criteria.

### Phase 2: Build and Implement
Implement agent logic in `app/`. Use `agents-cli playground` for interactive testing. Iterate based on user feedback.

### Phase 3: The Evaluation Loop (Main Iteration Phase)
Start with 1-2 eval cases, run `agents-cli eval run`, iterate. Expect 5-10+ iterations. See the **Evaluation Guide** for metrics, evalset schema, LLM-as-judge config, and common gotchas.

### Phase 4: Pre-Deployment Tests
Run `uv run pytest tests/unit tests/integration`. Fix issues until all tests pass.

### Phase 5: Deploy to Dev
**Requires explicit human approval.** Run `agents-cli deploy` only after user confirms. See the **Deployment Guide** for details.

### Phase 6: Production Deployment
Ask the user: Option A (simple single-project) or Option B (full CI/CD pipeline with `agents-cli infra cicd`).

## Development Commands

| Command | Purpose |
|---------|---------|
| `agents-cli playground` | Interactive local testing |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests |
| `agents-cli eval run` | Run evaluation against evalsets |
| `agents-cli lint` | Check code quality |
| `agents-cli infra single-project` | Set up project infrastructure (Terraform) |
| `agents-cli deploy` | Deploy to dev |
| `agents-cli scaffold enhance` | Add deployment target or CI/CD to project |
| `agents-cli scaffold upgrade` | Upgrade project to latest version |

---

## Documentation

The reasoning behind a non-obvious design decision is the most expensive thing to recover and the easiest thing to lose. Plans expire. Conversations get compacted. **Write durable design notes to `docs/` whenever you make a non-obvious architectural decision.**

Start at `docs/architecture-overview.md` — it's the navigable index. Existing examples (`docs/channels-context.md`, `docs/channels-gateway.md`) show the voice: short narrative covering *the problem*, *what we do*, and *why* — not exhaustive API docs.

When to write a doc:
- A new abstraction or service interface lands.
- A non-obvious tradeoff was made (e.g., "why workspace is not a backend interface").
- A deployment-shape concern was resolved.
- A privacy / safety / scope decision was made.

When *not* to write a doc:
- Bug fixes with self-evident reasoning.
- Renames, refactors that don't change shape.
- Anything already obvious from the code + commit message.

**Decisions and deferrals.** When you make an architectural decision, append it to `docs/decisions-and-deferrals.md` under **Decided**. When you explicitly punt on a question rather than resolve it, append to **Deferred** with the trigger that would force a decision. When you turn down a viable-looking alternative, append to **Rejected** with the reasoning. This file is the durable record — read it before opening any "why is X built this way?" conversation.

The doc lands in the same PR as the code; the PR description references it. This way the design rationale is reviewed and merged with the implementation, not deferred to a later cleanup that never happens.

## Operational Guidelines for Coding Agents

- **Code preservation**: Only modify code directly targeted by the user's request. Preserve all surrounding code, config values (e.g., `model`), comments, and formatting.
- **NEVER change the model** unless explicitly asked.
- **Model 404 errors**: Fix `GOOGLE_CLOUD_LOCATION` (e.g., `global` instead of `us-east1`), not the model name.
- **ADK tool imports**: Import the tool instance, not the module: `from google.adk.tools.load_web_page import load_web_page`
- **Run Python with `uv`**: `uv run python script.py`. Run `agents-cli install` first.
- **Stop on repeated errors**: If the same error appears 3+ times, fix the root cause instead of retrying.
- **Terraform conflicts** (Error 409): Use `terraform import` instead of retrying creation.
