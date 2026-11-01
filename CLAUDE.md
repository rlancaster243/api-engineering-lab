# CLAUDE.md

## Working model

- **The main session plans, designs and reviews. Subagents implement.** The main session writes code only for one-line fixes.
- **Use the cheapest model and effort that gets the task right.** Mechanical edits, renames, lockfile or doc updates: Haiku, low effort. Ordinary feature code and tests against a clear brief: Sonnet. Opus or high effort only for work that is genuinely hard to get right (concurrency, transactions, security-sensitive logic) or after a cheaper attempt failed review.
- **A subagent brief is the whole spec for that task.** State the goal, the files, the spec sections and ADRs that govern it, the tests that must exist, the commands to run, and what is out of scope. Do not hand over a conclusion to confirm.
- **Review every subagent result before it is committed.** Read the diff, run lint, format and the full test suite (with PostgreSQL), and check that tests can fail: break the change deliberately and confirm a test catches it. Send problems back to the subagent rather than rewriting the work.

## Governing documents

- `docs/PROJECT_1_ENGINEERING_FIDELITY.md` is the contract for `01-rest-api`. Code follows it; changing behavior means changing the spec first.
- ADRs live in `docs/decisions/`. This spec's decisions are `P1-ADR-NNN`; a bare `ADR-NNN` means the syllabus register (`docs/API_ENGINEERING_SYLLABUS.md` §13). Cite ADRs by file number (`0003`) in PRs and commits.
- New ADRs are written as **Proposed**. Only the repository owner moves one to Accepted.

## Commands (`01-rest-api/`)

```bash
uv sync --locked --extra dev          # never plain `uv sync`; commit uv.lock with any pyproject change
uv run ruff check . && uv run ruff format --check .
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rest_api_lab_test \
  REQUIRE_DATABASE=1 uv run pytest -q
uv run alembic upgrade head           # needs DATABASE_URL
```

- PostgreSQL tests rebuild the schema, so they read `TEST_DATABASE_URL` and never `DATABASE_URL`. Point it at a disposable database.
- Without `TEST_DATABASE_URL` those tests skip; `REQUIRE_DATABASE=1` (set in CI) makes that a failure. Do not report a result as passing if the PostgreSQL tests were skipped.
- The app fails closed at startup without `DATABASE_URL` and a 32+ byte `CURSOR_SIGNING_KEY`; `AUTH_MODE=local_dev` needs `APP_ENV=local` or `test`.

## Git and pull requests

- Never rebase, amend or force-push a branch someone else created; merge the base branch in instead.
- Open PRs as drafts. Merge only when the owner says to, after CI is green on the current head.
- Each PR description carries the spec §9 evidence record: commands run, actual results, and known limitations.
