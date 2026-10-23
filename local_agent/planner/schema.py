"""The planner's output contract: the one next step Qwen wants performed."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ActionType = Literal["click", "double_click", "right_click", "type", "shortcut", "scroll", "terminal", "menu"]
# Actions whose target must be resolved to a concrete UI element by Jev.
ELEMENT_ACTIONS = {"click", "double_click", "right_click"}


class Target(BaseModel):
    id: int | None = Field(
        default=None, description="The element's [number] from the screen view, when it is listed there."
    )
    role: str = Field(
        default="any",
        description="AX role of the element to act on, e.g. AXButton, AXTextField, AXMenuItem, AXLink, AXCell. "
        "Use 'any' if unsure.",
    )
    description: str = Field(
        default="",
        description="What the element is, in plain words, e.g. 'Send button in the toolbar'. "
        "Required when id is not given.",
    )


class PlannerOutput(BaseModel):
    done: bool = Field(description="True only when the user's goal is fully accomplished.")
    reasoning: str = Field(description="One short sentence on why this is the next step.")
    action: ActionType | None = Field(default=None, description="Null when done is true.")
    target: Target | None = Field(
        default=None,
        description="Required for click actions. Optional for type (null = type into the focused element) and scroll.",
    )
    text: str | None = Field(default=None, description="Text to type. Required when action is 'type'.")
    keys: str | None = Field(default=None, description="Shortcut like 'cmd+n' or 'return'. Required for 'shortcut'.")
    direction: Literal["up", "down", "left", "right"] | None = Field(default=None, description="For 'scroll'.")
    command: str | None = Field(
        default=None, description="Shell command to run in the terminal (action 'terminal'), or the menu path from menu_commands, "
        "e.g. 'File > Save As…' (action 'menu'). Required for both."
    )

    @model_validator(mode="after")
    def _check_required_fields(self) -> "PlannerOutput":
        if self.done:
            return self
        if self.action is None:
            raise ValueError("action is required when done is false")
        if self.action == "terminal" and not (self.command and self.command.strip()):
            raise ValueError("action 'terminal' requires a non-empty 'command'")
        if self.action == "menu" and not (self.command and self.command.strip()):
            # Small models put the path where they put other values; take the first non-empty one.
            self.command = next((v for v in (self.target.description if self.target else "", self.text, self.keys)
                                 if v and ">" in v), None) or next(
                (v for v in (self.target.description if self.target else "", self.text, self.keys) if v), None)
        if self.action == "menu" and not (self.command and self.command.strip()):
            raise ValueError("action 'menu' requires 'command' (a path from menu_commands)")
        if self.action == "type" and not self.text:
            raise ValueError("action 'type' requires 'text'")
        if self.action == "shortcut" and not (self.keys and self.keys.strip()):
            raise ValueError("action 'shortcut' requires 'keys'")
        if self.action in ELEMENT_ACTIONS and self.target is None:
            raise ValueError(f"action '{self.action}' requires 'target'")
        if self.target is not None and self.target.id is None and not self.target.description.strip():
            raise ValueError("target needs an 'id' from the screen view or a 'description'")
        if self.action == "scroll" and self.direction is None:
            self.direction = "down"
        return self

    def needs_jev(self) -> bool:
        if self.done or self.action is None:
            return False
        if self.action in ELEMENT_ACTIONS:
            return True
        return self.action in ("type", "scroll") and self.target is not None

    def summary(self) -> str:
        if self.done:
            return "done"
        bits = [self.action or ""]
        if self.target:
            if self.target.id is not None:
                bits.append(f"[{self.target.id}]")
            if self.target.description or self.target.id is None:
                bits.append(f"[{self.target.role}] {self.target.description}")
        if self.text:
            bits.append(f"text={self.text!r}")
        if self.keys:
            bits.append(f"keys={self.keys}")
        if self.direction and self.action == "scroll":
            bits.append(self.direction)
        if self.command:
            bits.append(f"{'menu' if self.action == 'menu' else 'command'}={self.command!r}")
        return " ".join(bits)
