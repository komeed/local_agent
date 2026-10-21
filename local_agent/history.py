"""Per-step execution log that is fed back to the planner."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class StepRecord:
    step: int
    plan: str  # PlannerOutput.summary()
    element: str | None = None  # chosen element description, if Jev was used
    probability: float | None = None  # Jev probability of the chosen option
    confidence: float | None = None  # Jev confidence (1 - normalized entropy)
    executed: bool = False
    verified: bool | None = None  # None when no verification ran
    verify_probability: float | None = None
    note: str = ""  # error / skip reason / terminal output tail

    @property
    def succeeded(self) -> bool:
        return self.executed and self.verified is not False

    def for_planner(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v not in (None, "")}
        for k in ("probability", "confidence", "verify_probability"):
            if k in d:
                d[k] = round(d[k], 3)
        d["result"] = "success" if self.succeeded else "failed"
        return d
