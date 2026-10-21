"""Runtime settings. Every field can be overridden with a LOCAL_AGENT_<NAME> env var or a CLI flag."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

# OpenJev rejects choice questions with more than 255 options.
JEV_MAX_CHOICES = 255


@dataclass
class Settings:
    ollama_url: str = "http://127.0.0.1:11434"
    planner_model: str = "qwen2.5-coder:7b"
    # "jevk5": JevK5 4B GGUF read through llama-server (default, ~5 GB). "openjev": an OpenJev /v1/systemone server.
    jev_backend: str = "jevk5"
    jevk5_url: str = "http://127.0.0.1:8091"
    # Calibration for jevk5-4b-v0.3 GGUFs, from the JevK5-GGUF model card; other files have their own values.
    jevk5_temperature: float = 1.22
    jevk5_knockout_temperature: float = 0.93
    jev_url: str = "http://127.0.0.1:8080"
    jev_model: str = "openjev-latest"
    jev_api_key: str = ""
    # Options per Jev choice question; clamped to JEV_MAX_CHOICES. Lower it if OpenJev runs out of memory.
    jev_chunk_size: int = JEV_MAX_CHOICES
    # Keep only this many candidates whose text best matches the planner's target before asking Jev
    # (0 disables). JevK5 reads 16 options per pass and is most accurate on short lists.
    prefilter_top_k: int = 32
    # Minimum Jev probability for the chosen element before we act on it.
    threshold: float = 0.5
    # Minimum Jev probability that the UI changed as intended for a step to count as verified.
    verify_threshold: float = 0.5
    max_steps: int = 20
    max_consecutive_failures: int = 3
    # AX tree walk limits.
    max_depth: int = 40
    max_elements: int = 3000
    walk_timeout_s: float = 5.0
    # Seconds to wait after an action before re-observing.
    settle_s: float = 0.8
    http_timeout_s: float = 120.0

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        s = cls()
        for f in fields(cls):
            raw = os.environ.get(f"LOCAL_AGENT_{f.name.upper()}")
            if raw is not None:
                setattr(s, f.name, type(getattr(s, f.name))(raw))
        for k, v in overrides.items():
            if v is not None:
                setattr(s, k, v)
        return s
