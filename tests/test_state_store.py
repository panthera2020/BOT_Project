"""
Unit tests for the SQLite StateStore, WAL mode, tables, and rejection logging.
"""
import pytest
import os
from src.storage.state_store import StateStore


@pytest.mark.asyncio
async def test_state_store_lifecycle(tmp_path):
    db_file = str(tmp_path / "test_bot.db")
    store = StateStore(db_path=db_file)
    await store.init_db()

    assert os.path.exists(db_file)

    # Log rejection telemetry
    await store.log_rejection("GATE_6_NET_RR", "NET_RR_BELOW_GATE (1.85 < 2.0)", "BTCUSDT", "d=200")
    await store.log_rejection("GATE_6_NET_RR", "NET_RR_BELOW_GATE (1.85 < 2.0)", "BTCUSDT", "d=210")
    await store.log_rejection("GATE_2_DAILY_CAP", "DAILY_CAP_REACHED (2/2)", "BTCUSDT")

    counts = await store.get_rejection_counts()
    assert len(counts) == 2
    rr_entry = next(c for c in counts if c["stage"] == "GATE_6_NET_RR")
    assert rr_entry["count"] == 2
