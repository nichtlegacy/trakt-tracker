from __future__ import annotations

from datetime import datetime, timezone

from trakt_tracker.aggregator import DailyAggregate
from trakt_tracker.models import WatchEvent
from trakt_tracker.noop_influx_writer import NoopInfluxWriter



def test_noop_influx_writer_methods_are_safe() -> None:
    writer = NoopInfluxWriter()

    event = WatchEvent(
        history_id=1,
        watched_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        media_type="movie",
        trakt_id=123,
        show_trakt_id=None,
        season_number=None,
        episode_number=None,
        runtime_min=100.0,
        year=2026,
        title="Movie",
        show_title=None,
        is_rewatch=False,
    )

    aggregate = DailyAggregate(
        day_start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        media_type="all",
        events_count=1,
        unique_titles_count=1,
        watch_minutes_total=100.0,
        rewatch_events_count=0,
        first_watch_events_count=1,
    )

    writer.write_watch_events([event])
    writer.write_daily_aggregates([aggregate])
    writer.delete_watch_events_range(
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc),
    )
    writer.close()
