from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from .client import Offer, dedupe_offers, filter_airports, filter_dates, filter_times
from .config import RouteConfig


AMADEUS_TEST_BASE_URL = "https://test.api.amadeus.com"
AMADEUS_PROD_BASE_URL = "https://api.amadeus.com"


class AmadeusClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        environment: str = "test",
        timeout_seconds: int = 20,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("Amadeus client id and secret are required.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = AMADEUS_PROD_BASE_URL if environment.lower() == "prod" else AMADEUS_TEST_BASE_URL
        self.timeout_seconds = timeout_seconds
        self._access_token: str | None = None

    def flight_offers(
        self,
        route: RouteConfig,
        currency: str,
        max_offers: int | None = None,
    ) -> list[Offer]:
        departure_date = _exact_date(route.departure_date_from, route.departure_date_to, route.departure_at)
        if not departure_date:
            return []

        return_date = None
        if not route.one_way:
            return_date = _exact_date(route.return_date_from, route.return_date_to, route.return_at)
            if not return_date:
                return []

        params: dict[str, str | int] = {
            "originLocationCode": route.origin.upper(),
            "destinationLocationCode": route.destination.upper(),
            "departureDate": departure_date,
            "adults": 1,
            "currencyCode": currency.upper(),
            "max": max(1, min(max_offers or route.amadeus_max_offers, 250)),
        }
        if return_date:
            params["returnDate"] = return_date
        if route.direct:
            params["nonStop"] = "true"

        payload = self._get_json("/v2/shopping/flight-offers", params)
        offers = parse_amadeus_offers(payload.get("data", []), route, currency)
        offers = filter_dates(offers, route)
        offers = filter_airports(offers, route)
        offers = filter_times(offers, route)
        return dedupe_offers(offers)

    def _access_token_value(self) -> str:
        if self._access_token:
            return self._access_token

        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}/v1/security/oauth2/token",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "flight-price-agent/0.1",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))

        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Amadeus token response did not contain access_token.")
        self._access_token = str(token)
        return self._access_token

    def _get_json(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token_value()}",
                "User-Agent": "flight-price-agent/0.1",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


def parse_amadeus_offers(data: list[dict[str, Any]], route: RouteConfig, currency: str) -> list[Offer]:
    offers: list[Offer] = []
    for item in data:
        itineraries = item.get("itineraries")
        if not isinstance(itineraries, list) or not itineraries:
            continue

        outbound_segments = _segments(itineraries[0])
        if not outbound_segments:
            continue

        return_segments = _segments(itineraries[1]) if len(itineraries) > 1 else []
        price = _price(item)
        if price is None:
            continue

        first_outbound = outbound_segments[0]
        last_outbound = outbound_segments[-1]
        first_return = return_segments[0] if return_segments else None
        airline = _airline(item, first_outbound)

        raw = dict(item)
        raw["_price_source"] = "amadeus"
        raw["gate"] = "Amadeus"

        offers.append(
            Offer(
                route_key=route.key,
                origin=_iata(first_outbound.get("departure")) or route.origin.upper(),
                destination=_iata(last_outbound.get("arrival")) or route.destination.upper(),
                departure_at=_at(first_outbound.get("departure")),
                return_at=_at(first_return.get("departure")) if first_return else None,
                one_way=route.one_way,
                direct=route.direct,
                currency=str(item.get("price", {}).get("currency") or currency).lower(),
                price=price,
                airline=airline,
                flight_number=_flight_number(first_outbound),
                transfers=_transfers(outbound_segments, return_segments),
                link=None,
                raw=raw,
            )
        )

    return sorted(offers, key=lambda offer: offer.price)


def _exact_date(start: str | None, end: str | None, fallback: str | None) -> str | None:
    if start and end and start == end and len(start) >= 10:
        return start[:10]
    if fallback and len(fallback) >= 10:
        return fallback[:10]
    return None


def _segments(itinerary: Any) -> list[dict[str, Any]]:
    if not isinstance(itinerary, dict):
        return []
    segments = itinerary.get("segments")
    if not isinstance(segments, list):
        return []
    return [segment for segment in segments if isinstance(segment, dict)]


def _price(item: dict[str, Any]) -> int | None:
    raw_price = item.get("price", {})
    if not isinstance(raw_price, dict):
        return None
    total = raw_price.get("grandTotal") or raw_price.get("total")
    if total is None:
        return None
    try:
        return round(float(str(total)))
    except ValueError:
        return None


def _airline(item: dict[str, Any], first_segment: dict[str, Any]) -> str | None:
    validating = item.get("validatingAirlineCodes")
    if isinstance(validating, list) and validating:
        return str(validating[0])
    carrier = first_segment.get("carrierCode")
    return str(carrier) if carrier else None


def _flight_number(segment: dict[str, Any]) -> str | None:
    carrier = segment.get("carrierCode")
    number = segment.get("number")
    if carrier and number:
        return f"{carrier}{number}"
    return None


def _transfers(outbound_segments: list[dict[str, Any]], return_segments: list[dict[str, Any]]) -> int:
    outbound_transfers = max(0, len(outbound_segments) - 1)
    return_transfers = max(0, len(return_segments) - 1)
    return max(outbound_transfers, return_transfers)


def _iata(location: Any) -> str | None:
    if not isinstance(location, dict):
        return None
    value = location.get("iataCode")
    return str(value).upper() if value else None


def _at(location: Any) -> str | None:
    if not isinstance(location, dict):
        return None
    value = location.get("at")
    return str(value) if value else None
