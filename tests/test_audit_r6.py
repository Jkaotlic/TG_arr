"""Audit round 6 (2026-07-28) — regression tests for the findings fixed in
`analysis/audit-2026-07-28.md`.

Each test is written to FAIL against the pre-fix code and pass after it:

- SEC-R6-02 — `VACUUM INTO` must not break on a quote in the backup path.
- BUG-R6-01 — background tasks must be awaited after cancel() on shutdown.

SEC-R6-01 (constant-time webhook token comparison) is gone with the webhook
itself: the *arr "on import" server it belonged to was removed on 2026-07-29,
replaced by polling in bot/services/library_watcher.py. Nothing left to test.
"""

import asyncio
import inspect
from pathlib import Path

import pytest

from bot.db import Database
from bot.main import _cancel_background_tasks


# ---------------------------------------------------------------- SEC-R6-02
@pytest.mark.asyncio
async def test_backup_survives_quote_in_path(tmp_path):
    """SEC-R6-02: the backup path was interpolated into `VACUUM INTO '<path>'`
    with an f-string — a single quote anywhere in the resolved path closes the
    SQL string literal and turns the rest of the path into SQL.
    """
    quoted_dir = tmp_path / "it's data"
    quoted_dir.mkdir()
    db = Database(str(quoted_dir / "bot.db"))
    await db.connect()
    try:
        created = await db._backup()
        assert created == 1
        backups = list((quoted_dir / "backup").glob("bot-*.db"))
        assert len(backups) == 1
        assert backups[0].stat().st_size > 0
    finally:
        await db.close()


# ---------------------------------------------------------------- BUG-R6-01
@pytest.mark.asyncio
async def test_cancel_background_tasks_awaits_cancellation():
    """BUG-R6-01: `task.cancel()` without an await returns before the task has
    actually processed the cancellation, so shutdown races the task's `finally`
    blocks and asyncio logs "Task was destroyed but it is pending".
    """
    cleaned: list[str] = []

    async def _worker(name: str) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cleaned.append(name)
            raise

    tasks = [asyncio.create_task(_worker("a")), asyncio.create_task(_worker("b"))]
    await asyncio.sleep(0)  # let both tasks reach their await point

    await _cancel_background_tasks(tasks)

    assert sorted(cleaned) == ["a", "b"], "cancellation must be awaited, not fired and forgotten"
    assert all(t.done() for t in tasks)


@pytest.mark.asyncio
async def test_cancel_background_tasks_ignores_task_errors():
    """A background task that raises on cancellation must not break shutdown."""

    async def _bad() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup exploded") from None

    task = asyncio.create_task(_bad())
    await asyncio.sleep(0)

    await _cancel_background_tasks([task])  # must not raise
    assert task.done()


@pytest.mark.asyncio
async def test_cancel_background_tasks_accepts_none_entries():
    """Shutdown passes optional tasks; None entries are skipped."""
    await _cancel_background_tasks([None])  # must not raise


def test_main_shutdown_uses_the_helper():
    """The finally-block in main() must route through the awaited helper."""
    from bot import main as main_mod

    source = inspect.getsource(main_mod.main)
    assert "_cancel_background_tasks" in source
    assert "liveness_task.cancel()" not in source
    assert "cleanup_task.cancel()" not in source


def test_analysis_report_exists():
    """The audit report itself is a deliverable — keep it in the repo."""
    report = Path(__file__).resolve().parents[1] / "analysis" / "audit-2026-07-28.md"
    assert report.is_file(), "analysis/audit-2026-07-28.md must exist"
    text = report.read_text(encoding="utf-8")
    for finding in ("SEC-R6-02", "BUG-R6-01"):
        assert finding in text
