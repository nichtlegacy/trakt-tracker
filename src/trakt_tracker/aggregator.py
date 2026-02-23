from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trakt_tracker.state_store import ProcessedEventRow


@dataclass(frozen=True)
class DailyAggregate:
    day_start_utc: datetime
    media_type: str
    events_count: int
    unique_titles_count: int
    watch_minutes_total: float
    rewatch_events_count: int
    first_watch_events_count: int



def build_daily_aggregates(
    rows: list[ProcessedEventRow],
    day_start_utc: datetime,
) -> list[DailyAggregate]:
    outputs: list[DailyAggregate] = []

    for media_type in ("all", "movie", "episode"):
        scoped_rows = rows if media_type == "all" else [row for row in rows if row.media_type == media_type]
        if not scoped_rows:
            continue

        events_count = len(scoped_rows)
        unique_titles_count = len({row.title_key for row in scoped_rows})
        watch_minutes_total = round(sum(row.runtime_min for row in scoped_rows), 2)
        rewatch_events_count = sum(1 for row in scoped_rows if row.is_rewatch)

        outputs.append(
            DailyAggregate(
                day_start_utc=day_start_utc,
                media_type=media_type,
                events_count=events_count,
                unique_titles_count=unique_titles_count,
                watch_minutes_total=watch_minutes_total,
                rewatch_events_count=rewatch_events_count,
                first_watch_events_count=events_count - rewatch_events_count,
            )
        )

    return outputs
