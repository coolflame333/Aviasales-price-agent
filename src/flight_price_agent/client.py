from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, time
from typing import Any

from .config import RouteConfig


LATEST_PRICES_URL = "https://api.travelpayouts.com/v2/prices/latest"
MONTH_MATRIX_URL = "https://api.travelpayouts.com/v2/prices/month-matrix"
DIRECT_PRICES_URL = "https://api.travelpayouts.com/v1/prices/direct"
CALENDAR_PRICES_URL = "https://api.travelpayouts.com/v1/prices/calendar"
DEFAULT_PRICE_SOURCES = ("latest",)


@dataclass(frozen=True)
class Offer:
    route_key: str
    origin: str
    destination: str
    departure_at: str | None
    return_at: str | None
    one_way: bool
    direct: bool
    currency: str
    price: int
    airline: str | None
    flight_number: str | None
    transfers: int | None
    link: str | None
    raw: dict[str, Any]

    @property
    def aviasales_url(self) -> str | None:
        if not self.link:
            return self.search_url
        if self.link.startswith("http://") or self.link.startswith("https://"):
            return self.link
        if self.link.startswith("/"):
            return f"https://www.aviasales.ru{self.link}"
        return f"https://www.aviasales.ru/{self.link.lstrip('/')}"

    @property
    def search_url(self) -> str | None:
        departure = _compact_date(self.departure_at)
        if not departure:
            return None
        return_date = _compact_date(self.return_at)
        origin = self.origin.upper()
        destination = self.destination.upper()
        if self.one_way or not return_date:
            return f"https://www.aviasales.ru/search/{origin}{departure}{destination}1"
        return f"https://www.aviasales.ru/search/{origin}{departure}{destination}{return_date}1"


class TravelpayoutsClient:
    def __init__(self, token: str, timeout_seconds: int = 20) -> None:
        if not token:
            raise ValueError("Travelpayouts API token is required.")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def latest_prices(
        self,
        route: RouteConfig,
        currency: str,
        market: str,
    ) -> list[Offer]:
        return self._filter_and_sort_offers(
            route,
            self._latest_prices(route, currency, market),
        )

    def prices(
        self,
        route: RouteConfig,
        currency: str,
        market: str,
    ) -> list[Offer]:
        sources = route.price_sources or list(DEFAULT_PRICE_SOURCES)
        offers: list[Offer] = []
        errors: list[str] = []
        for source in sources:
            source_key = source.strip().lower().replace("-", "_")
            try:
                if source_key == "latest":
                    offers.extend(self._latest_prices(route, currency, market))
                elif source_key == "month_matrix":
                    offers.extend(self._month_matrix_prices(route, currency, market))
                elif source_key == "direct":
                    offers.extend(self._direct_prices(route, currency, market))
                elif source_key == "calendar":
                    offers.extend(self._calendar_prices(route, currency, market))
                else:
                    raise ValueError(f"Unknown price source: {source}")
            except Exception as exc:
                errors.append(f"{source_key}: {exc}")

        if not offers and errors:
            raise RuntimeError("All price sources failed: " + "; ".join(errors))

        return self._filter_and_sort_offers(route, dedupe_offers(offers))

    def _latest_prices(
        self,
        route: RouteConfig,
        currency: str,
        market: str,
    ) -> list[Offer]:
        params: dict[str, str | int] = {
            "currency": currency,
            "origin": route.origin.upper(),
            "destination": route.destination.upper(),
            "period_type": "month" if route.departure_at else "year",
            "one_way": str(route.one_way).lower(),
            "limit": route.limit,
            "show_to_affiliates": "true",
            "sorting": route.sorting,
            "token": self.token,
        }
        if route.departure_at:
            params["beginning_of_period"] = _period_start(route.departure_at)

        all_data: list[dict[str, Any]] = []
        for page in range(1, max(1, route.pages) + 1):
            params["page"] = page
            payload = self._get_json(LATEST_PRICES_URL, params)

            page_data = payload.get("data", [])
            if not page_data:
                break
            all_data.extend(_tag_source(page_data, "latest"))

        return parse_offers(all_data, route, currency)

    def _month_matrix_prices(
        self,
        route: RouteConfig,
        currency: str,
        market: str,
    ) -> list[Offer]:
        params: dict[str, str | int] = {
            "currency": currency,
            "origin": route.origin.upper(),
            "destination": route.destination.upper(),
            "show_to_affiliates": "true",
            "token": self.token,
        }
        if market:
            params["market"] = market
        if route.departure_at:
            params["month"] = route.departure_at[:7]

        payload = self._get_json(MONTH_MATRIX_URL, params)
        data = payload.get("data", [])
        return parse_offers(_tag_source(_flatten_payload_data(data), "month_matrix"), route, currency)

    def _direct_prices(
        self,
        route: RouteConfig,
        currency: str,
        market: str,
    ) -> list[Offer]:
        params: dict[str, str | int] = {
            "currency": currency,
            "origin": route.origin.upper(),
            "destination": route.destination.upper(),
            "token": self.token,
        }
        if market:
            params["market"] = market
        if route.departure_at:
            params["departure_at"] = route.departure_at[:7]
        if route.return_at and not route.one_way:
            params["return_at"] = route.return_at[:7]

        payload = self._get_json(DIRECT_PRICES_URL, params)
        data = _flatten_payload_data(payload.get("data", []))
        return parse_offers(_tag_source(data, "direct"), route, currency)

    def _calendar_prices(
        self,
        route: RouteConfig,
        currency: str,
        market: str,
    ) -> list[Offer]:
        params: dict[str, str | int] = {
            "currency": currency,
            "origin": route.origin.upper(),
            "destination": route.destination.upper(),
            "token": self.token,
        }
        if market:
            params["market"] = market
        if route.departure_at:
            params["departure_at"] = route.departure_at[:7]
        if route.return_at and not route.one_way:
            params["return_at"] = route.return_at[:7]

        payload = self._get_json(CALENDAR_PRICES_URL, params)
        data = _flatten_payload_data(payload.get("data", []))
        return parse_offers(_tag_source(data, "calendar"), route, currency)

    def _filter_and_sort_offers(self, route: RouteConfig, offers: list[Offer]) -> list[Offer]:
        if route.direct:
            offers = [offer for offer in offers if offer.transfers == 0]
        if route.return_at:
            offers = [offer for offer in offers if offer.return_at and offer.return_at.startswith(route.return_at)]
        offers = filter_dates(offers, route)
        offers = filter_airports(offers, route)
        offers = filter_times(offers, route)
        return sorted(offers, key=lambda offer: offer.price)

    def _get_json(self, url: str, params: dict[str, str | int]) -> dict[str, Any]:
        request_url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            request_url,
            headers={"Accept": "application/json", "User-Agent": "flight-price-agent/0.1"},
            method="GET",
        )

        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if not payload.get("success", True):
            error = payload.get("error") or payload.get("message") or "unknown API error"
            raise RuntimeError(f"Travelpayouts API returned an error: {error}")
        return payload


def parse_offers(data: list[dict[str, Any]], route: RouteConfig, currency: str) -> list[Offer]:
    offers: list[Offer] = []
    for item in data:
        price = _int_or_none(item.get("price"))
        if price is None:
            price = _int_or_none(item.get("value"))
        if price is None:
            continue

        offers.append(
            Offer(
                route_key=route.key,
                origin=_str_or_none(item.get("origin_airport"))
                or _str_or_none(item.get("origin"))
                or route.origin.upper(),
                destination=_str_or_none(item.get("destination_airport"))
                or _str_or_none(item.get("destination"))
                or route.destination.upper(),
                departure_at=_str_or_none(item.get("departure_at"))
                or _str_or_none(item.get("depart_date"))
                or route.departure_at,
                return_at=_str_or_none(item.get("return_at"))
                or _str_or_none(item.get("return_date"))
                or route.return_at,
                one_way=route.one_way,
                direct=route.direct,
                currency=currency,
                price=price,
                airline=_str_or_none(item.get("airline")),
                flight_number=_str_or_none(item.get("flight_number")),
                transfers=_int_or_none(item.get("transfers"))
                if "transfers" in item
                else _int_or_none(item.get("number_of_changes")),
                link=_str_or_none(item.get("link")),
                raw=item,
            )
        )

    return sorted(offers, key=lambda offer: offer.price)


def dedupe_offers(offers: list[Offer]) -> list[Offer]:
    best_by_key: dict[tuple[str, str, str | None, str | None, int], Offer] = {}
    for offer in offers:
        key = (
            offer.origin.upper(),
            offer.destination.upper(),
            offer.departure_at,
            offer.return_at,
            offer.price,
        )
        current = best_by_key.get(key)
        if current is None:
            best_by_key[key] = offer
            continue
        if _source_priority(offer.raw.get("_price_source")) < _source_priority(current.raw.get("_price_source")):
            best_by_key[key] = offer
    return sorted(best_by_key.values(), key=lambda offer: offer.price)


def _source_priority(source: Any) -> int:
    priorities = {"duffel": 0, "amadeus": 1, "direct": 2, "calendar": 3, "month_matrix": 4, "latest": 5}
    return priorities.get(str(source), 99)


def _tag_source(data: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for item in data:
        clone = dict(item)
        clone["_price_source"] = source
        tagged.append(clone)
    return tagged


def _flatten_payload_data(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        rows: list[dict[str, Any]] = []
        for item in value.values():
            rows.extend(_flatten_payload_data(item))
        return rows
    return []


def filter_airports(offers: list[Offer], route: RouteConfig) -> list[Offer]:
    origin_airports = _code_set(route.origin_airports)
    destination_airports = _code_set(route.destination_airports)

    if not origin_airports and not destination_airports:
        return offers

    filtered: list[Offer] = []
    for offer in offers:
        if origin_airports and offer.origin.upper() not in origin_airports:
            continue
        if destination_airports and offer.destination.upper() not in destination_airports:
            continue
        filtered.append(offer)
    return filtered


def filter_dates(offers: list[Offer], route: RouteConfig) -> list[Offer]:
    departure_from = _date_or_none(route.departure_date_from)
    departure_to = _date_or_none(route.departure_date_to)
    return_from = _date_or_none(route.return_date_from)
    return_to = _date_or_none(route.return_date_to)

    if not any((departure_from, departure_to, return_from, return_to, route.min_trip_days, route.max_trip_days)):
        return offers

    filtered: list[Offer] = []
    for offer in offers:
        departure_date = _date_or_none(offer.departure_at)
        return_date = _date_or_none(offer.return_at)

        if departure_from and (departure_date is None or departure_date < departure_from):
            continue
        if departure_to and (departure_date is None or departure_date > departure_to):
            continue
        if return_from and (return_date is None or return_date < return_from):
            continue
        if return_to and (return_date is None or return_date > return_to):
            continue

        if route.min_trip_days is not None or route.max_trip_days is not None:
            if departure_date is None or return_date is None:
                continue
            trip_days = (return_date - departure_date).days
            if route.min_trip_days is not None and trip_days < route.min_trip_days:
                continue
            if route.max_trip_days is not None and trip_days > route.max_trip_days:
                continue

        filtered.append(offer)
    return filtered


def filter_times(offers: list[Offer], route: RouteConfig) -> list[Offer]:
    outbound_latest = _time_or_none(route.outbound_departure_latest)
    return_earliest = _time_or_none(route.return_departure_earliest)

    if not outbound_latest and not return_earliest:
        return offers

    filtered: list[Offer] = []
    for offer in offers:
        outbound_time = _first_time(
            offer.raw,
            (
                "departure_at",
                "depart_at",
                "departure_time",
                "depart_time",
                "outbound_departure_at",
                "outbound_departure_time",
                "depart_date",
            ),
        )
        if outbound_latest:
            if outbound_time is None:
                if route.require_time_filters:
                    continue
            elif outbound_time > outbound_latest:
                continue

        return_time = _first_time(
            offer.raw,
            (
                "return_at",
                "return_departure_at",
                "return_departure_time",
                "return_time",
                "return_date",
            ),
        )
        if return_earliest:
            if return_time is None:
                if route.require_time_filters:
                    continue
            elif return_time < return_earliest:
                continue

        filtered.append(offer)
    return filtered


def demo_offers(route: RouteConfig, currency: str) -> list[Offer]:
    destination_price_offset = sum(ord(char) for char in route.destination.upper()) % 900
    sample = [
        {
            "price": 18420 + destination_price_offset,
            "airline": "PC",
            "flight_number": "PC389",
            "origin": route.origin.upper(),
            "destination": route.destination.upper(),
            "departure_at": _demo_datetime(route.departure_at, "12", "10:20:00+03:00"),
            "return_at": _demo_datetime(route.return_at, "19", "18:10:00+03:00"),
            "transfers": 0,
            "link": f"/search/{route.origin.upper()}1209{route.destination.upper()}1",
        },
        {
            "price": 21990 + destination_price_offset,
            "airline": "SU",
            "flight_number": "SU2130",
            "origin": route.origin.upper(),
            "destination": route.destination.upper(),
            "departure_at": _demo_datetime(route.departure_at, "14", "14:10:00+03:00"),
            "return_at": _demo_datetime(route.return_at, "21", "13:30:00+03:00"),
            "transfers": 0,
            "link": f"/search/{route.origin.upper()}1409{route.destination.upper()}1",
        },
    ]
    offers = parse_offers(sample, route, currency)
    offers = filter_dates(offers, route)
    offers = filter_airports(offers, route)
    offers = filter_times(offers, route)
    return offers


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _code_set(values: list[str] | None) -> set[str]:
    return {value.strip().upper() for value in values or [] if value.strip()}


def _first_time(item: dict[str, Any], keys: tuple[str, ...]) -> time | None:
    for key in keys:
        parsed = _time_or_none(_str_or_none(item.get(key)))
        if parsed:
            return parsed
    return None


def _time_or_none(value: str | None) -> time | None:
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    if "T" not in text and len(text) == 10 and text[4] == "-" and text[7] == "-":
        return None

    candidate = text
    if "T" in candidate:
        candidate = candidate.split("T", 1)[1]
    if " " in candidate:
        candidate = candidate.rsplit(" ", 1)[-1]
    candidate = candidate.replace("Z", "")
    if "+" in candidate:
        candidate = candidate.split("+", 1)[0]
    if "-" in candidate[1:]:
        candidate = candidate.rsplit("-", 1)[0]

    parts = candidate.split(":")
    if len(parts) < 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _date_or_none(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _compact_date(value: str | None) -> str | None:
    parsed = _date_or_none(value)
    if not parsed:
        return None
    return parsed.strftime("%d%m")


def _period_start(value: str) -> str:
    if len(value) == 7:
        return f"{value}-01"
    return value


def _demo_datetime(value: str | None, day: str, clock: str) -> str:
    if not value:
        return f"2026-09-{day}T{clock}"
    if len(value) == 7:
        return f"{value}-{day}T{clock}"
    if len(value) == 10:
        return f"{value}T{clock}"
    return value
