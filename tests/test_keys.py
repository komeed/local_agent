import pytest
from Quartz import kCGEventFlagMaskCommand, kCGEventFlagMaskShift

from local_agent.act.keys import parse_shortcut


def test_parse():
    assert parse_shortcut("return") == (36, 0)
    assert parse_shortcut("cmd+shift+T") == (17, kCGEventFlagMaskCommand | kCGEventFlagMaskShift)


@pytest.mark.parametrize("bad", ["", "cmd+", "hyper+a", "cmd+nosuchkey"])
def test_rejects(bad):
    with pytest.raises(ValueError):
        parse_shortcut(bad)
