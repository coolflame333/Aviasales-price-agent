from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import gzip
from dataclasses import replace
from typing import Any

from .client import Offer, dedupe_offers, filter_airports, filter_dates, filter_times
from .config import RouteConfig


DUFFEL_API_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"


class DuffelClient:
    def __init__(self, access_token: str, timeout_seconds: int = 45) -> None:
        if not access_token:
            raise ValueError("Duffel access token is required.")
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds

    def flight_offers(
        self,
        route: RouteConfig,
        currency: str,
        max_offers: int | None = None,
        supplier_timeout_ms: int | None = None,
    ) -> list[Offer]:
        offers: list[Offer] = []
        for origin, destination in duffel_airport_pairs(route):
            route_variant = replace(route, origin=origin, destination=destination)
            offers.extend(
                self.flight_offers_for_route(
                    route_variant,
                    currency,
                    max_offers,
                    supplier_timeout_ms,
                    route_key=route.key,
                )
            )
        return dedupe_offers(offers)[: max(1, max_offers or route.duffel_max_offers)]

    def flight_offers_for_route(
        self,
        route: RouteConfig,
        currency: str,
        max_offers: int | None = None,
        supplier_timeout_ms: int | None = None,
        route_key: str | None = None,
    ) -> list[Offer]:
        departure_date = _exact_date(route.departure_date_from, route.departure_date_to, route.departure_at)
        if not departure_date:
            return []

        return_date = None
        if not route.one_way:
            return_date = _exact_date(route.return_date_from, route.return_date_to, route.return_at)
            if not return_date:
                return []

        data: dict[str, Any] = {
            "slices": _slices(route, departure_date, return_date),
            "passengers": [{"type": "adult"}],
            "cabin_class": route.cabin_class,
        }
        if route.direct:
            data["max_connections"] = 0
        elif route.max_connections is not None:
            data["max_connections"] = route.max_connections

        body = {
            "data": {
                **data,
            }
        }
        params = {
            "return_offers": "false",
            "supplier_timeout": max(2000, min(supplier_timeout_ms or route.duffel_supplier_timeout_ms, 60000)),
        }

        payload = self._post_json("/air/offer_requests", params, body)
        offer_request_id = payload.get("data", {}).get("id")
        if not offer_request_id:
            return []

        offers_payload = self._get_json(
            "/air/offers",
            {
                "offer_request_id": str(offer_request_id),
                "sort": "total_amount",
                "limit": max(1, min(max_offers or route.duffel_max_offers, 200)),
                "max_connections": 0 if route.direct else route.max_connections or 1,
            },
        )
        offers = parse_duffel_offers(offers_payload.get("data", []), route, currency, route_key=route_key)
        offers = filter_dates(offers, route)
        offers = filter_airports(offers, route)
        offers = filter_times(offers, route)
        offers = dedupe_offers(offers)
        return offers[: max(1, max_offers or route.duffel_max_offers)]

    def _post_json(self, path: str, params: dict[str, str | int], body: dict[str, Any]) -> dict[str, Any]:
        url = f"{DUFFEL_API_URL}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
                "Duffel-Version": DUFFEL_VERSION,
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": "flight-price-agent/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return _read_json_response(response)
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Duffel HTTP error {exc.code}: {details}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Duffel request timed out after {self.timeout_seconds}s") from exc
        except socket.timeout as exc:
            raise RuntimeError(f"Duffel request timed out after {self.timeout_seconds}s") from exc

    def _get_json(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        url = f"{DUFFEL_API_URL}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Duffel-Version": DUFFEL_VERSION,
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": "flight-price-agent/0.1",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return _read_json_response(response)
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Duffel HTTP error {exc.code}: {details}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Duffel request timed out after {self.timeout_seconds}s") from exc
        except socket.timeout as exc:
            raise RuntimeError(f"Duffel request timed out after {self.timeout_seconds}s") from exc


def parse_duffel_offers(
    data: list[dict[str, Any]],
    route: RouteConfig,
    currency: str,
    route_key: str | None = None,
) -> list[Offer]:
    offers: list[Offer] = []
    for item in data:
        slices = item.get("slices")
        if not isinstance(slices, list) or not slices:
            continue

        outbound_segments = _segments(slices[0])
        if not outbound_segments:
            continue

        return_segments = _segments(slices[1]) if len(slices) > 1 else []
        price = _price(item)
        if price is None:
            continue

        first_outbound = outbound_segments[0]
        last_outbound = outbound_segments[-1]
        first_return = return_segments[0] if return_segments else None
        airline = _airline(item, first_outbound)
        departure_at = _departing_at(first_outbound)
        return_at = _departing_at(first_return) if first_return else None

        raw = dict(item)
        raw["_price_source"] = "duffel"
        raw["gate"] = "Duffel"
        raw["departure_at"] = departure_at
        raw["return_at"] = return_at

        offers.append(
            Offer(
                route_key=route_key or route.key,
                origin=_iata(first_outbound.get("origin")) or route.origin.upper(),
                destination=_iata(last_outbound.get("destination")) or route.destination.upper(),
                departure_at=departure_at,
                return_at=return_at,
                one_way=route.one_way,
                direct=route.direct,
                currency=str(item.get("total_currency") or currency).lower(),
                price=price,
                airline=airline,
                flight_number=_flight_number(first_outbound),
                transfers=_transfers(outbound_segments, return_segments),
                link=None,
                raw=raw,
            )
        )

    return sorted(offers, key=lambda offer: offer.price)


def _slices(route: RouteConfig, departure_date: str, return_date: str | None) -> list[dict[str, Any]]:
    outbound = {
        "origin": route.origin.upper(),
        "destination": route.destination.upper(),
        "departure_date": departure_date,
    }
    if route.outbound_departure_latest:
        outbound["departure_time"] = {"to": route.outbound_departure_latest}

    slices = [outbound]
    if return_date:
        inbound = {
            "origin": route.destination.upper(),
            "destination": route.origin.upper(),
            "departure_date": return_date,
        }
        if route.return_departure_earliest:
            inbound["departure_time"] = {"from": route.return_departure_earliest}
        slices.append(inbound)
    return slices


def duffel_airport_pairs(route: RouteConfig) -> list[tuple[str, str]]:
    origins = _codes(route.duffel_origins) or [route.origin.upper()]
    destinations = _codes(route.duffel_destinations) or [route.destination.upper()]
    return [(origin, destination) for origin in origins for destination in destinations]


def _codes(values: list[str] | None) -> list[str]:
    return [value.strip().upper() for value in values or [] if value.strip()]


def _read_json_response(response: Any) -> dict[str, Any]:
    raw = response.read()
    if response.headers.get("Content-Encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _exact_date(start: str | None, end: str | None, fallback: str | None) -> str | None:
    if start and end and start == end and len(start) >= 10:
        return start[:10]
    if fallback and len(fallback) >= 10:
        return fallback[:10]
    return None


def _segments(slice_item: Any) -> list[dict[str, Any]]:
    if not isinstance(slice_item, dict):
        return []
    segments = slice_item.get("segments")
    if not isinstance(segments, list):
        return []
    return [segment for segment in segments if isinstance(segment, dict)]


def _price(item: dict[str, Any]) -> int | None:
    total = item.get("total_amount")
    if total is None:
        return None
    try:
        return round(float(str(total)))
    except ValueError:
        return None


def _airline(item: dict[str, Any], first_segment: dict[str, Any]) -> str | None:
    owner = item.get("owner")
    if isinstance(owner, dict):
        value = owner.get("iata_code") or owner.get("name")
        if value:
            return str(value)
    carrier = first_segment.get("marketing_carrier") or first_segment.get("operating_carrier")
    if isinstance(carrier, dict):
        value = carrier.get("iata_code") or carrier.get("name")
        if value:
            return str(value)
    return None


def _flight_number(segment: dict[str, Any]) -> str | None:
    carrier = segment.get("marketing_carrier")
    carrier_code = carrier.get("iata_code") if isinstance(carrier, dict) else None
    number = segment.get("marketing_carrier_flight_number")
    if carrier_code and number:
        return f"{carrier_code}{number}"
    return str(number) if number else None


def _transfers(outbound_segments: list[dict[str, Any]], return_segments: list[dict[str, Any]]) -> int:
    outbound_transfers = max(0, len(outbound_segments) - 1)
    return_transfers = max(0, len(return_segments) - 1)
    return max(outbound_transfers, return_transfers)


def _iata(location: Any) -> str | None:
    if not isinstance(location, dict):
        return None
    value = location.get("iata_code") or location.get("iataCode")
    return str(value).upper() if value else None


def _departing_at(segment: dict[str, Any] | None) -> str | None:
    if not isinstance(segment, dict):
        return None
    value = segment.get("departing_at")
    return str(value) if value else None
