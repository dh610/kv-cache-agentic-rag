# Shared project entry point

Read AGENTS.md, docs/team-contract.md and docs/contracts.md before modifying this repository.
Use the current GitHub implementation, not the earlier starter ZIP's file layout.
Role prompts live in prompts/<node>/*.j2, rubrics in rubrics/<node>.yaml,
and reproducible cases in tests/fixtures/<node>.
The common runtime owns question iteration, evidence merging, verification and status.
The final design's remaining differences are tracked in docs/final-design-review.md.
Do not treat mock output or an unmeasured retrieval score as a completed project result.
