from __future__ import annotations

from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from trakt_tracker.aggregator import DailyAggregate
from trakt_tracker.config import Settings
from trakt_tracker.models import WatchEvent


class InfluxWriter:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._settings = settings
        self._client = InfluxDBClient(
            url=settings.influx_url,
            token=settings.influx_token,
            org=settings.influx_org,
        )
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        self._delete_api = self._client.delete_api()
        self._logger = logger

    def close(self) -> None:
        self._client.close()

    def ping(self) -> bool:
        return self._client.ping()

    def write_watch_events(self, events: list[WatchEvent]) -> None:
        if not events:
            return

        points: list[Point] = []
        for event in events:
            point = (
                Point("watch_event")
                .tag("media_type", event.media_type)
                .tag("source", "trakt")
                .tag("is_rewatch", "true" if event.is_rewatch else "false")
                .field("history_id", event.history_id)
                .field("trakt_id", event.trakt_id)
                .field("runtime_min", float(event.runtime_min))
                .field("title", event.title)
                .field("ingested_at", _iso_utc_now())
                .time(event.watched_at.astimezone(timezone.utc), WritePrecision.S)
            )

            if event.show_trakt_id is not None:
                point = point.field("show_trakt_id", event.show_trakt_id)
            if event.season_number is not None:
                point = point.field("season_number", event.season_number)
            if event.episode_number is not None:
                point = point.field("episode_number", event.episode_number)
            if event.year is not None:
                point = point.field("year", event.year)
            if event.show_title:
                point = point.field("show_title", event.show_title)

            points.append(point)

        self._write_api.write(
            bucket=self._settings.influx_bucket_raw,
            org=self._settings.influx_org,
            record=points,
        )
        self._logger.info("influx_exported_watch_events", extra={"count": len(points), "bucket": self._settings.influx_bucket_raw})

    def write_daily_aggregates(self, aggregates: list[DailyAggregate]) -> None:
        if not aggregates:
            return

        points: list[Point] = []
        for aggregate in aggregates:
            points.append(
                Point("watch_daily")
                .tag("media_type", aggregate.media_type)
                .field("events_count", aggregate.events_count)
                .field("unique_titles_count", aggregate.unique_titles_count)
                .field("watch_minutes_total", float(aggregate.watch_minutes_total))
                .field("rewatch_events_count", aggregate.rewatch_events_count)
                .field("first_watch_events_count", aggregate.first_watch_events_count)
                .time(aggregate.day_start_utc.astimezone(timezone.utc), WritePrecision.S)
            )

        chunk_size = 2500
        for i in range(0, len(points), chunk_size):
            chunk = points[i:i + chunk_size]
            self._write_api.write(
                bucket=self._settings.influx_bucket_agg,
                org=self._settings.influx_org,
                record=chunk,
            )
            self._logger.info("influx_exported_aggregates", extra={"count": len(chunk), "bucket": self._settings.influx_bucket_agg})

    def delete_watch_events_range(
        self,
        start_inclusive_utc: datetime,
        end_exclusive_utc: datetime,
    ) -> None:
        if end_exclusive_utc <= start_inclusive_utc:
            return
        self._delete_api.delete(
            start=_to_rfc3339(start_inclusive_utc),
            stop=_to_rfc3339(end_exclusive_utc),
            predicate='_measurement="watch_event"',
            bucket=self._settings.influx_bucket_raw,
            org=self._settings.influx_org,
        )


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
