import pytest

from local_agent.config import Settings
from local_agent.selector import catalog


def test_ladder_is_largest_first_and_jevk5_on_top():
    rams = [m.ram_gb for m in catalog.CATALOG]
    assert rams == sorted(rams, reverse=True)
    assert catalog.CATALOG[0].backend == "jevk5" and catalog.CATALOG[-1].backend == "cosine"
    assert catalog.DEFAULT_KEY in catalog.BY_KEY


def test_apply_sets_file_and_its_temperatures():
    s = catalog.apply(Settings(), catalog.get("jevk5-9b-q8"))
    assert s.jev_backend == "jevk5" and s.jevk5_file == "jevk5-9b-v0.3.3-Q8_0.gguf"
    assert (s.jevk5_temperature, s.jevk5_knockout_temperature) == (1.316, 1.05)
    c = catalog.apply(Settings(), catalog.get("cosine-nomic"))
    assert c.jev_backend == "cosine" and c.embed_model == "nomic-embed-text"
    with pytest.raises(ValueError, match="unknown Jev model"):
        catalog.get("nope")


def test_resolve_follows_served_file(monkeypatch):
    monkeypatch.setattr(catalog, "served_file", lambda url, timeout=2.0: "jevk5-2b-v0.2-Q8_0.gguf")
    s = catalog.resolve(Settings())
    assert s.jev == "jevk5-2b-q8" and s.jevk5_temperature == 1.42
    assert catalog.mismatch(catalog.resolve(Settings(jev="jevk5-4b-q8"))) is not None
    monkeypatch.setattr(catalog, "served_file", lambda url, timeout=2.0: None)
    assert catalog.resolve(Settings()) == Settings()  # unreachable server: settings as given


def test_serve_command(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "MODELS_DIR", tmp_path)
    m = catalog.get("jevk5-4b-q5")
    cmd = catalog.serve_command(m, "http://127.0.0.1:9000")
    assert cmd[:5] == ["llama-server", "--hf-repo", catalog.HF_REPO, "--hf-file", m.file] and cmd[-1] == "9000"
    (tmp_path / m.file).write_bytes(b"")
    assert catalog.serve_command(m, "http://127.0.0.1:9000")[1:3] == ["-m", str(tmp_path / m.file)]
    assert catalog.serve_command(catalog.get("cosine-qwen3"), "") == ["ollama", "pull", "qwen3-embedding:0.6b"]
