from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from flight_price_agent.client import TravelpayoutsClient, demo_offers
from flight_price_agent.config import example_config, load_config
from flight_price_agent.duffel import DuffelClient, duffel_airport_pairs
from flight_price_agent.monitor import RouteReport, analyze_route, format_report
from flight_price_agent.notifier import (
    TelegramNotifier,
    alert_fingerprint,
    format_telegram_alerts,
    format_telegram_buttons,
)
from flight_price_agent.storage import PriceStore


def load_env_file(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def collect_reports(
    config_path: str | Path,
    db_path: str | Path,
    demo: bool,
) -> tuple[str, list[RouteReport]]:
    config = load_config(config_path)
    store = PriceStore(db_path)
    reports: list[RouteReport] = []

    try:
        client = None
        duffel = None
        if not demo:
            token = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
            client = TravelpayoutsClient(token)
            duffel = make_duffel_client()

        for route in config.routes:
            previous = store.previous_stats(route.key, config.currency)
            offers = (
                demo_offers(route, config.currency)
                if demo
                else client.prices(route, config.currency, config.market)  # type: ignore[union-attr]
            )
            if route.smart_verify and duffel and not demo:
                try:
                    duffel_offers = duffel.flight_offers(
                        route,
                        config.currency,
                        route.duffel_max_offers,
                        route.duffel_supplier_timeout_ms,
                    )
                    offers = sorted([*offers, *duffel_offers], key=lambda offer: offer.price)
                except Exception as exc:
                    print(f"duffel skipped for {route.origin}->{route.destination}: {exc}", flush=True)
            elif route.smart_verify and not demo:
                print("duffel skipped: DUFFEL_ACCESS_TOKEN is not set", flush=True)
            report = analyze_route(route, offers, previous, config)
            reports.append(report)
            store.insert_observations(offers)
            store.record_route_check(
                route_key=route.key,
                currency=config.currency,
                had_offers=report.best_offer is not None,
                best_price=report.best_offer.price if report.best_offer else None,
                reason=report.reason,
            )
    finally:
        store.close()

    return config.currency, reports


def make_duffel_client() -> DuffelClient | None:
    access_token = os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip()
    if not access_token or access_token.startswith("put_your_"):
        return None
    return DuffelClient(access_token=access_token)


def duffel_token_mode() -> str:
    access_token = os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip()
    if access_token.startswith("duffel_live_"):
        return "live"
    if access_token.startswith("duffel_test_"):
        return "test"
    if access_token:
        return "unknown"
    return "missing"


def serialize_reports(reports: list[RouteReport]) -> list[dict[str, object]]:
    return [
        {
            "route_key": report.route.key,
            "alert": report.alert,
            "reason": report.reason,
            "offers_count": report.offers_count,
            "best_price": report.best_offer.price if report.best_offer else None,
            "best_link": report.best_offer.aviasales_url if report.best_offer else None,
            "top_offers": [
                {
                    "price": offer.price,
                    "departure_at": offer.departure_at,
                    "return_at": offer.return_at,
                    "source": offer.airline or offer.raw.get("gate"),
                    "transfers": offer.transfers,
                }
                for offer in report.offers
            ],
        }
        for report in reports
    ]


def print_reports(reports: list[RouteReport], currency: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(serialize_reports(reports), ensure_ascii=False, indent=2))
    else:
        print(format_report(reports, currency))


def send_telegram_if_needed(
    reports: list[RouteReport],
    currency: str,
    db_path: str | Path,
    enabled: bool,
    include_ok: bool = False,
    dedupe_hours: int = 24,
) -> None:
    if not enabled:
        return

    selected = [report for report in reports if include_ok or report.alert]
    skipped = 0

    if not include_ok and dedupe_hours > 0:
        store = PriceStore(db_path)
        try:
            fresh: list[RouteReport] = []
            for report in selected:
                fingerprint = alert_fingerprint(report)
                if store.notification_sent_recently(fingerprint, "telegram", dedupe_hours):
                    skipped += 1
                    continue
                fresh.append(report)
            selected = fresh
        finally:
            store.close()

    message = format_telegram_alerts(selected, currency, include_ok=True)
    if not message:
        if skipped:
            print(f"telegram skipped: {skipped} duplicate alert(s) inside {dedupe_hours}h", flush=True)
        return

    try:
        TelegramNotifier.from_env().send_message(message, reply_markup=format_telegram_buttons(selected))
    except ValueError as exc:
        print(f"telegram skipped: {exc}", flush=True)
        return

    if not include_ok:
        store = PriceStore(db_path)
        try:
            for report in selected:
                offer = report.best_offer
                store.record_notification(
                    fingerprint=alert_fingerprint(report),
                    route_key=report.route.key,
                    channel="telegram",
                    price=offer.price if offer else None,
                    departure_at=offer.departure_at if offer else None,
                    return_at=offer.return_at if offer else None,
                )
        finally:
            store.close()

    print("telegram notification sent", flush=True)


def run_agent(args: argparse.Namespace) -> int:
    load_env_file(args.env)
    currency, reports = collect_reports(args.config, args.db, args.demo)
    print_reports(reports, currency, args.json)
    send_telegram_if_needed(
        reports,
        currency,
        args.db,
        args.notify,
        args.notify_always,
        args.notify_dedupe_hours,
    )
    if args.no_alert_exit_code:
        return 0
    return 1 if any(report.alert for report in reports) else 0


def watch_agent(args: argparse.Namespace) -> int:
    load_env_file(args.env)
    interval_seconds = max(60, args.every_minutes * 60)

    try:
        while True:
            checked_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
            print(f"\n=== check {checked_at} ===", flush=True)
            try:
                currency, reports = collect_reports(args.config, args.db, args.demo)
                print_reports(reports, currency, args.json)
                send_telegram_if_needed(
                    reports,
                    currency,
                    args.db,
                    args.notify,
                    args.notify_always,
                    args.notify_dedupe_hours,
                )
            except Exception as exc:
                print(f"check failed: {exc}", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


def init_config(args: argparse.Namespace) -> int:
    path = Path(args.output)
    if path.exists() and not args.force:
        raise FileExistsError(f"{path} already exists. Use --force to overwrite.")
    path.write_text(
        json.dumps(example_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {path}")
    return 0


def show_history(args: argparse.Namespace) -> int:
    store = PriceStore(args.db)
    try:
        rows = store.latest_rows(args.limit)
    finally:
        store.close()

    for row in rows:
        print(
            f"{row['observed_at_utc']} {row['origin']}->{row['destination']} "
            f"{row['price']} {row['currency'].upper()} {row['departure_at'] or ''} {row['link'] or ''}"
        )
    return 0


def build_digest(rows: list, currency: str, hours: int) -> tuple[str, dict | None]:
    if not rows:
        return f"Aviasales price monitor\nDigest for last {hours}h\n\nNo stored prices.", None

    best_by_route: dict[str, dict[str, object]] = {}
    counts: dict[str, int] = {}
    for row in rows:
        route_key = row["route_key"]
        counts[route_key] = counts.get(route_key, 0) + 1
        current = best_by_route.get(route_key)
        if current is None or row["price"] < current["price"]:
            best_by_route[route_key] = dict(row)

    lines = [f"Aviasales price monitor", f"Digest for last {hours}h"]
    buttons: list[list[dict[str, str]]] = []
    for index, (route_key, row) in enumerate(
        sorted(best_by_route.items(), key=lambda item: int(item[1]["price"])),
        1,
    ):
        origin, destination, trip_label = _route_key_label(route_key)
        lines.append("")
        lines.append(f"{index}. {origin} -> {destination}{trip_label}")
        lines.append(f"Best: {row['price']} {currency.upper()}")
        lines.append(f"Departure: {row['departure_at'] or 'n/a'}")
        if row["return_at"]:
            lines.append(f"Return: {row['return_at']}")
        lines.append(f"Provider: {row['airline'] or 'n/a'}")
        lines.append(f"Observations: {counts[route_key]}")
        if row["link"]:
            lines.append(f"Link: {row['link']}")
            buttons.append([{"text": f"Open {origin} {row['departure_at'] or ''}"[:64], "url": row["link"]}])

    markup = {"inline_keyboard": buttons[:5]} if buttons else None
    return "\n".join(lines), markup


def _route_key_label(route_key: str) -> tuple[str, str, str]:
    parts = route_key.split("|")
    origin = parts[0] if len(parts) > 0 else "?"
    destination = parts[1] if len(parts) > 1 else "?"
    trip_kind = parts[4] if len(parts) > 4 else ""
    return origin, destination, f" -> {origin}" if trip_kind == "roundtrip" else ""


def _route_label(route_key: str) -> str:
    parts = route_key.split("|")
    origin, destination, trip_label = _route_key_label(route_key)
    departure_at = parts[9].removeprefix("dep_from:") if len(parts) > 9 else ""
    return_at = parts[11].removeprefix("ret_from:") if len(parts) > 11 else ""
    date_label = departure_at if departure_at and departure_at != "*" else "any date"
    if return_at and return_at not in ("*", departure_at):
        date_label = f"{date_label}/{return_at}"
    return f"{origin} -> {destination}{trip_label} {date_label}"


def _format_dt(value: datetime) -> str:
    return value.astimezone().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S local time")


def build_status_message(
    statuses: list,
    route_labels: dict[str, str],
    currency: str,
    stale_minutes: int,
) -> str:
    checked = [status for status in statuses if status.checked_at_utc]
    with_prices = [status for status in checked if status.had_offers]
    without_prices = [status for status in checked if status.had_offers is False]
    missing = len(statuses) - len(checked)

    latest_check = max(
        (datetime.fromisoformat(status.checked_at_utc) for status in checked),
        default=None,
    )
    stale = False
    if latest_check:
        stale = (datetime.now(UTC) - latest_check).total_seconds() > stale_minutes * 60

    lines = ["Aviasales price monitor", "Agent status"]
    lines.append("")
    lines.append(f"Routes checked: {len(checked)}/{len(statuses)}")
    lines.append(f"Routes with prices: {len(with_prices)}")
    lines.append(f"Routes without prices: {len(without_prices)}")
    if missing:
        lines.append(f"Never checked: {missing}")
    lines.append(f"Status: {'stale' if stale else 'ok'}")
    lines.append(f"Last check: {_format_dt(latest_check) if latest_check else 'n/a'}")

    best = min(with_prices, key=lambda status: status.best_price or 10**12, default=None)
    if best:
        lines.append(f"Best current price: {best.best_price} {currency.upper()}")
        lines.append(f"Best route: {route_labels.get(best.route_key, best.route_key)}")

    recent_prices = sorted(with_prices, key=lambda status: status.best_price or 10**12)
    if recent_prices:
        lines.append("")
        lines.append("Top current prices:")
        for index, status in enumerate(recent_prices[:5], 1):
            label = route_labels.get(status.route_key, status.route_key)
            lines.append(f"{index}. {label}: {status.best_price} {currency.upper()}")

    return "\n".join(lines)


def digest_command(args: argparse.Namespace) -> int:
    load_env_file(args.env)
    config = load_config(args.config)
    active_route_keys = {route.key for route in config.routes}
    store = PriceStore(args.db)
    try:
        rows = [row for row in store.digest_rows(args.hours) if row["route_key"] in active_route_keys]
    finally:
        store.close()

    message, markup = build_digest(rows, config.currency, args.hours)
    print(message)
    if args.notify:
        try:
            TelegramNotifier.from_env().send_message(message, reply_markup=markup)
            print("telegram digest sent")
        except ValueError as exc:
            print(f"telegram skipped: {exc}", flush=True)
    return 0


def status_command(args: argparse.Namespace) -> int:
    load_env_file(args.env)
    config = load_config(args.config)
    route_keys = [route.key for route in config.routes]
    route_labels = {route.key: _route_label(route.key) for route in config.routes}
    store = PriceStore(args.db)
    try:
        statuses = store.latest_route_checks(route_keys, config.currency)
    finally:
        store.close()

    message = build_status_message(statuses, route_labels, config.currency, args.stale_minutes)
    print(message)
    if args.notify:
        try:
            TelegramNotifier.from_env().send_message(message)
            print("telegram status sent")
        except ValueError as exc:
            print(f"telegram skipped: {exc}", flush=True)
    return 0


def test_telegram(args: argparse.Namespace) -> int:
    load_env_file(args.env)
    TelegramNotifier.from_env().send_message(
        "Aviasales Price Agent test: Telegram notifications are connected."
    )
    print("telegram test message sent")
    return 0


def check_duffel(args: argparse.Namespace) -> int:
    load_env_file(args.env)
    config = load_config(args.config)
    duffel = make_duffel_client()
    print(f"duffel token mode: {duffel_token_mode()}")
    if not duffel:
        print("duffel skipped: DUFFEL_ACCESS_TOKEN is not set")
        return 0

    for route in config.routes:
        print(
            f"route: {route.origin.upper()} -> {route.destination.upper()} "
            f"{route.departure_date_from or route.departure_at or ''}"
        )
        pairs = duffel_airport_pairs(route)
        print("airport pairs: " + ", ".join(f"{origin}->{destination}" for origin, destination in pairs))
        for origin, destination in pairs:
            route_variant = replace(route, origin=origin, destination=destination)
            try:
                offers = duffel.flight_offers_for_route(
                    route_variant,
                    config.currency,
                    args.max_offers,
                    route.duffel_supplier_timeout_ms,
                    route_key=route.key,
                )
            except Exception as exc:
                print(f"[error] {origin}->{destination}: {exc}")
                continue

            if not offers:
                print(f"[ok] {origin}->{destination}: 0 offers")
                continue

            best = offers[0]
            print(
                f"[ok] {origin}->{destination}: {len(offers)} offer(s); "
                f"best {best.price} {best.currency.upper()}; "
                f"departure {best.departure_at or 'n/a'}; "
                f"return {best.return_at or 'n/a'}; "
                f"airline {best.airline or 'n/a'}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aviasales Data API price monitoring agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Fetch prices, store observations, and detect drops")
    run.add_argument("--config", default="routes.json", help="Path to JSON route config")
    run.add_argument("--db", default="data/prices.sqlite", help="SQLite database path")
    run.add_argument("--env", default=".env", help="Optional .env file with TRAVELPAYOUTS_TOKEN")
    run.add_argument("--demo", action="store_true", help="Use built-in sample offers instead of API")
    run.add_argument("--json", action="store_true", help="Print machine-readable JSON report")
    run.add_argument("--notify", action="store_true", help="Send Telegram message when ALERT is detected")
    run.add_argument("--notify-always", action="store_true", help="Send Telegram message even without ALERT")
    run.add_argument("--notify-dedupe-hours", type=int, default=24, help="Suppress duplicate Telegram alerts")
    run.add_argument("--no-alert-exit-code", action="store_true", help="Return 0 even when ALERT is detected")
    run.set_defaults(func=run_agent)

    watch = subparsers.add_parser("watch", help="Run checks in a loop")
    watch.add_argument("--config", default="routes.json", help="Path to JSON route config")
    watch.add_argument("--db", default="data/prices.sqlite", help="SQLite database path")
    watch.add_argument("--env", default=".env", help="Optional .env file with TRAVELPAYOUTS_TOKEN")
    watch.add_argument("--every-minutes", type=int, default=30, help="Polling interval")
    watch.add_argument("--demo", action="store_true", help="Use built-in sample offers instead of API")
    watch.add_argument("--json", action="store_true", help="Print machine-readable JSON report")
    watch.add_argument("--notify", action="store_true", help="Send Telegram message when ALERT is detected")
    watch.add_argument("--notify-always", action="store_true", help="Send Telegram message even without ALERT")
    watch.add_argument("--notify-dedupe-hours", type=int, default=24, help="Suppress duplicate Telegram alerts")
    watch.set_defaults(func=watch_agent)

    init = subparsers.add_parser("init-config", help="Write an example routes.json")
    init.add_argument("--output", default="routes.json", help="Where to write the config")
    init.add_argument("--force", action="store_true", help="Overwrite an existing file")
    init.set_defaults(func=init_config)

    history = subparsers.add_parser("history", help="Print latest stored observations")
    history.add_argument("--db", default="data/prices.sqlite", help="SQLite database path")
    history.add_argument("--limit", type=int, default=20)
    history.set_defaults(func=show_history)

    digest = subparsers.add_parser("digest", help="Print or send a price-history digest")
    digest.add_argument("--config", default="routes.json", help="Path to JSON route config")
    digest.add_argument("--db", default="data/prices.sqlite", help="SQLite database path")
    digest.add_argument("--env", default=".env", help="Optional .env file with Telegram settings")
    digest.add_argument("--hours", type=int, default=12, help="History window")
    digest.add_argument("--notify", action="store_true", help="Send digest to Telegram")
    digest.set_defaults(func=digest_command)

    status = subparsers.add_parser("status", help="Print or send agent health status")
    status.add_argument("--config", default="routes.json", help="Path to JSON route config")
    status.add_argument("--db", default="data/prices.sqlite", help="SQLite database path")
    status.add_argument("--env", default=".env", help="Optional .env file with Telegram settings")
    status.add_argument(
        "--stale-minutes",
        type=int,
        default=90,
        help="Mark the agent stale if the last check is older than this",
    )
    status.add_argument("--notify", action="store_true", help="Send status to Telegram")
    status.set_defaults(func=status_command)

    telegram = subparsers.add_parser("test-telegram", help="Send a Telegram test message")
    telegram.add_argument("--env", default=".env", help="Optional .env file with Telegram settings")
    telegram.set_defaults(func=test_telegram)

    duffel = subparsers.add_parser("check-duffel", help="Check Duffel live verification parameters")
    duffel.add_argument("--config", default="routes.json", help="Path to JSON route config")
    duffel.add_argument("--env", default=".env", help="Optional .env file with Duffel settings")
    duffel.add_argument("--max-offers", type=int, default=1, help="Offers to request per airport pair")
    duffel.set_defaults(func=check_duffel)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
