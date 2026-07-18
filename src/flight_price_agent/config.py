from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RouteConfig:
    origin: str
    destination: str
    departure_at: str | None = None
    return_at: str | None = None
    one_way: bool = True
    direct: bool = False
    limit: int = 30
    pages: int = 5
    sorting: str = "price"
    unique: bool = False
    max_price: int | None = None
    origin_airports: list[str] | None = None
    destination_airports: list[str] | None = None
    departure_date_from: str | None = None
    departure_date_to: str | None = None
    return_date_from: str | None = None
    return_date_to: str | None = None
    min_trip_days: int | None = None
    max_trip_days: int | None = None
    outbound_departure_latest: str | None = None
    return_departure_earliest: str | None = None
    require_time_filters: bool = False
    price_sources: list[str] | None = None
    monitor_origins: list[str] | None = None
    monitor_dates: list[str] | None = None
    smart_verify: bool = False
    amadeus_max_offers: int = 20
    duffel_max_offers: int = 1
    duffel_supplier_timeout_ms: int = 5000
    duffel_origins: list[str] | None = None
    duffel_destinations: list[str] | None = None
    cabin_class: str = "economy"
    max_connections: int | None = None

    @property
    def key(self) -> str:
        origin_airports = ",".join(sorted(code.upper() for code in self.origin_airports or [])) or "*"
        destination_airports = (
            ",".join(sorted(code.upper() for code in self.destination_airports or [])) or "*"
        )
        parts = [
            self.origin.upper(),
            self.destination.upper(),
            self.departure_at or "*",
            self.return_at or "*",
            "oneway" if self.one_way else "roundtrip",
            "direct" if self.direct else "any",
            f"max_price:{self.max_price if self.max_price is not None else '*'}",
            f"from:{origin_airports}",
            f"to:{destination_airports}",
            f"dep_from:{self.departure_date_from or '*'}",
            f"dep_to:{self.departure_date_to or '*'}",
            f"ret_from:{self.return_date_from or '*'}",
            f"ret_to:{self.return_date_to or '*'}",
            f"min_days:{self.min_trip_days if self.min_trip_days is not None else '*'}",
            f"max_days:{self.max_trip_days if self.max_trip_days is not None else '*'}",
            f"out_before:{self.outbound_departure_latest or '*'}",
            f"return_after:{self.return_departure_earliest or '*'}",
            "strict_time" if self.require_time_filters else "allow_unknown_time",
        ]
        return "|".join(parts)


@dataclass(frozen=True)
class AgentConfig:
    currency: str = "rub"
    market: str = "ru"
    threshold_percent: float = 12.0
    absolute_drop: int = 2500
    routes: tuple[RouteConfig, ...] = ()


def load_config(path: str | Path) -> AgentConfig:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    routes = tuple(_expand_route(RouteConfig(**item)) for item in raw.get("routes", []))
    routes = tuple(route for group in routes for route in group)
    if not routes:
        raise ValueError("Config must contain at least one route in 'routes'.")

    return AgentConfig(
        currency=str(raw.get("currency", "rub")).lower(),
        market=str(raw.get("market", "ru")).lower(),
        threshold_percent=float(raw.get("threshold_percent", 12.0)),
        absolute_drop=int(raw.get("absolute_drop", 2500)),
        routes=routes,
    )


def example_config() -> dict[str, Any]:
    return {
        "currency": "rub",
        "market": "ru",
        "threshold_percent": 12,
        "absolute_drop": 3000,
        "routes": [
            {
                "origin": "MOW",
                "destination": "IST",
                "departure_at": "2026-08",
                "return_at": "2026-08",
                "one_way": False,
                "direct": True,
                "max_price": 18000,
                "destination_airports": ["IST"],
                "departure_date_from": "2026-08-06",
                "departure_date_to": "2026-08-06",
                "return_date_from": "2026-08-06",
                "return_date_to": "2026-08-06",
                "min_trip_days": 0,
                "outbound_departure_latest": "12:00",
                "return_departure_earliest": "16:00",
                "require_time_filters": False,
                "price_sources": ["latest", "month_matrix", "direct", "calendar"],
                "monitor_origins": ["MOW", "VKO", "SVO", "DME"],
                "monitor_dates": ["2026-08-05", "2026-08-06", "2026-08-07"],
                "smart_verify": True,
                "duffel_max_offers": 1,
                "duffel_supplier_timeout_ms": 5000,
                "duffel_destinations": ["IST"],
                "cabin_class": "economy",
                "limit": 30,
                "pages": 5,
            },
        ],
    }


def _expand_route(route: RouteConfig) -> list[RouteConfig]:
    origins = _codes(route.monitor_origins) or [route.origin.upper()]
    dates = route.monitor_dates or []
    if not dates:
        return [route]

    variants: list[RouteConfig] = []
    for origin in origins:
        for date in dates:
            clean_date = str(date).strip()
            variants.append(
                RouteConfig(
                    **{
                        **route.__dict__,
                        "origin": origin,
                        "origin_airports": [origin],
                        "departure_at": clean_date[:7],
                        "return_at": clean_date[:7] if not route.one_way else route.return_at,
                        "departure_date_from": clean_date,
                        "departure_date_to": clean_date,
                        "return_date_from": clean_date if not route.one_way else route.return_date_from,
                        "return_date_to": clean_date if not route.one_way else route.return_date_to,
                        "duffel_origins": [origin],
                    }
                )
            )
    return variants


def _codes(values: list[str] | None) -> list[str]:
    return [value.strip().upper() for value in values or [] if value.strip()]
