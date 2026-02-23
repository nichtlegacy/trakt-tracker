from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from trakt_tracker.models import WatchEvent


@dataclass(frozen=True)
class ProcessedEventRow:
    history_id: int
    watched_at: datetime
    media_type: str
    title_key: str
    runtime_min: float
    is_rewatch: bool


class StateStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_events (
                history_id INTEGER PRIMARY KEY,
                watched_at TEXT NOT NULL,
                media_type TEXT NOT NULL,
                trakt_id INTEGER NOT NULL,
                title_key TEXT NOT NULL,
                runtime_min REAL NOT NULL,
                is_rewatch INTEGER NOT NULL,
                show_trakt_id INTEGER,
                season_number INTEGER,
                episode_number INTEGER,
                year INTEGER,
                title TEXT,
                show_title TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_processed_events_watched_at
                ON processed_events(watched_at);

            CREATE TABLE IF NOT EXISTS dead_letters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                history_id INTEGER,
                payload_json TEXT NOT NULL,
                error TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._ensure_processed_events_columns()
        self._conn.commit()

    def _ensure_processed_events_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(processed_events)").fetchall()
        }
        alter_statements = {
            "show_trakt_id": "ALTER TABLE processed_events ADD COLUMN show_trakt_id INTEGER",
            "season_number": "ALTER TABLE processed_events ADD COLUMN season_number INTEGER",
            "episode_number": "ALTER TABLE processed_events ADD COLUMN episode_number INTEGER",
            "year": "ALTER TABLE processed_events ADD COLUMN year INTEGER",
            "title": "ALTER TABLE processed_events ADD COLUMN title TEXT",
            "show_title": "ALTER TABLE processed_events ADD COLUMN show_title TEXT",
        }
        for column_name, statement in alter_statements.items():
            if column_name not in columns:
                self._conn.execute(statement)

    def close(self) -> None:
        self._conn.close()

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_state(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def get_backfill_completed(self) -> bool:
        return self.get_state("backfill_completed") == "1"

    def set_backfill_completed(self, completed: bool) -> None:
        self.set_state("backfill_completed", "1" if completed else "0")

    def get_trakt_refresh_token(self) -> str | None:
        return self.get_state("trakt_refresh_token")

    def set_trakt_refresh_token(self, refresh_token: str) -> None:
        self.set_state("trakt_refresh_token", refresh_token)

    def get_cursor(self) -> tuple[datetime | None, int | None]:
        watched_at = self.get_state("last_watched_at")
        history_id = self.get_state("last_history_id")

        cursor_dt = None
        if watched_at:
            cursor_dt = _parse_datetime(watched_at)

        cursor_id = int(history_id) if history_id else None
        return cursor_dt, cursor_id

    def set_cursor(self, watched_at: datetime, history_id: int) -> None:
        self.set_state("last_watched_at", watched_at.astimezone(timezone.utc).isoformat())
        self.set_state("last_history_id", str(history_id))

    def has_processed(self, history_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_events WHERE history_id = ?", (history_id,)
        ).fetchone()
        return row is not None

    def mark_processed_many(self, events: Iterable[WatchEvent]) -> None:
        rows = [
            (
                event.history_id,
                event.watched_at.astimezone(timezone.utc).isoformat(),
                event.media_type,
                event.trakt_id,
                event.title_key,
                event.runtime_min,
                int(event.is_rewatch),
                event.show_trakt_id,
                event.season_number,
                event.episode_number,
                event.year,
                event.title,
                event.show_title,
            )
            for event in events
        ]
        if not rows:
            return

        self._conn.executemany(
            """
            INSERT INTO processed_events(
                history_id,
                watched_at,
                media_type,
                trakt_id,
                title_key,
                runtime_min,
                is_rewatch,
                show_trakt_id,
                season_number,
                episode_number,
                year,
                title,
                show_title
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(history_id) DO NOTHING
            """,
            rows,
        )
        self._conn.commit()

    def record_dead_letter(self, history_id: int | None, payload: dict, error: str) -> None:
        self._conn.execute(
            """
            INSERT INTO dead_letters(history_id, payload_json, error, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                history_id,
                json.dumps(payload, ensure_ascii=True),
                error,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def fetch_events_in_range(
        self,
        start_inclusive_utc: datetime,
        end_exclusive_utc: datetime,
    ) -> list[ProcessedEventRow]:
        rows = self._conn.execute(
            """
            SELECT history_id, watched_at, media_type, title_key, runtime_min, is_rewatch
            FROM processed_events
            WHERE watched_at >= ? AND watched_at < ?
            ORDER BY watched_at ASC, history_id ASC
            """,
            (
                start_inclusive_utc.astimezone(timezone.utc).isoformat(),
                end_exclusive_utc.astimezone(timezone.utc).isoformat(),
            ),
        ).fetchall()

        return [_row_to_processed_event(row) for row in rows]

    def fetch_watch_events_in_range(
        self,
        start_inclusive_utc: datetime,
        end_exclusive_utc: datetime,
    ) -> list[WatchEvent]:
        rows = self._conn.execute(
            """
            SELECT
                history_id,
                watched_at,
                media_type,
                trakt_id,
                show_trakt_id,
                season_number,
                episode_number,
                runtime_min,
                year,
                title,
                show_title,
                is_rewatch
            FROM processed_events
            WHERE watched_at >= ? AND watched_at < ?
            ORDER BY watched_at ASC, history_id ASC
            """,
            (
                start_inclusive_utc.astimezone(timezone.utc).isoformat(),
                end_exclusive_utc.astimezone(timezone.utc).isoformat(),
            ),
        ).fetchall()

        return [_row_to_watch_event(row) for row in rows]

    def delete_processed_history_ids(self, history_ids: set[int]) -> list[ProcessedEventRow]:
        ids = tuple(sorted(history_ids))
        if not ids:
            return []

        placeholders = _build_placeholders(len(ids))
        rows = self._conn.execute(
            f"""
            SELECT history_id, watched_at, media_type, title_key, runtime_min, is_rewatch
            FROM processed_events
            WHERE history_id IN ({placeholders})
            """,
            ids,
        ).fetchall()

        self._conn.execute(
            f"DELETE FROM processed_events WHERE history_id IN ({placeholders})",
            ids,
        )
        self._conn.commit()

        return [_row_to_processed_event(row) for row in rows]


def _build_placeholders(count: int) -> str:
    return ",".join(["?"] * count)


def _row_to_processed_event(row: sqlite3.Row) -> ProcessedEventRow:
    return ProcessedEventRow(
        history_id=int(row["history_id"]),
        watched_at=_parse_datetime(str(row["watched_at"])),
        media_type=str(row["media_type"]),
        title_key=str(row["title_key"]),
        runtime_min=float(row["runtime_min"]),
        is_rewatch=bool(int(row["is_rewatch"])),
    )


def _row_to_watch_event(row: sqlite3.Row) -> WatchEvent:
    title_raw = row["title"]
    show_title_raw = row["show_title"]
    return WatchEvent(
        history_id=int(row["history_id"]),
        watched_at=_parse_datetime(str(row["watched_at"])),
        media_type=str(row["media_type"]),
        trakt_id=int(row["trakt_id"]),
        show_trakt_id=_int_or_none(row["show_trakt_id"]),
        season_number=_int_or_none(row["season_number"]),
        episode_number=_int_or_none(row["episode_number"]),
        runtime_min=float(row["runtime_min"]),
        year=_int_or_none(row["year"]),
        title=str(title_raw) if title_raw else "Unknown",
        show_title=str(show_title_raw) if show_title_raw else None,
        is_rewatch=bool(int(row["is_rewatch"])),
    )


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _parse_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)
