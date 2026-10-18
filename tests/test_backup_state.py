"""
Tests for backing up and restoring the state file.
"""
import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.storage.state_store import StateStore
from src.tools import backup_state as bs

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def state_file(tmp_path):
    path = str(tmp_path / "state" / "trading_bot.db")
    os.makedirs(os.path.dirname(path))

    async def make():
        store = StateStore(db_path=path)
        await store.init_db()
        await store.set_many({"console_state": '{"hello": "world"}', "k": "v"})

    asyncio.run(make())
    return path


def read_state(path, key):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_backup_is_a_verified_copy_with_the_same_data(state_file, tmp_path):
    dest = str(tmp_path / "backups")
    path = bs.backup(state_file, dest, now=NOW)
    assert os.path.basename(path) == "trading_bot-20261018-120000.db"
    assert read_state(path, "console_state") == '{"hello": "world"}'
    assert bs._integrity_ok(path)
    assert not [f for f in os.listdir(dest) if f.endswith(".partial")]


def test_backup_works_while_the_app_has_the_file_open_and_writing(state_file, tmp_path):
    writer = sqlite3.connect(state_file)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO system_state (key, value) VALUES ('live', 'row') ON CONFLICT(key) DO UPDATE SET value='row'")
        writer.commit()
        writer.execute("BEGIN")
        writer.execute("UPDATE system_state SET value = 'uncommitted' WHERE key = 'live'")  # open transaction
        path = bs.backup(state_file, str(tmp_path / "b"), now=NOW)
    finally:
        writer.rollback()
        writer.close()
    assert read_state(path, "live") == "row"  # committed data only, and a consistent file


def test_old_backups_are_pruned_oldest_first(state_file, tmp_path):
    dest = str(tmp_path / "b")
    for i in range(5):
        bs.backup(state_file, dest, keep=3, now=NOW + timedelta(minutes=i))
    names = [os.path.basename(p) for p in bs.list_backups(dest)]
    assert names == [
        "trading_bot-20261018-120200.db",
        "trading_bot-20261018-120300.db",
        "trading_bot-20261018-120400.db",
    ]


def test_unrelated_files_in_the_backup_folder_are_never_touched(state_file, tmp_path):
    dest = tmp_path / "b"
    dest.mkdir()
    (dest / "photo.jpg").write_bytes(b"x")
    (dest / "trading_bot-notes.txt").write_text("keep me")
    for i in range(3):
        bs.backup(state_file, str(dest), keep=1, now=NOW + timedelta(minutes=i))
    assert (dest / "photo.jpg").exists() and (dest / "trading_bot-notes.txt").exists()
    assert len(bs.list_backups(str(dest))) == 1


def test_backup_of_a_missing_file_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError):
        bs.backup(str(tmp_path / "nope.db"), str(tmp_path / "b"))


def test_backup_of_a_file_that_is_not_a_database_is_discarded(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 20)
    with pytest.raises(Exception):
        bs.backup(str(bad), str(tmp_path / "b"), now=NOW)
    assert bs.list_backups(str(tmp_path / "b")) == []


def test_keep_must_be_positive(state_file, tmp_path):
    with pytest.raises(ValueError):
        bs.backup(state_file, str(tmp_path / "b"), keep=0)


def test_restore_replaces_the_state_and_keeps_the_old_file(state_file, tmp_path):
    backup_path = bs.backup(state_file, str(tmp_path / "b"), now=NOW)
    conn = sqlite3.connect(state_file)
    conn.execute("UPDATE system_state SET value = 'changed later' WHERE key = 'k'")
    conn.commit()
    conn.close()

    with pytest.raises(FileExistsError):
        bs.restore(backup_path, state_file)  # refuses without --force
    assert read_state(state_file, "k") == "changed later"

    kept = bs.restore(backup_path, state_file, force=True)
    assert read_state(state_file, "k") == "v"
    assert read_state(kept, "k") == "changed later"


def test_restore_onto_a_fresh_phone_creates_the_folder(state_file, tmp_path):
    backup_path = bs.backup(state_file, str(tmp_path / "b"), now=NOW)
    target = str(tmp_path / "new-phone" / "state" / "trading_bot.db")
    assert bs.restore(backup_path, target) is None
    assert read_state(target, "console_state") == '{"hello": "world"}'


def test_restore_refuses_a_damaged_backup_and_changes_nothing(state_file, tmp_path):
    damaged = tmp_path / "trading_bot-20200101-000000.db"
    damaged.write_bytes(b"garbage" * 50)
    with pytest.raises(RuntimeError):
        bs.restore(str(damaged), state_file, force=True)
    assert read_state(state_file, "k") == "v"
    assert not os.path.exists(state_file + ".before-restore")


def test_the_restored_state_file_works_with_the_app(state_file, tmp_path):
    backup_path = bs.backup(state_file, str(tmp_path / "b"), now=NOW)
    target = str(tmp_path / "other" / "trading_bot.db")
    bs.restore(backup_path, target)

    async def check():
        store = StateStore(db_path=target)
        await store.init_db()
        return await store.get_state("console_state")

    assert asyncio.run(check()) == '{"hello": "world"}'


def test_command_line_reports_success_and_failure(state_file, tmp_path, capsys):
    dest = str(tmp_path / "cli")
    assert bs.main(["backup", state_file, dest, "--keep", "2"]) == 0
    assert "backup written" in capsys.readouterr().out
    assert bs.main(["backup", str(tmp_path / "missing.db"), dest]) == 1
    assert "error" in capsys.readouterr().err
    assert bs.main(["restore", str(tmp_path / "missing.db"), state_file]) == 1
