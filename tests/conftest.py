import json
from pathlib import Path

import pytest

from local_agent.observe.models import Frame, Snapshot, UIElement

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def textedit() -> Snapshot:
    return Snapshot.from_dict(json.loads((FIXTURES / "textedit.json").read_text()))


@pytest.fixture
def big_snapshot() -> Snapshot:
    """600 buttons, enough to force a multi-chunk Jev tournament."""
    els = [
        UIElement(id=i, role="AXButton", title=f"Button {i}", frame=Frame(i % 40 * 20, i // 40 * 20, 18, 18))
        for i in range(600)
    ]
    return Snapshot(app="Big", bundle_id="x.big", pid=1, window_title="Many", elements=els)
