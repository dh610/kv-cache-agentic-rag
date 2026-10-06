# Latency ablation experiment

This is an experimental runner for locating latency in the existing six-role pipeline. It does not replace production defaults, bypass citation verification, or turn unresolved findings into successful judgments. The public NodeInput/NodeRun contracts, all acceptance questions and rubric IDs remain unchanged.

## Experiment matrix

| Run | Judge execution | Search attempts/question | Answer fixes | Supplement rounds | Report |
| --- | --- | --- | --- | --- | --- |
| baseline | bounded parallel, 4 across research roles | 2 | 0 | 0 | code assembly |
| A_serial_judge | original sequential per role | 2 | 0 | 0 | code assembly |
| B_followup_search | bounded parallel | 3 | 0 | 0 | code assembly |
| C_answer_fix | bounded parallel | 2 | 1 | 0 | code assembly |
| D_supplement | bounded parallel | 2 | 0 | 1 | code assembly |
| E_llm_report | bounded parallel | 2 | 0 | 0 | original LLM generation/verification |
| full_control | original sequential per role | 3 | 1 | 1 | original LLM generation/verification |

The baseline still calls the planner, sufficiency model, generators and citation Judge, and still performs positive/critical retrieval. It removes the five selected latency multipliers, not every possible source of latency. A parallel Judge uses the existing per-claim method with the same payload and validation; no batching or fabricated supported checks are introduced.

One factor is restored per contrast. The suite brackets a seeded randomized factor order with two baseline runs, followed by full_control: eight runs total. These are pilot measurements, not statistically established causal rankings. Live search results and LLM outputs vary; repeat the largest contrast with fixed evidence/payloads before declaring a winner. Effects and interactions are not additive. If fix, rewrite or supplement never executes, that contrast is unexercised, not proven free.

## Commands

Run from the repository root, using the existing Python 3.11 environment:

```bash
uv run --no-sync python -m app.benchmark_latency health \
  --output outputs/local/latency-health

uv run --no-sync python -m app.benchmark_latency suite --mode live \
  --output outputs/local/latency-live-UNIQUE \
  --timeout 600 --max-calls 300

uv run --no-sync python -m app.benchmark_latency run --mode live \
  --variant A_serial_judge --output outputs/local/latency-serial-UNIQUE

uv run --no-sync python -m app.benchmark_latency suite --mode mock \
  --output outputs/local/latency-offline-UNIQUE --timeout 60
```

Health tests a minimal structured OpenAI response for each configured model, one Tavily search with raw content, and read access to the configured LangSmith project. Failed generation/search checks stop live experiments. The LangSmith check establishes project read access; successful trace upload/readback is verified after execution. Tracing follows LANGSMITH_TRACING and the configured personal project. Never put secrets in command arguments, Git or benchmark artifacts.

Exported environment variables override `.env`, just as in the existing application. When intentionally using an OPENAI_API_KEY newly placed in the project `.env`, run the command with `env -u OPENAI_API_KEY` so an inherited key cannot shadow it. Do not copy keys into chat.

## Measurements and bounds

- events.jsonl: chain/LLM start/end, stage ancestry, input character count, provider token usage, search durations and failures. No prompt text is written to this local timing log. LangSmith may contain normal model input/output when tracing is enabled.
- manifest.json: commit, dirty state, complete non-secret settings, factor choices and input hashes.
- summary.json: graph wall time, LLM calls/errors, stage call-time sums, actual stage visits, supplement round, fixes, verified claims, assessment/unknown/error counts, report checks and optional trace URL.
- state.json, nodes/, report.md and report.pdf: normal outputs for reviewing actual content, including unresolved items.
- comparison.json: suite progress saved after each experiment, including subprocess elapsed time and timeout/failure status.

LLM span sums overlap during parallel work and must not be mistaken for elapsed wall time. Graph wall time excludes initial model/index loading and final LangSmith flushing; process wall time includes preparation and preflight. Keep these measurements separate. The timed search adapter measures combined source calls, not individual underlying HTTP requests.

The suite enforces a per-process deadline and kills the process group on timeout, preserving already appended events and completed node artifacts. The call limit counts LangChain model invocation starts; SDK-internal retry requests are not separate callback starts. It is not a hard token or monetary cap. Existing per-request timeouts and provider retry settings are preserved in all variants. A failed/timed-out run stops the suite for inspection instead of triggering more expensive runs.

Unknown counts and automatic checks are quality signals, not a factual-quality score. Faster output that drops important evidence or claims is not automatically better. Mock runs prove wiring only: they do not execute live LLM concurrency, adaptive retrieval or the live supplement branch.

## Integration boundary

Only `graph/main_graph.py` gains `code_report=False`, allowing report assembly to be selected independently of first-pass search/fix/supplement limits. Default calls behave exactly as before. Experimental modules own the CLI, instrumentation and parallel Judge. Graph and report owners should review this additive switch and the unchanged NodeRun boundary before any production adoption; this experiment does not reassign team content responsibilities.
