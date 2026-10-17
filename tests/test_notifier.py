"""
Unit tests for the AlertDispatcher (Telegram & Email notification channels).
"""
import pytest
from src.alerts.notifier import AlertDispatcher


@pytest.mark.asyncio
async def test_alert_dispatcher():
    dispatcher = AlertDispatcher(
        telegram_token="mock_token",
        telegram_chat_id="mock_chat_id",
        smtp_host="smtp.example.com",
    )

    alert = await dispatcher.send_alert(
        title="TRADE_FILLED",
        body="BTCUSDT Long filled at 99,655.0",
        level="INFO",
    )
    assert alert.title == "TRADE_FILLED"
    assert len(dispatcher.sent_alerts) == 1

    report = await dispatcher.send_daily_report("Daily PnL: +2.1R")
    assert "DAILY BOT LEDGER" in report.title
    assert len(dispatcher.sent_alerts) == 2
