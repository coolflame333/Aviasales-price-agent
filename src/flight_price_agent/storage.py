from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .client import Offer


@dataclass(frozen=True)
class PreviousStats:
    previous_best: int | None
    previous_latest_best: int | None
    observations: int
    previous_had_offers: bool | None = None
    previous_check_at: str | None = None


class PriceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.connection.close()

    def init_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at_utc TEXT NOT NULL,
                route_key TEXT NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                departure_at TEXT,
                return_at TEXT,
                one_way INTEGER NOT NULL,
                direct INTEGER NOT NULL,
                currency TEXT NOT NULL,
                price INTEGER NOT NULL,
                airline TEXT,
                flight_number TEXT,
                transfers INTEGER,
                link TEXT,
                raw_json TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_route_time "
            "ON observations(route_key, observed_at_utc)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at_utc TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                route_key TEXT NOT NULL,
                channel TEXT NOT NULL,
                price INTEGER,
                departure_at TEXT,
                return_at TEXT
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_notifications_fingerprint "
            "ON sent_notifications(fingerprint, channel, sent_at_utc)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS route_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at_utc TEXT NOT NULL,
                route_key TEXT NOT NULL,
                currency TEXT NOT NULL,
                had_offers INTEGER NOT NULL,
                best_price INTEGER,
                reason TEXT
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_route_checks_route_time "
            "ON route_checks(route_key, currency, checked_at_utc)"
        )
        self.connection.commit()

    def previous_stats(self, route_key: str, currency: str) -> PreviousStats:
        best_row = self.connection.execute(
            """
            SELECT MIN(price) AS price, COUNT(*) AS observations
            FROM observations
            WHERE route_key = ? AND currency = ?
            """,
            (route_key, currency),
        ).fetchone()

        latest_time_row = self.connection.execute(
            """
            SELECT MAX(observed_at_utc) AS observed_at_utc
            FROM observations
            WHERE route_key = ? AND currency = ?
            """,
            (route_key, currency),
        ).fetchone()

        latest_best: int | None = None
        latest_time = latest_time_row["observed_at_utc"] if latest_time_row else None
        if latest_time:
            latest_row = self.connection.execute(
                """
                SELECT MIN(price) AS price
                FROM observations
                WHERE route_key = ? AND currency = ? AND observed_at_utc = ?
                """,
                (route_key, currency, latest_time),
            ).fetchone()
            latest_best = latest_row["price"] if latest_row else None

        latest_check_row = self.connection.execute(
            """
            SELECT checked_at_utc, had_offers
            FROM route_checks
            WHERE route_key = ? AND currency = ?
            ORDER BY checked_at_utc DESC
            LIMIT 1
            """,
            (route_key, currency),
        ).fetchone()

        previous_had_offers = None
        previous_check_at = None
        if latest_check_row:
            previous_had_offers = bool(latest_check_row["had_offers"])
            previous_check_at = latest_check_row["checked_at_utc"]

        return PreviousStats(
            previous_best=best_row["price"] if best_row else None,
            previous_latest_best=latest_best,
            observations=best_row["observations"] if best_row else 0,
            previous_had_offers=previous_had_offers,
            previous_check_at=previous_check_at,
        )

    def insert_observations(self, offers: list[Offer]) -> str:
        observed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        rows = [
            (
                observed_at,
                offer.route_key,
                offer.origin,
                offer.destination,
                offer.departure_at,
                offer.return_at,
                1 if offer.one_way else 0,
                1 if offer.direct else 0,
                offer.currency,
                offer.price,
                offer.airline or offer.raw.get("gate"),
                offer.flight_number,
                offer.transfers,
                offer.aviasales_url,
                json.dumps(offer.raw, ensure_ascii=False, sort_keys=True),
            )
            for offer in offers
        ]
        if rows:
            self.connection.executemany(
                """
                INSERT INTO observations (
                    observed_at_utc, route_key, origin, destination, departure_at, return_at,
                    one_way, direct, currency, price, airline, flight_number, transfers, link, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.connection.commit()
        return observed_at

    def record_route_check(
        self,
        route_key: str,
        currency: str,
        had_offers: bool,
        best_price: int | None,
        reason: str,
    ) -> None:
        checked_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        self.connection.execute(
            """
            INSERT INTO route_checks (
                checked_at_utc, route_key, currency, had_offers, best_price, reason
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (checked_at, route_key, currency, 1 if had_offers else 0, best_price, reason),
        )
        self.connection.commit()

    def latest_rows(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT *
            FROM observations
            ORDER BY observed_at_utc DESC, price ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def digest_rows(self, hours: int = 12) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT *
            FROM observations
            WHERE observed_at_utc >= datetime('now', ?)
            ORDER BY route_key ASC, price ASC, observed_at_utc DESC
            """,
            (f"-{hours} hours",),
        ).fetchall()

    def notification_sent_recently(
        self,
        fingerprint: str,
        channel: str,
        ttl_hours: int,
    ) -> bool:
        if ttl_hours <= 0:
            return False

        row = self.connection.execute(
            """
            SELECT sent_at_utc
            FROM sent_notifications
            WHERE fingerprint = ? AND channel = ?
            ORDER BY sent_at_utc DESC
            LIMIT 1
            """,
            (fingerprint, channel),
        ).fetchone()
        if not row:
            return False

        sent_at = datetime.fromisoformat(row["sent_at_utc"])
        age = datetime.now(UTC) - sent_at
        return age.total_seconds() < ttl_hours * 3600

    def record_notification(
        self,
        fingerprint: str,
        route_key: str,
        channel: str,
        price: int | None,
        departure_at: str | None,
        return_at: str | None,
    ) -> None:
        sent_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        self.connection.execute(
            """
            INSERT INTO sent_notifications (
                sent_at_utc, fingerprint, route_key, channel, price, departure_at, return_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sent_at, fingerprint, route_key, channel, price, departure_at, return_at),
        )
        self.connection.commit()
