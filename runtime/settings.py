from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Models(SettingsModel):
    generator: str
    judge: str
    temperature: float
    timeout_seconds: int = Field(ge=1, le=300)
    max_retries: int = Field(ge=0, le=3)


class Limits(SettingsModel):
    search: int = Field(ge=1, le=3)
    questions: int = Field(ge=1, le=12)
    fix: int = Field(default=1, ge=0, le=1)
    # 사용자 승인: 설계서 표 13의 보완 1라운드가 기본. 0으로 끌 수 있다.
    supplement: int = Field(default=1, ge=0, le=1)


class Retrieval(SettingsModel):
    model: str
    revision: str
    chunk_size: int = Field(ge=100)
    chunk_overlap: int = Field(ge=0)
    top_k: int = Field(ge=1, le=20)
    rerank: bool
    reranker: str
    candidates: int = Field(ge=1, le=100)
    index_dir: str

    @model_validator(mode="after")
    def valid_window(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if self.candidates < self.top_k:
            raise ValueError("candidates must be >= top_k")
        return self


class Evaluation(SettingsModel):
    samples: int = Field(default=30, ge=2)
    per_technology: int = Field(default=15, ge=1)
    language: Literal["ko"] = "ko"
    acronym_numeric_fraction: float = Field(default=1 / 3, ge=0, le=1)
    tie_mrr: float = Field(default=0.05, ge=0)
    min_hit5: float = Field(default=0.80, ge=0, le=1)
    min_mrr: float = Field(default=0.60, ge=0, le=1)
    candidates: list[str] = Field(
        default_factory=lambda: [
            "BAAI/bge-m3",
            "intfloat/multilingual-e5-large",
            "Alibaba-NLP/gte-multilingual-base",
        ]
    )
    remediation: list[str] = Field(
        default_factory=lambda: ["chunking", "bilingual", "dense_sparse", "rerank"]
    )

    @model_validator(mode="after")
    def registered_protocol(self):
        if self.samples != 2 * self.per_technology:
            raise ValueError("Evaluation samples must equal two technologies times per_technology")
        if len(self.candidates) != 3 or len(set(self.candidates)) != 3:
            raise ValueError("Evaluation requires three distinct candidate models")
        if self.remediation != ["chunking", "bilingual", "dense_sparse", "rerank"]:
            raise ValueError("Keep the registered remediation order")
        return self


class GapPolicy(SettingsModel):
    unknown_fraction: float = Field(default=0.5, gt=0, le=1)
    critical_criteria: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "market": ["adoption"],
            "domain": ["quality", "cost"],
            "stakeholder": ["competitors"],
        }
    )


class Settings(SettingsModel):
    schema_version: Literal[2]
    target_techs: dict[str, str]
    domain: str
    models: Models
    limits: Limits
    retrieval: Retrieval
    evaluation: Evaluation = Field(default_factory=Evaluation)
    gap_policy: GapPolicy = Field(default_factory=GapPolicy)


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env", override=False)
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    for env, key in (("GENERATOR_MODEL", "generator"), ("JUDGE_MODEL", "judge")):
        if os.getenv(env):
            raw["models"][key] = os.environ[env]
    return Settings.model_validate(raw)


def require_key(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required; set it in your local .env (see .env.example).")
    return value
