"""
Shared in-process state + a tiny pub/sub event hub.

Used so the demo front end and the live call pipeline can talk:
  - the dashboard POSTs the campaign context here (bot reads it when a call connects)
  - the bot publishes transcript + status events here; the dashboard's /events
    websocket streams them to the browser

Single-process, in-memory — perfect for a demo, not for multi-worker production.
"""

import asyncio
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()  # imported before server.py loads .env, and CALL_LANGUAGE is read below

# artintele.ai calling for itself. Every KEY_FACT is taken from the live artintele.ai
# pages (home, /services, /pricing) as of 28 Sep 2026 — the agent is told to say
# nothing beyond these, so do not add a fact here that the site does not state.
DEFAULT_CAMPAIGN: dict[str, Any] = {
    "CALL_LANGUAGE": os.getenv("CALL_LANGUAGE", "kn"),
    "CALL_TYPE": "outbound",
    "GOAL": "Introduce artintele.ai's AI telecaller and AI receptionist, and get the caller's "
            "consent for our team to contact them on WhatsApp to book a free consultation",
    "CUSTOMER": {"name": "sir/madam", "phone": "", "source": "business outreach"},
    "OFFER": "A free consultation: in one conversation we tell you whether AI is worth it for "
             "your business at all, and if it isn't yet, we say so",
    "KEY_FACTS": [
        "artintele.ai is a brand of Prachalas Private Limited, based in Bengaluru",
        "This call itself is being made by artintele.ai's own AI telecaller",
        "artintele.ai is a full-service AI automation company: AI voice agents, AI telecaller "
        "and AI receptionist, WhatsApp automation, AI chatbots and workflow automation",
        "The AI telecaller greets callers, qualifies leads, routes requests and books "
        "appointments, around the clock, so no call is missed",
        "The AI agents speak 20+ languages, commonly English, Hindi, Kannada, Tamil, Telugu "
        "and Malayalam",
        "Every solution is custom-built around the business's existing processes and "
        "deployed end to end",
        "WhatsApp automation is built on the official WhatsApp Business Platform",
        "CRM tools: custom CRM setups and integrations that capture every lead and keep "
        "follow-ups organised",
        "Pricing is custom, priced to the business's actual requirements; there is no fixed "
        "published price",
        "For comparison, a full-time receptionist or telecaller in India typically costs about "
        "fifteen to thirty-five thousand rupees a month plus benefits (approximate market range)",
        "Sectors we deploy in, each with its own script, integrations and compliance rules: "
        "Healthcare (appointment booking, reminder and reschedule calls, insurance workflows, "
        "patient support); Education (admissions enquiries answered the minute they arrive, "
        "lead qualification, fee reminders, student support); Real Estate (every portal "
        "enquiry called back, qualified on budget and locality, site visits booked); "
        "Manufacturing (vendor communication, order and dispatch status, process tracking); "
        "Finance (document collection, KYC workflows, payment and renewal reminders); "
        "Retail and E-commerce (order confirmation, delivery and returns support)",
        "If a business's sector is not listed, it is almost certainly still a fit — we would "
        "ask them to tell us the workflow",
        "Website: artintele.ai. Customer care: plus nine one, nine one one zero eight, "
        "four five two two seven",
    ],
    "DO_NOT_SAY": "Never quote a price, discount or delivery timeline. Never name a client, "
                  "give a performance figure or claim a specific CRM integration.",
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
