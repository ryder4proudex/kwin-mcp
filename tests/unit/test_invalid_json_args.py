"""An object argument written as invalid JSON gets a remedy line after the validation error.

Clients that pass objects as strings (shell wrappers, ``key=value`` CLIs) often
send relaxed JSON such as ``{app_id:dolphin}``. The MCP layer parses valid JSON
strings itself; invalid ones used to reach pydantic and come back only as
"Input should be a valid dictionary", which does not say the JSON was the problem.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kwin_mcp.server import KwinServer, ServerFlags

if TYPE_CHECKING:
    from ._fakes import FakeEngine


def _error(fake_engine: FakeEngine, tool: str, arguments: dict[str, object]) -> str:
    server = KwinServer(fake_engine.engine, ServerFlags())
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(server.call_tool(tool, arguments))
    return str(exc_info.value)


@pytest.mark.qa("QA-GATE-010")
def test_relaxed_json_object_gets_a_json_remedy(fake_engine: FakeEngine) -> None:
    text = _error(fake_engine, "window_focus", {"window": "{app_id:dolphin}"})

    lines = text.splitlines()
    assert (
        lines[0]
        == "Error executing tool window_focus: 1 validation error for window_focusArguments"
    )
    assert lines[-1] == (
        "Argument 'window' is not valid JSON (Expecting property name enclosed in double "
        'quotes at column 2); write objects as strict JSON, e.g. {"key": "value"}.'
    )
    assert fake_engine.eis.events == []


@pytest.mark.qa("QA-GATE-010")
def test_other_validation_errors_get_no_json_remedy(fake_engine: FakeEngine) -> None:
    text = _error(fake_engine, "window_focus", {"window": {"kind": []}})

    assert "validation error" in text
    assert "not valid JSON" not in text


@pytest.mark.qa("QA-GATE-010")
def test_string_arguments_that_start_like_json_get_no_json_remedy(
    fake_engine: FakeEngine,
) -> None:
    text = _error(fake_engine, "keyboard_type", {"text": "{hello", "method": "invalid"})

    assert "validation error" in text
    assert "not valid JSON" not in text
