from local_agent.config import Settings


def test_env_bool_parsing(monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_PLANNER_THINK", "false")
    assert Settings.from_env().planner_think is False
    monkeypatch.setenv("LOCAL_AGENT_PLANNER_THINK", "1")
    assert Settings.from_env().planner_think is True


def test_env_and_overrides(monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_THRESHOLD", "0.7")
    s = Settings.from_env(max_steps=5, planner_model=None)
    assert s.threshold == 0.7 and s.max_steps == 5 and s.planner_model == Settings().planner_model
