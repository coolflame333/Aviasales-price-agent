from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LiveSearchConfig:
    name: str
    url: str
    currency: str = "rub"
    max_price: int | None = None
    min_price: int = 10_000
    direct_only: bool = True
    wait_seconds: int = 30
    headless: bool = True
    profile_dir: str | None = None
    manual_check_seconds: int = 0
    outbound_departure_latest: str | None = None
    excluded_origin_airports: tuple[str, ...] = ()
    excluded_carriers: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveSearchResult:
    search: LiveSearchConfig
    price: int | None
    prices: list[int]
    alert: bool
    reason: str
    best_match: LivePriceCandidate | None = None


@dataclass(frozen=True)
class LivePriceCandidate:
    price: int
    departure_time: str | None = None
    arrival_time: str | None = None
    origin_airport: str | None = None
    destination_airport: str | None = None
    carrier: str | None = None


def load_live_config(path: str | Path) -> list[LiveSearchConfig]:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = {
        "currency": str(raw.get("currency", "rub")).lower(),
        "wait_seconds": int(raw.get("wait_seconds", 30)),
        "headless": bool(raw.get("headless", True)),
        "profile_dir": raw.get("profile_dir"),
        "manual_check_seconds": int(raw.get("manual_check_seconds", 0)),
        "min_price": int(raw.get("min_price", 10_000)),
        "outbound_departure_latest": raw.get("outbound_departure_latest"),
        "excluded_origin_airports": _normalize_airports(raw.get("excluded_origin_airports")),
        "excluded_carriers": _normalize_names(raw.get("excluded_carriers")),
    }

    searches = []
    for item in raw.get("searches", []):
        payload = {**defaults, **item}
        payload["excluded_origin_airports"] = _normalize_airports(payload.get("excluded_origin_airports"))
        payload["excluded_carriers"] = _normalize_names(payload.get("excluded_carriers"))
        searches.append(LiveSearchConfig(**payload))
    if not searches:
        raise ValueError("Live config must contain at least one item in 'searches'.")
    return searches


def check_live_search(search: LiveSearchConfig, previous_price: int | None = None) -> LiveSearchResult:
    page_text = _render_page_text(search)
    candidates = extract_price_candidates(
        page_text,
        direct_only=search.direct_only,
        min_price=search.min_price,
        outbound_departure_latest=search.outbound_departure_latest,
        excluded_origin_airports=search.excluded_origin_airports,
        excluded_carriers=search.excluded_carriers,
    )
    prices = [candidate.price for candidate in candidates]
    best_match = candidates[0] if candidates else None
    price = min(prices) if prices else None

    alert = False
    reasons: list[str] = []
    if price is None:
        reasons.append("no visible live price")
    else:
        if search.max_price is not None and price <= search.max_price:
            alert = True
            reasons.append(f"at or below target {search.max_price} {search.currency.upper()}")
        if previous_price is not None and price < previous_price:
            alert = True
            reasons.append(f"down {previous_price - price} {search.currency.upper()} vs previous live check")
        if not reasons:
            reasons.append("no buy signal")

    return LiveSearchResult(search, price, prices[:10], alert, "; ".join(reasons), best_match=best_match)


def extract_prices(
    page_text: str,
    direct_only: bool = True,
    min_price: int = 1_000,
    outbound_departure_latest: str | None = None,
    excluded_origin_airports: tuple[str, ...] = (),
    excluded_carriers: tuple[str, ...] = (),
) -> list[int]:
    return [
        candidate.price
        for candidate in extract_price_candidates(
            page_text,
            direct_only=direct_only,
            min_price=min_price,
            outbound_departure_latest=outbound_departure_latest,
            excluded_origin_airports=excluded_origin_airports,
            excluded_carriers=excluded_carriers,
        )
    ]


def extract_price_candidates(
    page_text: str,
    direct_only: bool = True,
    min_price: int = 1_000,
    outbound_departure_latest: str | None = None,
    excluded_origin_airports: tuple[str, ...] = (),
    excluded_carriers: tuple[str, ...] = (),
) -> list[LivePriceCandidate]:
    relevant_text = _direct_flights_section(page_text) if direct_only else page_text
    if outbound_departure_latest:
        return _extract_timed_candidates(
            relevant_text,
            outbound_departure_latest,
            min_price,
            excluded_origin_airports,
            excluded_carriers,
        )

    candidates = []
    for match in re.finditer(r"(\d[\d\s\u00a0]{2,})\s*(?:₽|руб|RUB)", relevant_text, re.IGNORECASE):
        value = int(re.sub(r"\D", "", match.group(1)))
        if min_price <= value <= 1_000_000:
            candidates.append(LivePriceCandidate(value))
    return sorted({candidate.price: candidate for candidate in candidates}.values(), key=lambda candidate: candidate.price)


def format_live_results(results: list[LiveSearchResult]) -> str:
    if not results:
        return "Live price monitor\nNo live searches checked."

    lines = ["Live price monitor"]
    for result in results:
        lines.append("")
        lines.append(result.search.name)
        if result.price is None:
            lines.append("Visible price: n/a")
        else:
            lines.append(f"Visible price: {result.price} {result.search.currency.upper()}")
            if result.best_match is not None:
                lines.append(f"Best match: {_format_candidate(result.best_match)}")
        lines.append(f"Reason: {result.reason}")
        lines.append(f"Open: {result.search.url}")
    return "\n".join(lines)


def live_buttons(results: list[LiveSearchResult]) -> dict[str, Any] | None:
    buttons = []
    for result in results:
        label = f"Open {result.search.name}"[:64]
        buttons.append([{"text": label, "url": result.search.url}])
    if not buttons:
        return None
    return {"inline_keyboard": buttons[:5]}


def _direct_flights_section(page_text: str) -> str:
    lower = page_text.lower()
    direct_marker = "прямые рейсы"
    starts = [match.start() for match in re.finditer(re.escape(direct_marker), lower)]
    start = starts[-1] if starts else -1
    if start == -1:
        return page_text

    tail = page_text[start:]
    lower_tail = tail.lower()
    end_candidates = [
        lower_tail.find(marker)
        for marker in (
            "другие рейсы",
            "показать ещё билеты",
            "авиакомпании\n",
            "направления\n",
        )
        if lower_tail.find(marker) > 0
    ]
    if not end_candidates:
        return tail
    return tail[: min(end_candidates)]


def _extract_timed_prices(
    section_text: str,
    latest_time: str,
    min_price: int,
    excluded_origin_airports: tuple[str, ...] = (),
    excluded_carriers: tuple[str, ...] = (),
) -> list[int]:
    return [
        candidate.price
        for candidate in _extract_timed_candidates(
            section_text,
            latest_time,
            min_price,
            excluded_origin_airports,
            excluded_carriers,
        )
    ]


def _extract_timed_candidates(
    section_text: str,
    latest_time: str,
    min_price: int,
    excluded_origin_airports: tuple[str, ...] = (),
    excluded_carriers: tuple[str, ...] = (),
) -> list[LivePriceCandidate]:
    latest_minutes = _time_to_minutes(latest_time)
    if latest_minutes is None:
        return []

    lines = [line.strip() for line in section_text.splitlines() if line.strip()]
    excluded_airports = {airport.upper() for airport in excluded_origin_airports}
    excluded_carrier_names = {carrier.casefold() for carrier in excluded_carriers}
    carriers_by_price = _carriers_by_price(lines)
    excluded_carrier_times = _excluded_carrier_times(lines, excluded_carrier_names)
    excluded_carrier_price_times = _excluded_carrier_price_times(lines, excluded_carrier_names)
    excluded_carrier_prices = {
        price
        for price, carriers in carriers_by_price.items()
        if any(carrier.casefold() in excluded_carrier_names for carrier in carriers)
    }
    candidates: list[LivePriceCandidate] = []
    for index, line in enumerate(lines):
        price = _parse_price(line)
        if price is None or price < min_price or price > 1_000_000:
            continue
        if "багаж" in line.lower():
            continue
        carriers = carriers_by_price.get(price, ())
        if any(carrier.casefold() in excluded_carrier_names for carrier in carriers) or price in excluded_carrier_prices:
            continue

        next_lines = lines[index + 1 :]
        next_price_index = next(
            (
                offset
                for offset, next_line in enumerate(next_lines)
                if _parse_price(next_line) is not None and "багаж" not in next_line.lower()
            ),
            len(next_lines),
        )
        flight_lines = next_lines[:next_price_index]
        flight_text = "\n".join(flight_lines).lower()
        if "пересад" in flight_text:
            continue
        if excluded_airports:
            block_airports = [
                candidate.upper()
                for candidate in flight_lines
                if re.fullmatch(r"[A-ZА-Я]{3}", candidate.upper())
            ]
            if set(block_airports) & excluded_airports:
                continue
            if not block_airports:
                continue
        else:
            block_airports = [
                candidate.upper()
                for candidate in flight_lines
                if re.fullmatch(r"[A-ZА-Я]{3}", candidate.upper())
            ]

        flight_times = [
            candidate
            for candidate in flight_lines
            if _time_to_minutes(candidate) is not None
        ]
        if not flight_times:
            continue
        if (departure_minutes := _time_to_minutes(flight_times[0])) is None or departure_minutes > latest_minutes:
            continue
        if flight_times[0] in excluded_carrier_times:
            continue
        if (price, flight_times[0]) in excluded_carrier_price_times:
            continue
        candidates.append(
            LivePriceCandidate(
                price=price,
                departure_time=flight_times[0],
                arrival_time=flight_times[-1] if len(flight_times) > 1 else None,
                origin_airport=block_airports[0] if block_airports else None,
                destination_airport=block_airports[-1] if len(block_airports) > 1 else None,
                carrier=carriers[0] if carriers else None,
            )
        )
    return sorted(
        {(
            candidate.price,
            candidate.departure_time,
            candidate.origin_airport,
        ): candidate for candidate in candidates}.values(),
        key=lambda candidate: candidate.price,
    )


def _normalize_airports(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip().upper(),) if value.strip() else ()
    return tuple(str(item).strip().upper() for item in value if str(item).strip())


def _normalize_names(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _carriers_by_price(lines: list[str]) -> dict[int, tuple[str, ...]]:
    carriers_by_price: dict[int, list[str]] = {}
    for index, line in enumerate(lines):
        price = _parse_price(line)
        if price is None:
            continue
        carrier = _previous_label(lines, index)
        if carrier and _looks_like_carrier(carrier):
            carriers_by_price.setdefault(price, [])
            if carrier not in carriers_by_price[price]:
                carriers_by_price[price].append(carrier)
    return {price: tuple(carriers) for price, carriers in carriers_by_price.items()}


def _excluded_carrier_price_times(lines: list[str], excluded_carriers: set[str]) -> set[tuple[int, str]]:
    if not excluded_carriers:
        return set()

    excluded: set[tuple[int, str]] = set()
    for index, line in enumerate(lines):
        price = _parse_price(line)
        if price is None:
            continue
        carrier = _previous_label(lines, index)
        if not carrier or carrier.casefold() not in excluded_carriers:
            continue
        next_lines = lines[index + 1 :]
        next_price_index = next(
            (
                offset
                for offset, next_line in enumerate(next_lines)
                if _parse_price(next_line) is not None and "багаж" not in next_line.lower()
            ),
            len(next_lines),
        )
        for candidate in next_lines[:next_price_index]:
            if _time_to_minutes(candidate) is not None:
                excluded.add((price, candidate))
    return excluded


def _excluded_carrier_times(lines: list[str], excluded_carriers: set[str]) -> set[str]:
    if not excluded_carriers:
        return set()

    excluded: set[str] = set()
    for index, line in enumerate(lines):
        price = _parse_price(line)
        if price is None:
            continue
        carrier = _previous_label(lines, index)
        if not carrier or carrier.casefold() not in excluded_carriers:
            continue
        next_lines = lines[index + 1 :]
        next_price_index = next(
            (
                offset
                for offset, next_line in enumerate(next_lines)
                if _parse_price(next_line) is not None and "багаж" not in next_line.lower()
            ),
            len(next_lines),
        )
        excluded.update(
            candidate
            for candidate in next_lines[:next_price_index]
            if _time_to_minutes(candidate) is not None
        )
    return excluded


def _previous_label(lines: list[str], index: int) -> str | None:
    for candidate in reversed(lines[:index]):
        if _parse_price(candidate) is not None:
            return None
        if _time_to_minutes(candidate) is not None:
            continue
        if re.fullmatch(r"[A-ZА-Я]{3}", candidate.upper()):
            continue
        if candidate.lower() in {"москва", "стамбул"}:
            continue
        return candidate
    return None


def _looks_like_carrier(text: str) -> bool:
    normalized = text.casefold()
    carrier_markers = (
        "победа",
        "s7",
        "аэрофлот",
        "airlines",
        "авиалинии",
        "уральские",
        "turkish",
        "southwind",
        "red wings",
        "азимут",
    )
    return any(marker in normalized for marker in carrier_markers)


def _format_candidate(candidate: LivePriceCandidate) -> str:
    route = " -> ".join(
        airport
        for airport in (candidate.origin_airport, candidate.destination_airport)
        if airport
    )
    times = " -> ".join(
        time
        for time in (candidate.departure_time, candidate.arrival_time)
        if time
    )
    if route and times:
        details = f"{route}, {times}"
    else:
        details = route or times or "details unavailable"
    if candidate.carrier:
        return f"{details}, {candidate.carrier}"
    return details


def _parse_price(text: str) -> int | None:
    match = re.search(r"(\d[\d\s\u00a0\u202f]{2,})\s*(?:₽|руб|RUB)", text, re.IGNORECASE)
    if not match:
        return None
    return int(re.sub(r"\D", "", match.group(1)))


def _time_to_minutes(text: str) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def _render_page_text(search: LiveSearchConfig) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for live checks. Install it with: "
            "python -m pip install playwright && python -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        try:
            text = _render_with_context(playwright, search)
        except Exception:
            if not search.profile_dir:
                raise
            fallback = LiveSearchConfig(
                **{**search.__dict__, "profile_dir": None}
            )
            text = _render_with_context(playwright, fallback)
    return text


def _render_with_context(playwright, search: LiveSearchConfig) -> str:
    context = _new_context(playwright, search)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(search.url, wait_until="domcontentloaded", timeout=search.wait_seconds * 1000)
        _click_text(page, "Да без проблем")
        if _click_text(page, "Найти билеты"):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=search.wait_seconds * 1000)
            except Exception:
                pass
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        _wait_for_results(page, search.wait_seconds)
        if _looks_like_bot_check(page) and search.manual_check_seconds > 0:
            page.wait_for_timeout(search.manual_check_seconds * 1000)
            _wait_for_results(page, search.wait_seconds)
        return page.locator("body").inner_text(timeout=search.wait_seconds * 1000)
    finally:
        context.close()


def _new_context(playwright, search: LiveSearchConfig):
    if search.profile_dir:
        profile_dir = Path(search.profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=search.headless,
            locale="ru-RU",
        )

    browser = playwright.chromium.launch(headless=search.headless)
    return browser.new_context(locale="ru-RU")


def _click_text(page, text: str, timeout_ms: int = 3_000) -> bool:
    try:
        page.get_by_text(text, exact=True).first.click(timeout=timeout_ms)
        return True
    except Exception:
        return False


def _wait_for_results(page, wait_seconds: int) -> None:
    deadline_ms = max(5_000, wait_seconds * 1000)
    for marker in ("Прямые рейсы", "Рекомендованный", "С пересадками"):
        try:
            page.get_by_text(marker).first.wait_for(timeout=deadline_ms)
            return
        except Exception:
            continue
    page.wait_for_timeout(5_000)


def _looks_like_bot_check(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        return False
    markers = (
        "не робот",
        "я не робот",
        "докажите",
        "captcha",
        "капча",
        "проверка",
    )
    return any(marker in text for marker in markers)
