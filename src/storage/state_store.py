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

            # Closed trades shown on the console (Demo and Live). The id is the id the
            # in-memory tracker assigned, so ids continue unbroken after a restart.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    pnl_r REAL NOT NULL,
                    is_win INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT 'sweep_reversal'
                );
            """)
            # Files saved before the strategy selector have no strategy column: add it. Their
            # trades were all taken by the original strategy, which is the column's default.
            async with db.execute("PRAGMA table_info(trade_history)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if "strategy" not in columns:
                await db.execute("ALTER TABLE trade_history ADD COLUMN strategy TEXT NOT NULL DEFAULT 'sweep_reversal'")

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

    async def set_many(self, items: Dict[str, str]) -> None:
        """Save several keys in ONE transaction: either all are saved or none are, so a
        power cut mid-write can never leave a half-updated state."""
        async with aiosqlite.connect(self.db_path) as db:
            for key, value in items.items():
                await db.execute(
                    "INSERT INTO system_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                    (key, value),
                )
            await db.commit()

    async def delete_state(self, key: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM system_state WHERE key = ?", (key,))
            await db.commit()

    async def save_trade(self, trade: Dict[str, Any], state_items: Optional[Dict[str, str]] = None) -> None:
        """Save one closed trade and, in the same transaction, any state that changes
        with it (for example the clean-trade count). Either both are saved or neither."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO trade_history "
                "(id, mode, symbol, side, qty, entry_price, exit_price, pnl_usd, pnl_r, is_win, status, timestamp, strategy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade["id"], trade["mode"], trade["symbol"], trade["side"], trade["qty"],
                    trade["entry_price"], trade["exit_price"], trade["pnl_usd"], trade["pnl_r"],
                    1 if trade["is_win"] else 0, trade["status"], trade["timestamp"],
                    trade.get("strategy") or "sweep_reversal",
                ),
            )
            for key, value in (state_items or {}).items():
                await db.execute(
                    "INSERT INTO system_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                    (key, value),
                )
            await db.commit()

    async def load_trades(self) -> List[Dict[str, Any]]:
        """All saved trades, oldest first."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM trade_history ORDER BY id ASC") as cursor:
                rows = await cursor.fetchall()
        trades = []
        for row in rows:
            trade = dict(row)
            trade["is_win"] = bool(trade["is_win"])
            trades.append(trade)
        return trades

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
