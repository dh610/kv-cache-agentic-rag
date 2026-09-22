"""Apply only human-reviewed URL annotations; missing metadata remains unknown."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from runtime.settings import ROOT


class SourceAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    affiliation: Literal["first_party", "independent", "unknown"]
    affiliation_reason: str = Field(min_length=1)
    stance: Literal["positive", "critical", "mixed", "neutral", "unknown"]
    reviewed_by: str = Field(min_length=1)
    published_at: str | None = None
    publisher: str | None = None


def annotate(evidence, path: Path | None = None):
    path = path or ROOT / "data/source_annotations.yaml"
    if not path.exists():
        return evidence
    raw = yaml.safe_load(path.read_text()) or {}
    # Per technology: the same organization's relationship can differ by target.
    entry = raw.get(evidence.technology, {}).get(evidence.url)
    if entry:
        annotation = SourceAnnotation.model_validate(entry)
        updates = annotation.model_dump(exclude={"reviewed_by"}, exclude_none=True)
        return evidence.model_copy(update=updates)
    return evidence
