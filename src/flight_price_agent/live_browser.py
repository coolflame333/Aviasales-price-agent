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
    direct_only: bool = True
    wait_seconds: int = 30
    headless: bool = True
    profile_dir: str | None = None
    manual_check_seconds: int = 0


@dataclass(frozen=True)
class LiveSearchResult:
    search: LiveSearchConfig
    price: int | None
    prices: list[int]
    alert: bool
    reason: str


def load_live_config(path: str | Path) -> list[LiveSearchConfig]:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = {
        "currency": str(raw.get("currency", "rub")).lower(),
        "wait_seconds": int(raw.get("wait_seconds", 30)),
        "headless": bool(raw.get("headless", True)),
        "profile_dir": raw.get("profile_dir"),
        "manual_check_seconds": int(raw.get("manual_check_seconds", 0)),
    }

    searches = []
    for item in raw.get("searches", []):
        payload = {**defaults, **item}
        searches.append(LiveSearchConfig(**payload))
    if not searches:
        raise ValueError("Live config must contain at least one item in 'searches'.")
    return searches


def check_live_search(search: LiveSearchConfig, previous_price: int | None = None) -> LiveSearchResult:
    page_text = _render_page_text(search)
    prices = extract_prices(page_text, direct_only=search.direct_only)
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

    return LiveSearchResult(search, price, prices[:10], alert, "; ".join(reasons))


def extract_prices(page_text: str, direct_only: bool = True) -> list[int]:
    relevant_text = _direct_flights_section(page_text) if direct_only else page_text
    prices = []
    for match in re.finditer(r"(\d[\d\s\u00a0]{2,})\s*(?:₽|руб|RUB)", relevant_text, re.IGNORECASE):
        value = int(re.sub(r"\D", "", match.group(1)))
        if 1_000 <= value <= 1_000_000:
            prices.append(value)
    return sorted(set(prices))


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
    start = lower.find("прямые рейсы")
    if start == -1:
        return page_text

    tail = page_text[start:]
    lower_tail = tail.lower()
    end_candidates = [
        lower_tail.find(marker)
        for marker in (
            "рекомендованный",
            "рейсы с пересадками",
            "с пересадками",
            "другие рейсы",
        )
        if lower_tail.find(marker) > 0
    ]
    if not end_candidates:
        return tail
    return tail[: min(end_candidates)]


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
