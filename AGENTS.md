# Shared development contract

- Read README.md and docs/contracts.md before changing node contracts.
- Use uv and Python 3.11. Keep pyproject.toml and uv.lock in sync for dependency changes.
- Prefer node-owned prompts/<node>, rubrics/<node>.yaml, and tests/fixtures/<node> for role development.
- Shared graph/schema changes must retain all question handling, bounded retries, evidence identity, and failure propagation.
- Never describe mock output as a factual technology assessment or a retrieval quality measurement.
- Do not silently turn unknown into a low score or clear unverified findings.
- Keep .env, credentials, private papers, model/index caches and generated outputs out of Git. .env.example must contain placeholders only.
- Validate behavior with: uv run ruff check .; uv run ruff format --check .; uv run pytest -q; uv run python -m app.run_pipeline --mode mock.
- For retrieval storage changes also run: uv run --with faiss-cpu --with numpy pytest tests/test_index.py -q.
- Avoid live paid API calls for tests. Use fixture mode deliberately when the user has configured credentials and requested live evaluation.
