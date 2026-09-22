"""Prompt views preserve source identity; the full evidence stays in NodeRun."""

import re


def evidence_payload(evidence, questions=(), *, full_text=False):
    """Extract query-relevant web passages, never model-written evidence summaries.

    Paper chunks and citation verification keep their complete text. Long web pages
    expose original character offsets so excerpts can be audited against the archive.
    """
    result = []
    for item in evidence:
        row = item.model_dump(exclude_none=True)
        if not full_text and item.source_type == "web" and len(item.text) > 4800:
            query = " ".join(
                f"{q.criterion} {q.text}"
                for q in questions
                if item.technology in (q.technology, "other")
            )
            words = set(re.findall(r"[\w-]{3,}", query.lower()))
            spans = [
                (start, min(start + 1800, len(item.text)))
                for start in range(0, len(item.text), 1500)
            ]
            ranked = sorted(
                spans,
                key=lambda span: (
                    -sum(min(item.text[span[0] : span[1]].lower().count(word), 3) for word in words)
                ),
            )
            # Preserve two contextual windows and a window with possible counter-evidence.
            chosen = set(ranked[:2])
            contrary = ("limitation", "however", "although", "drawback", "한계", "하지만")
            counter = next(
                (
                    span
                    for span in ranked
                    if any(word in item.text[span[0] : span[1]].lower() for word in contrary)
                ),
                None,
            )
            if counter:
                chosen.add(counter)
            ranges = []
            for start, end in sorted(chosen):
                if ranges and start <= ranges[-1][1]:
                    ranges[-1][1] = max(ranges[-1][1], end)
                else:
                    ranges.append([start, end])
            row["text"] = "\n[…원문 중간 생략…]\n".join(
                item.text[start:end] for start, end in ranges
            )
            row["excerpt_ranges"] = ranges
            row["original_characters"] = len(item.text)
            row["excerpt_notice"] = "관련 원문 발췌. 생략 부분의 사실·부재를 추론하지 마세요."
        result.append(row)
    return result
