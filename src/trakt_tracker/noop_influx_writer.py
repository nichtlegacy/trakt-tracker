from __future__ import annotations

from datetime import datetime

from trakt_tracker.aggregator import DailyAggregate
from trakt_tracker.models import WatchEvent


class NoopInfluxWriter:
    """
    Drop-in replacement for InfluxWriter used for local scraping/state tests.
    """

    def close(self) -> None:
        return

    def ping(self) -> bool:
        return True

    def write_watch_events(self, events: list[WatchEvent]) -> None:
        del events
        return

    def write_daily_aggregates(self, aggregates: list[DailyAggregate]) -> None:
        del aggregates
        return

    def delete_watch_events_range(
        self,
        start_inclusive_utc: datetime,
        end_exclusive_utc: datetime,
    ) -> None:
        del start_inclusive_utc, end_exclusive_utc
        return
