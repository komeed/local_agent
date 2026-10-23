"""Runtime settings. Every field can be overridden with a LOCAL_AGENT_<NAME> env var or a CLI flag."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

# OpenJev rejects choice questions with more than 255 options.
JEV_MAX_CHOICES = 255


def _parse(raw: str, kind: type):
    if kind is bool:  # bool("false") is True
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return kind(raw)


@dataclass
class Settings:
    ollama_url: str = "http://127.0.0.1:11434"
    planner_model: str = "qwen3.5:9b"  # won evals/planner_eval.py: 94% vs 82% (qwen2.5-coder:7b), 71% (qwen3.5:4b)
    # Qwen3.5-style "thinking" adds seconds per step; plans are short, so it's off by default.
    planner_think: bool = False
    # "json" (one free-form step) | "choice" (kind, then details; for small models) | "kind" (Jev picks the kind,
    # the planner the details, with the screen view only for kinds that need it; planner/kind.py)
    planner_mode: str = "json"
    # Planner prompts are ~1k tokens; Ollama otherwise reserves KV cache for the model's full context
    # (262k for Qwen3.5), which more than doubles its memory.
    planner_num_ctx: int = 8192
    # Text view of the screen for the planner (observe/screen_view.py): "ids" numbers each control so the planner can
    # answer with target.id; "names" lists controls without numbers (Jev picks every element); "off" sends only
    # Snapshot.scope(). Capped at planner_view_tokens and to what fits in planner_num_ctx.
    planner_view: str = "ids"
    planner_view_tokens: int = 3000
    # A model from selector/catalog.py (`local-agent jev list`), e.g. "jevk5-9b-q8" or "cosine-nomic"; sets
    # jev_backend, the file and its temperatures. Empty: use the fields below, with JevK5's temperatures taken from
    # whichever catalog file llama-server is serving.
    jev: str = ""
    # "jevk5": JevK5 GGUF read through llama-server (default, 4B Q8_0 ~5 GB). "openjev": an OpenJev /v1/systemone
    # server. "cosine": embedding similarity through Ollama (selector/cosine.py).
    jev_backend: str = "jevk5"
    jevk5_url: str = "http://127.0.0.1:8091"
    jevk5_file: str = "jevk5-4b-v0.3-Q8_0.gguf"
    # Calibration for jevk5-4b-v0.3 GGUFs, from the JevK5-GGUF model card; other files have their own values.
    jevk5_temperature: float = 1.22
    jevk5_knockout_temperature: float = 0.93
    # Cosine backend: Ollama embedding model and the softmax temperature over cosine similarities (they cluster
    # within a few hundredths, so it's small). lexical_weight blends in exact word overlap with the target, which
    # embeddings blur (invoice-0425 vs invoice-0424): without it qwen3-embedding picked 5/10 selector cases right.
    # Fit on evals/selector_eval.py for nomic-embed-text; the catalog holds each model's values.
    embed_model: str = "nomic-embed-text"
    cosine_temperature: float = 0.01
    cosine_lexical_weight: float = 0.3
    jev_url: str = "http://127.0.0.1:8080"
    jev_model: str = "openjev-latest"
    jev_api_key: str = ""
    # Options per Jev choice question; clamped to JEV_MAX_CHOICES. Lower it if OpenJev runs out of memory.
    jev_chunk_size: int = JEV_MAX_CHOICES
    # Keep only this many candidates whose text best matches the planner's target before asking Jev
    # (0 disables). JevK5 reads 16 options per pass and is most accurate on short lists.
    prefilter_top_k: int = 32
    # Candidates tied with the top_k-th are kept too (a list where every row has "Delete"), up to this many.
    prefilter_max_k: int = 128
    # Minimum Jev probability for the chosen element before we act on it.
    threshold: float = 0.5
    # Minimum Jev probability that the UI changed as intended for a step to count as verified.
    verify_threshold: float = 0.5
    # Terminal commands run without asking when Jev rates them at least this safe (and they don't match
    # safety.ALWAYS_ASK); otherwise the user is asked. turbo runs every command with no check at all.
    safety_threshold: float = 0.8
    turbo: bool = False
    max_steps: int = 20
    max_consecutive_failures: int = 3
    # After each successful step Jev judges whether the screen now shows the whole goal done (JevClient.goal_done):
    # at or above goal_done_threshold the run ends as a success; at or above goal_hint_threshold the planner is told
    # the goal may be done. Tuned on evals/goal_eval.py; a false stop is worse than one extra step, so done is high.
    # Menu commands as a planner action (observe/menus.py). Off: the 3B scored lower with it in every mode (README).
    menu_actions: bool = False
    # Read the page of a front browser through its JavaScript (observe/web.py); AX-only when it isn't available.
    web_reading: bool = True
    web_max_elements: int = 400
    # Seconds before a browser AppleScript call gives up: reads (status, tabs, page outline) and actions (open, select, click).
    # Plan once (a numbered outline from the planner model) for goals the facts can't cover; replan when stuck.
    outline: bool = True
    # Small Mac: models other than JevK5 are unloaded after each use (outline calls send keep_alive 0).
    low_memory: bool = False
    # Which model writes the outline: "planner" (the Ollama planner model) or "jev" (the JevK5 llama-server, so a
    # small Mac keeps one model). None: "jev" when planner_mode is "jev" and low_memory is on, else "planner".
    outline_model: str | None = None
    browser_timeout_s: float = 3.0
    browser_action_timeout_s: float = 5.0
    goal_check: bool = True
    goal_done_threshold: float = 0.9
    goal_hint_threshold: float = 0.5
    goal_view_tokens: int = 2500  # screen view Jev reads for the check (its context is 8k)
    # Stop when the same step (or block of up to 3 steps) succeeds this many times in a row: completed work being
    # redone. The planner is warned one repeat earlier.
    max_repeated_successes: int = 3
    # Jev mode: when Jev's best option has probability below this, isn't the checklist's own next line and isn't `done`,
    # nothing is done (the step fails with an "unsure" note); the second such step in a row ends the run. 0 turns it off.
    jev_min_pick_p: float = 0.3
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
                kind = type(getattr(s, f.name))
                if kind is type(None):  # an optional field: parse as the type it names
                    kind = bool if "bool" in str(f.type) else str
                setattr(s, f.name, _parse(raw, kind))
        for k, v in overrides.items():
            if v is not None:
                setattr(s, k, v)
        return s

    def menus_on(self) -> bool:
        return self.menu_actions
