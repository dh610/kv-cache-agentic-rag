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
    fix: Literal[0] = 0
    supplement: Literal[0] = 0


# Only the simplified execution contract is implemented in this foundation.


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


class Settings(SettingsModel):
    schema_version: Literal[1]
    target_techs: dict[str, str]
    domain: str
    models: Models
    limits: Limits
    retrieval: Retrieval


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
