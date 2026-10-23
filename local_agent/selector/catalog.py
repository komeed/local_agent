"""Jev models the agent can run, largest first: the published JevK5 GGUFs (huggingface.co/alibiserikbay/JevK5-GGUF),
then the cosine-similarity System 1 (selector/cosine.py) on an Ollama embedding model.

RAM is what the model holds while serving: the file, plus (JevK5) llama-server's 8k-token KV cache and compute
buffers with `--parallel 1 --cache-ram 0`. Only the 4B Q8_0 figure was measured here (it agrees with the ~5 GB in the
README); the others are scaled from the file sizes. Temperatures and JevBench scores come from the JevK5-GGUF model
card; each file needs its own temperatures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

import httpx

from local_agent.config import Settings

HF_REPO = "alibiserikbay/JevK5-GGUF"
MODELS_DIR = Path(os.environ.get("LOCAL_AGENT_MODELS_DIR", "~/models/jevk5")).expanduser()


@dataclass(frozen=True)
class JevModel:
    key: str
    backend: str  # "jevk5" (llama-server) or "cosine" (Ollama embeddings)
    file: str  # GGUF file in HF_REPO, or the Ollama embedding model
    size_gb: float
    ram_gb: float
    note: str  # JevBench standard / hard tier accuracy (model card), or what the rung trades away
    temperature: float = 0.0  # JevK5: per-pass calibration; cosine: softmax temperature over similarities
    knockout_temperature: float = 0.0
    lexical_weight: float = 0.0  # cosine only: weight of exact word overlap (evals/selector_eval.py fit)


CATALOG: list[JevModel] = [
    JevModel("jevk5-9b-q8", "jevk5", "jevk5-9b-v0.3.3-Q8_0.gguf", 9.53, 10.5,
             "JevBench 0.958 / 0.766", 1.316, 1.05),
    JevModel("jevk5-9b-q5", "jevk5", "jevk5-9b-v0.3.3-Q5_K_M.gguf", 6.47, 7.4, "JevBench 0.958 / 0.757", 1.316, 1.05),
    JevModel("jevk5-4b-q8", "jevk5", "jevk5-4b-v0.3-Q8_0.gguf", 4.48, 5.1, "JevBench 0.944 / 0.784 (default)", 1.22,
             0.93),
    JevModel("jevk5-4b-q5", "jevk5", "jevk5-4b-v0.3-Q5_K_M.gguf", 3.07, 3.7, "JevBench 0.944 / 0.784", 1.22, 0.93),
    JevModel("jevk5-4b-q4", "jevk5", "jevk5-4b-v0.3-Q4_K_M.gguf", 2.71, 3.3, "JevBench 0.917 / 0.766", 1.22, 0.93),
    JevModel("jevk5-2b-q8", "jevk5", "jevk5-2b-v0.2-Q8_0.gguf", 2.01, 2.4, "JevBench 0.792 / 0.622", 1.42, 0.77),
    JevModel("cosine-qwen3", "cosine", "qwen3-embedding:0.6b", 0.64, 0.7,
             "embeddings: picks only, no reasoning; 9/10 on selector evals", 0.01, lexical_weight=0.5),
    JevModel("cosine-nomic", "cosine", "nomic-embed-text", 0.27, 0.3,
             "embeddings: picks only, no reasoning; 10/10 on selector evals", 0.01, lexical_weight=0.3),
]  # fmt: skip
BY_KEY = {m.key: m for m in CATALOG}
BY_FILE = {m.file: m for m in CATALOG}
DEFAULT_KEY = "jevk5-4b-q8"


def get(key: str) -> JevModel:
    try:
        return BY_KEY[key]
    except KeyError:
        raise ValueError(f"unknown Jev model {key!r}; choose one of: {', '.join(BY_KEY)}") from None


def apply(settings: Settings, model: JevModel) -> Settings:
    """Settings that run `model`: backend, file and its calibration."""
    if model.backend == "cosine":
        return replace(settings, jev=model.key, jev_backend="cosine", embed_model=model.file,
                       cosine_temperature=model.temperature, cosine_lexical_weight=model.lexical_weight)
    return replace(settings, jev=model.key, jev_backend="jevk5", jevk5_file=model.file,
                   jevk5_temperature=model.temperature, jevk5_knockout_temperature=model.knockout_temperature)  # fmt: skip


def served_file(jevk5_url: str, timeout: float = 2.0) -> str | None:
    """The GGUF file llama-server has loaded (from /props), or None when it isn't reachable."""
    try:
        path = httpx.get(f"{jevk5_url}/props", timeout=timeout).json().get("model_path") or ""
    except (httpx.HTTPError, ValueError):
        return None
    return Path(path).name or None


def resolve(settings: Settings) -> Settings:
    """Apply `settings.jev` when set. Otherwise, for JevK5, use the calibration of whichever catalog file llama-server
    is serving, so starting it with another file doesn't silently run with the 4B's temperatures."""
    if settings.jev:
        return apply(settings, get(settings.jev))
    if settings.jev_backend == "jevk5":
        model = BY_FILE.get(served_file(settings.jevk5_url) or "")
        if model is not None:
            return apply(settings, model)
    return settings


def mismatch(settings: Settings) -> str | None:
    """Why the running llama-server can't serve the chosen JevK5 file, or None."""
    if settings.jev_backend != "jevk5":
        return None
    served = served_file(settings.jevk5_url)
    if served and served != settings.jevk5_file:
        return f"llama-server is serving {served}, not {settings.jevk5_file}"
    return None


def local_path(model: JevModel) -> Path:
    return MODELS_DIR / model.file


def downloaded(model: JevModel, ollama_models: set[str] | None = None) -> bool:
    if model.backend == "cosine":
        names = ollama_models or set()
        return model.file in names or f"{model.file}:latest" in names
    return local_path(model).exists()


def serve_command(model: JevModel, jevk5_url: str) -> list[str]:
    """How to start `model`: llama-server on the local file (or straight from Hugging Face), or an Ollama pull."""
    if model.backend == "cosine":
        return ["ollama", "pull", model.file]
    port = str(urlparse(jevk5_url).port or 8091)
    source = (["-m", str(local_path(model))] if local_path(model).exists()
              else ["--hf-repo", HF_REPO, "--hf-file", model.file])  # fmt: skip
    return ["llama-server", *source, "-c", "8192", "-ngl", "99", "--parallel", "1", "--cache-ram", "0", "--port", port]
