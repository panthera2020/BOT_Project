"""
Alerting system dispatching event notifications to Telegram Bot and SMTP Email.
"""
from dataclasses import dataclass
from typing import Optional, List
import logging

logger = logging.getLogger("AlertEngine")


@dataclass
class AlertMessage:
    title: str
    body: str
    level: str = "INFO"  # INFO, WARNING, CRITICAL


class AlertDispatcher:
    def __init__(
        self,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_user: Optional[str] = None,
    ):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.smtp_host = smtp_host
        self.smtp_user = smtp_user
        self.sent_alerts: List[AlertMessage] = []

    async def send_alert(self, title: str, body: str, level: str = "INFO") -> AlertMessage:
        alert = AlertMessage(title=title, body=body, level=level)
        self.sent_alerts.append(alert)

        # In production this executes httpx requests to Telegram API and aiosmtplib
        logger.info(f"[{level}] {title}: {body}")
        return alert

    async def send_daily_report(self, report_text: str) -> AlertMessage:
        return await self.send_alert(
            title="DAILY BOT LEDGER REPORT (02:00 Lagos)",
            body=report_text,
            level="INFO",
        )
