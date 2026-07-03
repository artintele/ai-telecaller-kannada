"""
Shared in-process state + a tiny pub/sub event hub.

Used so the demo front end and the live call pipeline can talk:
  - the dashboard POSTs the campaign context here (bot reads it when a call connects)
  - the bot publishes transcript + status events here; the dashboard's /events
    websocket streams them to the browser

Single-process, in-memory — perfect for a demo, not for multi-worker production.
"""

import asyncio
from typing import Any

DEFAULT_CAMPAIGN: dict[str, Any] = {
    "CALL_TYPE": "outbound",
    "GOAL": "Confirm interest in the personal loan offer and get consent to send details on WhatsApp",
    "CUSTOMER": {"name": "sir/madam", "phone": "", "source": "website signup"},
    "OFFER": "Personal loan starting at twelve percent, no processing fee below five lakh",
    "KEY_FACTS": [
        "Interest starts at twelve percent per annum",
        "No processing fee for loans below five lakh rupees",
        "Documents: Aadhaar, PAN, 3 months bank statement, salary slip",
    ],
    "DO_NOT_SAY": "Do not promise final approval or a specific rate before verification",
}


class Hub:
    def __init__(self):
        self.campaign: dict[str, Any] = dict(DEFAULT_CAMPAIGN)
        self.status: str = "idle"
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    async def publish(self, event: dict) -> None:
        if event.get("type") == "status":
            self.status = event.get("status", self.status)
        for q in list(self.subscribers):
            await q.put(event)


hub = Hub()
