"""
Asynchronous SQLite database schema, transaction ledger, and state persistence engine.
"""
import os
import aiosqlite
from typing import Optional, Dict, Any, List


class StateStore:
    def __init__(self, db_path: str = "trading_bot.db"):
        self.db_path = db_path

    async def init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            
            # Key-value config & system state table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Trade ledger table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT UNIQUE NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    tp1_price REAL NOT NULL,
                    tp2_price REAL NOT NULL,
                    status TEXT NOT NULL,
                    realized_pnl_r REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                );
            """)

            # Rejection funnel telemetry table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS rejection_funnel (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stage TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Virtual bot wallet & withdrawal ledger
            await db.execute("""
                CREATE TABLE IF NOT EXISTS wallet_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    balance REAL NOT NULL,
                    available_balance REAL NOT NULL,
                    locked_margin REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 24-hour withdrawal lock requests
            await db.execute("""
                CREATE TABLE IF NOT EXISTS withdrawal_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    amount REAL NOT NULL,
                    destination_address TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    unlock_at TIMESTAMP NOT NULL
                );
            """)
            await db.commit()

    async def set_state(self, key: str, value: str) -> None:
        """Insert or update one key in the system_state table."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO system_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (key, value),
            )
            await db.commit()

    async def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value FROM system_state WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default

    async def log_rejection(self, stage: str, reason: str, symbol: str, details: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO rejection_funnel (stage, reason, symbol, details) VALUES (?, ?, ?, ?)",
                (stage, reason, symbol, details),
            )
            await db.commit()

    async def get_rejection_counts(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT stage, reason, COUNT(*) as count FROM rejection_funnel GROUP BY stage, reason"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
