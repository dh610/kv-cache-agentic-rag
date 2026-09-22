from __future__ import annotations

import hashlib
import json

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.settings import ROOT
from schemas.contracts import Evidence, NodeInput, NodeName


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    definition: str
    judgments: list[str]


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: NodeName
    objective: str
    criteria: list[Criterion] = Field(min_length=1)
    source_policy: str

    @model_validator(mode="after")
    def valid_criteria(self):
        ids = [c.id for c in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate rubric criterion IDs")
        if any("확인 불가" not in c.judgments for c in self.criteria):
            raise ValueError("Every criterion must allow 확인 불가")
        return self


def load_rubric(node: NodeName) -> Rubric:
    result = Rubric.model_validate(
        yaml.safe_load((ROOT / "rubrics" / f"{node}.yaml").read_text(encoding="utf-8"))
    )
    if result.role != node:
        raise ValueError(f"Rubric role mismatch: {node}")
    return result


def render(node: NodeName, data: NodeInput, evidence: list[Evidence]) -> tuple[str, str, str]:
    env = Environment(
        loader=FileSystemLoader(ROOT / "prompts"),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    context = {
        "node": node,
        "rubric": load_rubric(node).model_dump(),
        # Live retrieval must never see the fixture's original evidence as another source.
        "input_json": data.model_dump_json(indent=2, exclude={"evidence"}),
        "evidence_json": json.dumps(
            [e.model_dump() for e in evidence], ensure_ascii=False, indent=2
        ),
    }
    system = env.get_template(f"{node}/system.j2").render(**context)
    user = env.get_template(f"{node}/user.j2").render(**context)
    digest = hashlib.sha256((system + "\n" + user).encode()).hexdigest()
    return system, user, digest
