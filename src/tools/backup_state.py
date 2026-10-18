"""
Backup and restore of the console's state file (SQLite).

    python -m src.tools.backup_state backup  STATE_FILE BACKUP_DIR [--keep N]
    python -m src.tools.backup_state restore BACKUP_FILE STATE_FILE [--force]

A backup is made with SQLite's own backup API, so it is a consistent copy even while
the app is running and writing. Each backup is checked before it is kept. Secrets inside
are still encrypted (they were encrypted before they were saved), so a backup is no more
sensitive than the state file itself.

Why: on a phone the state lives in the terminal app's private storage. If that app's
data is ever cleared or the app is removed, the state goes with it. Keep backups in a
different place (for example the phone's shared storage).
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from typing import List, Optional

PREFIX = "trading_bot-"
SUFFIX = ".db"


def _integrity_ok(path: str) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def list_backups(backup_dir: str) -> List[str]:
    """Backup files, oldest first (the timestamp in the name sorts correctly)."""
    if not os.path.isdir(backup_dir):
        return []
    return sorted(
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.startswith(PREFIX) and name.endswith(SUFFIX)
    )


def backup(state_file: str, backup_dir: str, keep: int = 24, now: Optional[datetime] = None) -> str:
    """Write a consistent, verified copy of state_file into backup_dir and prune the
    oldest copies beyond `keep`. Returns the new backup's path."""
    if not os.path.isfile(state_file):
        raise FileNotFoundError(f"state file not found: {state_file}")
    if keep < 1:
        raise ValueError("keep must be at least 1")
    os.makedirs(backup_dir, exist_ok=True)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    final = os.path.join(backup_dir, f"{PREFIX}{stamp}{SUFFIX}")
    partial = final + ".partial"  # not matched by list_backups, so a half-written copy is never used

    source = sqlite3.connect(f"file:{state_file}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    if not _integrity_ok(partial):
        os.remove(partial)
        raise RuntimeError("the backup failed its integrity check and was discarded")
    os.replace(partial, final)

    for old in list_backups(backup_dir)[:-keep]:
        os.remove(old)
    return final


def restore(backup_file: str, state_file: str, force: bool = False) -> Optional[str]:
    """Put a backup in place as the state file. Stop the app first. An existing state
    file is kept next to it as '<name>.before-restore' (and needs --force to replace).
    Returns the path of the kept copy, if any."""
    if not os.path.isfile(backup_file):
        raise FileNotFoundError(f"backup not found: {backup_file}")
    if not _integrity_ok(backup_file):
        raise RuntimeError("that backup is damaged (integrity check failed); nothing was changed")

    kept = None
    if os.path.exists(state_file):
        if not force:
            raise FileExistsError(f"{state_file} already exists; stop the app and use --force to replace it")
        kept = state_file + ".before-restore"
        shutil.copy2(state_file, kept)
    os.makedirs(os.path.dirname(os.path.abspath(state_file)), exist_ok=True)
    # The old file's write-ahead log would not match the restored data.
    for suffix in ("-wal", "-shm"):
        if os.path.exists(state_file + suffix):
            os.remove(state_file + suffix)
    shutil.copy2(backup_file, state_file)
    return kept


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Back up or restore the console state file.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("state_file")
    b.add_argument("backup_dir")
    b.add_argument("--keep", type=int, default=24)
    r = sub.add_parser("restore")
    r.add_argument("backup_file")
    r.add_argument("state_file")
    r.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            print(f"backup written: {backup(args.state_file, args.backup_dir, keep=args.keep)}")
        else:
            kept = restore(args.backup_file, args.state_file, force=args.force)
            print(f"restored {args.backup_file} -> {args.state_file}" + (f" (previous file kept as {kept})" if kept else ""))
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
