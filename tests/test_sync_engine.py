from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trakt_tracker.config import Settings
from trakt_tracker.models import WatchEvent
from trakt_tracker.state_store import StateStore
from trakt_tracker.sync_engine import SyncEngine


class FakeTraktClient:
    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = payloads

    def iter_history(self, start_at, end_at, per_page: int = 100, page_callback=None):
        del start_at, end_at, per_page, page_callback
        yield from self._payloads

    def current_refresh_token(self) -> str:
        return "rotated-refresh-token"


class FakeInfluxWriter:
    def __init__(self) -> None:
        self.raw_writes: list[list[WatchEvent]] = []
        self.aggregate_writes = 0
        self.deleted_ranges = 0

    def write_watch_events(self, events: list[WatchEvent]) -> None:
        self.raw_writes.append(events)

    def write_daily_aggregates(self, aggregates) -> None:
        if aggregates:
            self.aggregate_writes += 1

    def delete_watch_events_range(self, start_inclusive_utc, end_exclusive_utc) -> None:
        del start_inclusive_utc, end_exclusive_utc
        self.deleted_ranges += 1



def _settings(db_path: Path) -> Settings:
    return Settings(
        trakt_client_id="client",
        trakt_client_secret="secret",
        trakt_refresh_token="refresh",
        trakt_auth_code=None,
        influx_enabled=True,
        influx_url="http://localhost:8086",
        influx_token="token",
        influx_org="org",
        influx_bucket_raw="trakt_raw",
        influx_bucket_agg="trakt_agg",
        sync_cron="0 6,18 * * *",
        reconcile_cron="30 3 * * *",
        timezone="UTC",
        overlap_hours=24,
        reconcile_days=7,
        state_db_path=str(db_path),
        log_level="INFO",
        trakt_max_retries=5,
        trakt_retry_after_margin=0.9,
        trakt_min_request_interval_seconds=0.0,
        running_in_docker=False,
        config_path="",
    )



def test_reconcile_removes_deleted_events_and_rewrites_raw_day(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    store = StateStore(str(db_path))

    watched_at_keep = datetime.now(timezone.utc) - timedelta(days=1, minutes=10)
    watched_at_deleted = datetime.now(timezone.utc) - timedelta(days=1, minutes=5)

    keep_event = WatchEvent(
        history_id=100,
        watched_at=watched_at_keep,
        media_type="movie",
        trakt_id=999,
        show_trakt_id=None,
        season_number=None,
        episode_number=None,
        runtime_min=95.0,
        year=2026,
        title="Example Movie",
        show_title=None,
        is_rewatch=False,
    )
    deleted_event = WatchEvent(
        history_id=101,
        watched_at=watched_at_deleted,
        media_type="episode",
        trakt_id=5001,
        show_trakt_id=7001,
        season_number=1,
        episode_number=1,
        runtime_min=45.0,
        year=2024,
        title="Pilot",
        show_title="Example Show",
        is_rewatch=False,
    )
    store.mark_processed_many([keep_event, deleted_event])

    payloads = [
        {
            "id": 100,
            "type": "movie",
            "watched_at": keep_event.watched_at.isoformat().replace("+00:00", "Z"),
            "rewatched": False,
            "movie": {
                "title": "Example Movie",
                "year": 2026,
                "runtime": 95,
                "ids": {"trakt": 999},
            },
        }
    ]

    fake_trakt = FakeTraktClient(payloads=payloads)
    fake_influx = FakeInfluxWriter()
    settings = _settings(db_path)

    engine = SyncEngine(
        settings=settings,
        trakt_client=fake_trakt,
        influx_writer=fake_influx,
        state_store=store,
        logger=logging.getLogger("test_sync_engine"),
    )

    stats = engine.run_reconcile()

    assert stats["events_deleted"] == 1
    assert stats["days_rewritten_raw"] == 1
    assert store.has_processed(100) is True
    assert store.has_processed(101) is False
    assert fake_influx.deleted_ranges == 1

    rewritten_events = [batch for batch in fake_influx.raw_writes if batch]
    assert len(rewritten_events) == 1
    assert [event.history_id for event in rewritten_events[0]] == [100]

    assert store.get_trakt_refresh_token() == "rotated-refresh-token"

    store.close()
