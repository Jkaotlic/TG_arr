"""Every callback handler must acknowledge its callback.

Telegram spins the button until `answerCallbackQuery` arrives, then shows the
user a timeout. A handler that forgets `callback.answer()` looks like the bot
hung — and it is the kind of omission that only shows up by tapping the button,
which no unit test does.

This walks the handler modules with `ast` instead of testing behaviour: the
point is coverage over *all* handlers, including ones nobody wrote a test for.
"""

import ast
from pathlib import Path

HANDLERS = Path(__file__).resolve().parent.parent / "bot" / "handlers"

#: Handlers that legitimately never ack, because they hand the event to another
#: handler that owns the single ack.
DELEGATES = {
    "handle_confirm_grab",   # dispatches to handle_confirm_music_add for music
}


def _is_callback_handler(node: ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        text = ast.dump(dec)
        if "callback_query" in text:
            return True
    return False


def _acks(node: ast.AST) -> bool:
    """Whether this function body ever calls something.answer(...) or delegates."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute) and func.attr == "answer":
            return True
        # `await handle_x(callback, ...)` — the callee owns the ack.
        if isinstance(func, ast.Name) and func.id.startswith("handle_"):
            return True
        if isinstance(func, ast.Attribute) and func.attr.startswith("handle_"):
            return True
    return False


def _callback_handlers():
    for path in sorted(HANDLERS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and _is_callback_handler(node):
                yield path, node


def test_every_callback_handler_acknowledges():
    missing = [
        f"{path.name}:{node.lineno} {node.name}"
        for path, node in _callback_handlers()
        if node.name not in DELEGATES and not _acks(node)
    ]

    assert not missing, "callback handlers that never answer(): " + ", ".join(missing)


def test_the_scan_finds_the_handlers_at_all():
    """Guard against the AST walk silently matching nothing and passing."""
    found = list(_callback_handlers())

    assert len(found) > 20, f"only found {len(found)} callback handlers — scan is broken"
