from datetime import datetime, timezone

from trakt_tracker.aggregator import build_daily_aggregates
from trakt_tracker.state_store import ProcessedEventRow


def test_build_daily_aggregates_counts_and_minutes() -> None:
    day_start = datetime(2026, 2, 20, 0, 0, tzinfo=timezone.utc)
    rows = [
        ProcessedEventRow(
            history_id=1,
            watched_at=datetime(2026, 2, 20, 10, 0, tzinfo=timezone.utc),
            media_type="movie",
            title_key="movie:10",
            runtime_min=120.0,
            is_rewatch=False,
        ),
        ProcessedEventRow(
            history_id=2,
            watched_at=datetime(2026, 2, 20, 11, 0, tzinfo=timezone.utc),
            media_type="episode",
            title_key="episode:44:s1:e1",
            runtime_min=45.0,
            is_rewatch=True,
        ),
        ProcessedEventRow(
            history_id=3,
            watched_at=datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc),
            media_type="episode",
            title_key="episode:44:s1:e1",
            runtime_min=45.0,
            is_rewatch=False,
        ),
    ]

    aggregates = build_daily_aggregates(rows=rows, day_start_utc=day_start)

    all_agg = next(row for row in aggregates if row.media_type == "all")
    assert all_agg.events_count == 3
    assert all_agg.unique_titles_count == 2
    assert all_agg.watch_minutes_total == 210.0
    assert all_agg.rewatch_events_count == 1
    assert all_agg.first_watch_events_count == 2

    episode_agg = next(row for row in aggregates if row.media_type == "episode")
    assert episode_agg.events_count == 2
    assert episode_agg.unique_titles_count == 1
