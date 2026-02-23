from datetime import datetime, timezone

from trakt_tracker.models import WatchEvent
from trakt_tracker.state_store import StateStore


def test_state_store_cursor_and_dedupe(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    store = StateStore(str(db_path))

    event = WatchEvent(
        history_id=100,
        watched_at=datetime(2026, 2, 21, 20, 0, tzinfo=timezone.utc),
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
    event_2 = WatchEvent(
        history_id=101,
        watched_at=datetime(2026, 2, 21, 21, 0, tzinfo=timezone.utc),
        media_type="episode",
        trakt_id=5001,
        show_trakt_id=7001,
        season_number=2,
        episode_number=5,
        runtime_min=48.0,
        year=2023,
        title="Example Episode",
        show_title="Example Show",
        is_rewatch=True,
    )

    assert store.has_processed(100) is False
    store.mark_processed_many([event, event_2])
    assert store.has_processed(100) is True

    store.set_cursor(event.watched_at, event.history_id)
    watched_at, history_id = store.get_cursor()

    assert watched_at == event.watched_at
    assert history_id == event.history_id

    rows = store.fetch_events_in_range(
        start_inclusive_utc=datetime(2026, 2, 21, 0, 0, tzinfo=timezone.utc),
        end_exclusive_utc=datetime(2026, 2, 22, 0, 0, tzinfo=timezone.utc),
    )
    assert len(rows) == 2
    assert rows[0].history_id == 100

    watch_events = store.fetch_watch_events_in_range(
        start_inclusive_utc=datetime(2026, 2, 21, 0, 0, tzinfo=timezone.utc),
        end_exclusive_utc=datetime(2026, 2, 22, 0, 0, tzinfo=timezone.utc),
    )
    assert len(watch_events) == 2
    assert watch_events[1].show_trakt_id == 7001
    assert watch_events[1].episode_number == 5

    deleted = store.delete_processed_history_ids({101})
    assert len(deleted) == 1
    assert deleted[0].history_id == 101
    assert store.has_processed(101) is False

    assert store.get_trakt_refresh_token() is None
    store.set_trakt_refresh_token("refresh-token-v2")
    assert store.get_trakt_refresh_token() == "refresh-token-v2"

    store.close()
