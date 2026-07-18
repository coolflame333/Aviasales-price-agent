from __future__ import annotations

from dataclasses import dataclass

from .client import Offer
from .config import AgentConfig, RouteConfig
from .storage import PreviousStats


@dataclass(frozen=True)
class RouteReport:
    route: RouteConfig
    best_offer: Offer | None
    offers: list[Offer]
    offers_count: int
    previous: PreviousStats
    alert: bool
    reason: str


def analyze_route(
    route: RouteConfig,
    offers: list[Offer],
    previous: PreviousStats,
    config: AgentConfig,
) -> RouteReport:
    best_offer = offers[0] if offers else None
    if best_offer is None:
        alert = previous.previous_had_offers is True
        reason = "price disappeared" if alert else "no offers returned"
        return RouteReport(route, None, [], 0, previous, alert, reason)

    alert = False
    reasons: list[str] = []

    if previous.previous_had_offers is False:
        alert = True
        reasons.append("price returned")

    if previous.previous_latest_best:
        latest_drop = previous.previous_latest_best - best_offer.price
        latest_drop_percent = latest_drop / previous.previous_latest_best * 100
        if latest_drop_percent >= config.threshold_percent:
            alert = True
            reasons.append(f"down {latest_drop_percent:.1f}% vs previous run")
        if latest_drop >= config.absolute_drop:
            alert = True
            reasons.append(f"down {latest_drop} {config.currency.upper()} vs previous run")
    else:
        reasons.append("first observation")

    if previous.previous_best and best_offer.price < previous.previous_best:
        alert = True
        record_drop = previous.previous_best - best_offer.price
        reasons.append(f"new historical low by {record_drop} {config.currency.upper()}")

    if route.max_price is not None and best_offer.price <= route.max_price:
        alert = True
        reasons.append(f"at or below target price {route.max_price} {config.currency.upper()}")

    if (
        (route.outbound_departure_latest or route.return_departure_earliest)
        and not route.require_time_filters
        and not _offer_has_time_fields(best_offer)
    ):
        reasons.append("time unknown in Data API; time filters not enforced")

    if not reasons:
        reasons.append("no significant drop")

    return RouteReport(route, best_offer, offers[:5], len(offers), previous, alert, "; ".join(reasons))


def _offer_has_time_fields(offer: Offer) -> bool:
    keys = (
        "departure_at",
        "depart_at",
        "departure_time",
        "depart_time",
        "outbound_departure_at",
        "outbound_departure_time",
        "return_at",
        "return_departure_at",
        "return_departure_time",
        "return_time",
    )
    for key in keys:
        value = offer.raw.get(key)
        if value and ":" in str(value):
            return True
    return False


def format_report(reports: list[RouteReport], currency: str) -> str:
    if not reports:
        return "No routes were checked."

    lines: list[str] = []
    for report in reports:
        status = "ALERT" if report.alert else "ok"
        route = f"{report.route.origin.upper()}->{report.route.destination.upper()}"
        if not report.best_offer:
            lines.append(f"[{status}] {route}: no offers returned ({report.reason})")
            continue

        offer = report.best_offer
        previous = (
            f", previous run best {report.previous.previous_latest_best} {currency.upper()}"
            if report.previous.previous_latest_best
            else ""
        )
        link = f", {offer.aviasales_url}" if offer.aviasales_url else ""
        lines.append(
            f"[{status}] {route}: best {offer.price} {currency.upper()} "
            f"({offer.departure_at or 'any date'}{previous}; {report.reason}){link}"
        )
    return "\n".join(lines)
