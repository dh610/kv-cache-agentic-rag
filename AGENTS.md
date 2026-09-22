# Shared development contract

- Before editing, read README.md, docs/onboarding.md (including the Git workflow), and docs/team-contract.md for the current role/file boundaries. Read docs/contracts.md before changing node contracts.
- Inspect `git status --short --branch` first. Do not edit, commit, or push directly on main. Start new work from an up-to-date main on a personal task branch, such as `feat/market-prompt-1nyeonart` or `docs/team-workflow-dh610`; an existing assigned branch such as `agent/tech` is also valid. Reuse the current branch only when it belongs to the same person's task.
- Preserve existing uncommitted work. Do not reset, clean, stash, switch another person's branch, or overwrite their changes merely to start your task; use a separate worktree when necessary.
- Stage only the intended files, inspect the staged diff, and push the task branch. Submit a PR targeting main; do not merge unless the user has authorized it. Do not force-push shared branches.
- Follow the named assignments in docs/team-contract.md. Ask for the role only if it is unclear from the request; do not infer it from the clone owner's Git identity. Domain, synthesis and report remain unassigned and must not be silently assigned to another role.
- Shared-file changes require an explanation of the affected contracts and coordination with the relevant owners in the PR. File boundaries reduce conflicts; they do not replace PR review or enforce GitHub branch protection.
- Follow docs/team-contract.md for question batching, references and handoff checks; track remaining design differences in docs/final-design-review.md.
- Use uv and Python 3.11. Keep pyproject.toml and uv.lock in sync for dependency changes.
- Prefer node-owned prompts/<node>, rubrics/<node>.yaml, and tests/fixtures/<node> for role development.
- Shared graph/schema changes must retain all question handling, bounded retries, evidence identity, and failure propagation.
- Never describe mock output as a factual technology assessment or a retrieval quality measurement.
- Do not silently turn unknown into a low score or clear unverified findings.
- Keep .env, credentials, private papers, model/index caches and generated outputs out of Git. .env.example must contain placeholders only.
- For code, prompt, rubric or fixture changes, validate with: uv run ruff check .; uv run ruff format --check .; uv run pytest -q; uv run python -m app.run_pipeline --mode mock. For documentation-only changes, check links/paths and `git diff --check`; no live API calls or model downloads are needed.
- For retrieval storage changes also run: uv run --with faiss-cpu --with numpy pytest tests/test_index.py -q.
- Avoid live paid API calls for tests. Use fixture mode deliberately when the user has configured credentials and requested live evaluation.
