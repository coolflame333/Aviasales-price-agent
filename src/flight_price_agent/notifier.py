from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from hashlib import sha256

from .monitor import RouteReport


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_ids: list[str], timeout_seconds: int = 20) -> None:
        if not bot_token or bot_token.startswith("put_your_"):
            raise ValueError("TELEGRAM_BOT_TOKEN is required.")
        chat_ids = [chat_id for chat_id in chat_ids if chat_id and not chat_id.startswith("put_your_")]
        if not chat_ids:
            raise ValueError("TELEGRAM_CHAT_ID or TELEGRAM_CHAT_IDS is required.")
        self.bot_token = bot_token
        self.chat_ids = chat_ids
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        return cls(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_ids=_chat_ids_from_env(),
        )

    def send_message(self, text: str, reply_markup: dict | None = None) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        for chat_id in self.chat_ids:
            self._send_one(url, chat_id, text, reply_markup)

    def _send_one(self, url: str, chat_id: str, text: str, reply_markup: dict | None) -> None:
        payload_data = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload_data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        payload = urllib.parse.urlencode(payload_data).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "flight-price-agent/0.1",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTP error {exc.code}: {details}") from exc

        if not result.get("ok"):
            description = result.get("description") or "unknown Telegram API error"
            raise RuntimeError(f"Telegram API error: {description}")


def format_telegram_alerts(
    reports: list[RouteReport],
    currency: str,
    include_ok: bool = False,
) -> str | None:
    selected = [report for report in reports if include_ok or report.alert]
    if not selected:
        return None

    lines = ["Aviasales price monitor"]
    for report in selected:
        route = f"{report.route.origin.upper()} -> {report.route.destination.upper()}"
        if not report.route.one_way:
            route = f"{route} -> {report.route.origin.upper()}"
        lines.append(route)
        if report.alert:
            lines.append(f"Reason: {report.reason}")

        if not report.best_offer:
            lines.append("No offers found")
            continue

        for index, offer in enumerate(report.offers[:5], 1):
            lines.append("")
            lines.append(f"{index}. {offer.price} {currency.upper()}")
            lines.append(f"Departure: {_display_when(offer.departure_at)}")
            if not report.route.one_way:
                lines.append(f"Return: {_display_when(offer.return_at)}")
            lines.append(f"Provider: {_provider(offer)}")
            lines.append(f"Link: {offer.aviasales_url or 'n/a'}")

    return "\n".join(lines)


def format_telegram_buttons(reports: list[RouteReport]) -> dict | None:
    buttons: list[list[dict[str, str]]] = []
    for report in reports:
        offer = report.best_offer
        if not offer or not offer.aviasales_url:
            continue
        label = f"Open {report.route.origin.upper()} {offer.departure_at or ''}".strip()
        buttons.append([{"text": label[:64], "url": offer.aviasales_url}])
    if not buttons:
        return None
    return {"inline_keyboard": buttons[:5]}


def _display_when(value: str | None) -> str:
    if not value:
        return "n/a"
    if "T" in value or ":" in value:
        return value
    return f"{value} time n/a"


def _provider(offer) -> str:
    provider = str(offer.raw.get("gate") or offer.airline or "n/a")
    source = str(offer.raw.get("_price_source") or "").strip()
    if source == "duffel":
        airline = f" / {offer.airline}" if offer.airline else ""
        return f"Duffel{airline}"
    if source == "amadeus":
        airline = f" / {offer.airline}" if offer.airline else ""
        return f"Amadeus{airline}"
    if source:
        return f"{provider} ({source})"
    return provider


def _chat_ids_from_env() -> list[str]:
    raw_many = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
    if raw_many:
        return [item.strip() for item in raw_many.split(",") if item.strip()]

    raw_one = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if raw_one:
        return [item.strip() for item in raw_one.split(",") if item.strip()]
    return []


def alert_fingerprint(report: RouteReport) -> str:
    offer = report.best_offer
    if not offer:
        payload = f"{report.route.key}|no-offer"
    else:
        payload = "|".join(
            [
                report.route.key,
                offer.departure_at or "",
                offer.return_at or "",
                str(offer.price),
                _alert_kind(report.reason),
            ]
        )
    return sha256(payload.encode("utf-8")).hexdigest()


def _alert_kind(reason: str) -> str:
    if "price disappeared" in reason:
        return "disappeared"
    if "price returned" in reason:
        return "returned"
    if "historical low" in reason:
        return "historical_low"
    if "down " in reason:
        return "drop"
    return "alert"
