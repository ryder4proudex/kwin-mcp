"""Echo of caller text in keyboard_type results (F96).

The engine runs over the recording fake EIS client, and the unicode route is
replaced by a stub that reports success, so only the result is observed. The
text is echoed only with ``echo=True`` (typed text can be a password): up to
200 characters as its repr, longer text as its length only. Without it the
result reports the length alone.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

import kwin_mcp.input
from kwin_mcp.core import AutomationEngine
from kwin_mcp.input import InputBackend
from kwin_mcp.models import TypeRoute

if TYPE_CHECKING:
    from kwin_mcp.models import TypeMethod

    from ._fakes import FakeEngine

_METHODS: list[tuple[TypeMethod, str, str]] = [
    ("keys", "a", "Typed: "),
    ("unicode", "é", "Typed unicode: "),
]


def _typed_through(monkeypatch: pytest.MonkeyPatch, fake_engine: FakeEngine) -> list[str]:
    """Record what reaches each typing route; the key route still sends real fake events."""
    typed: list[str] = []
    original = InputBackend.keyboard_type

    def keys(self: InputBackend, text: str) -> None:
        typed.append(text)
        original(self, text)

    def unicode(self: InputBackend, text: str, *_args: object, **_kwargs: object) -> TypeRoute:
        typed.append(text)
        return TypeRoute(kind="wtype")

    monkeypatch.setattr(kwin_mcp.input.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(InputBackend, "keyboard_type", keys)
    monkeypatch.setattr(InputBackend, "keyboard_type_unicode", unicode)
    monkeypatch.setattr(fake_engine.engine, "_session_env", dict)
    return typed


@pytest.mark.parametrize(("method", "char", "prefix"), _METHODS)
def test_text_at_the_limit_is_echoed_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeEngine,
    method: TypeMethod,
    char: str,
    prefix: str,
) -> None:
    typed = _typed_through(monkeypatch, fake_engine)
    text = char * 200

    result = fake_engine.engine.keyboard_type(text, method=method, echo=True)

    assert result.echo == repr(text)
    assert result.chars == 200
    assert result.render().splitlines()[0] == f"{prefix}{text!r}"
    assert typed == [text]


@pytest.mark.parametrize(("method", "char", "prefix"), _METHODS)
def test_text_beyond_the_limit_is_echoed_as_its_length(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeEngine,
    method: TypeMethod,
    char: str,
    prefix: str,
) -> None:
    typed = _typed_through(monkeypatch, fake_engine)
    text = char * 201

    result = fake_engine.engine.keyboard_type(text, method=method, echo=True)

    assert result.echo == "<201 characters>"
    assert result.chars == 201
    assert result.render().splitlines()[0] == f"{prefix}<201 characters>"
    assert typed == [text]


@pytest.mark.parametrize(
    ("method", "char", "first_line"),
    [
        ("keys", "a", "Typed: 12 characters (not echoed)."),
        ("unicode", "é", "Typed unicode: 12 characters (not echoed)."),
    ],
)
def test_text_is_not_echoed_by_default(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeEngine,
    method: TypeMethod,
    char: str,
    first_line: str,
) -> None:
    typed = _typed_through(monkeypatch, fake_engine)
    secret = char * 12

    result = fake_engine.engine.keyboard_type(secret, method=method)

    assert result.echo is None
    assert result.chars == 12
    text = result.render()
    assert text.splitlines()[0] == first_line
    assert secret not in text
    assert secret not in result.model_dump_json()
    assert typed == [secret]


def test_one_character_is_singular(
    monkeypatch: pytest.MonkeyPatch, fake_engine: FakeEngine
) -> None:
    _typed_through(monkeypatch, fake_engine)
    assert fake_engine.engine.keyboard_type("x").render().splitlines()[0] == (
        "Typed: 1 character (not echoed)."
    )


def test_failed_unicode_typing_does_not_echo_the_text(
    monkeypatch: pytest.MonkeyPatch, fake_engine: FakeEngine
) -> None:
    _typed_through(monkeypatch, fake_engine)
    monkeypatch.setattr(InputBackend, "keyboard_type_unicode", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError) as excinfo:
        fake_engine.engine.keyboard_type("hunter2-secret", method="unicode")

    assert "hunter2" not in str(excinfo.value)
    assert str(excinfo.value).startswith("Failed to type unicode text (14 characters)")


def test_keyboard_type_rejects_unsupported_characters_without_a_session() -> None:
    """The key-map check runs before any session or input backend is looked up."""
    with pytest.raises(ValueError, match="'é', '€'") as excinfo:
        AutomationEngine().keyboard_type("héllo€!", echo=True)
    assert str(excinfo.value) == (
        "keyboard_type cannot type 'é', '€'.\nRetry with method=\"unicode\"."
    )


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("pässword", "1 character at position 2"),
        ("é€ab€", "3 characters at positions 1, 2, 5"),
        ("é" * 12, "12 characters at positions 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, ..."),
    ],
)
def test_rejected_characters_are_not_echoed_by_default(text: str, where: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        AutomationEngine().keyboard_type(text)
    message = str(excinfo.value)
    assert message == (
        f"keyboard_type cannot type {where} (not echoed; echo=true names them).\n"
        'Retry with method="unicode".'
    )
    assert "é" not in message
    assert "€" not in message


def test_hung_wtype_does_not_echo_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kwin_mcp.input.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _hang(args: list[str], **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(args, 5)

    monkeypatch.setattr(kwin_mcp.input.subprocess, "run", _hang)
    backend = InputBackend.__new__(InputBackend)

    with pytest.raises(RuntimeError) as excinfo:
        backend.keyboard_type_unicode("hunter2-sëcret", env={}, focused_class="x")

    message = str(excinfo.value)
    assert "hunter2" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert message == (
        "wtype did not finish within 5 s; some of the 14 characters may be typed.\n"
        "Check the focused field before typing again."
    )
