from __future__ import annotations

import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from trakt_tracker.aggregator import build_daily_aggregates
from trakt_tracker.config import Settings
from trakt_tracker.influx_writer import InfluxWriter
from trakt_tracker.models import WatchEvent, parse_watch_event
from trakt_tracker.state_store import StateStore
from trakt_tracker.trakt_client import TraktClient


class SyncEngine:
    def __init__(
        self,
        settings: Settings,
        trakt_client: TraktClient,
        influx_writer: InfluxWriter,
        state_store: StateStore,
        logger: logging.Logger,
    ) -> None:
        self._settings = settings
        self._trakt = trakt_client
        self._influx = influx_writer
        self._state = state_store
        self._logger = logger
        self._timezone = ZoneInfo(settings.timezone)

    def run_backfill(self, force: bool = False) -> dict:
        if self._state.get_backfill_completed() and not force:
            self._logger.info("backfill_already_completed")
            return {"status": "skipped", "reason": "already_completed"}

        try:
            stats, _, _ = self._run_sync_window(
                job_name="backfill",
                start_at=None,
                end_at=None,
                update_cursor=True,
            )
            self._state.set_backfill_completed(True)
            return stats
        finally:
            self._persist_trakt_refresh_token()

    def run_incremental(self) -> dict:
        cursor_watched_at, _ = self._state.get_cursor()

        if cursor_watched_at:
            start_at = cursor_watched_at - timedelta(hours=self._settings.overlap_hours)
        else:
            start_at = None

        try:
            stats, _, _ = self._run_sync_window(
                job_name="incremental",
                start_at=start_at,
                end_at=None,
                update_cursor=True,
            )
            return stats
        finally:
            self._persist_trakt_refresh_token()

    def run_reconcile(self) -> dict:
        end_at = datetime.now(timezone.utc)
        start_at = end_at - timedelta(days=self._settings.reconcile_days)

        try:
            stats, _, remote_history_ids = self._run_sync_window(
                job_name="reconcile",
                start_at=start_at,
                end_at=end_at,
                update_cursor=False,
                collect_remote_history_ids=True,
            )

            deleted_events = 0
            deleted_days_count = 0
            parse_errors = int(stats.get("parse_errors", 0))
            if parse_errors == 0 and remote_history_ids is not None:
                deleted_events, deleted_days = self._reconcile_hard_deletes(
                    start_at=start_at,
                    end_at=end_at,
                    remote_history_ids=remote_history_ids,
                )
                deleted_days_count = len(deleted_days)
            elif parse_errors > 0:
                self._logger.warning(
                    "reconcile_skip_hard_delete_due_parse_errors",
                    extra={"parse_errors": parse_errors},
                )

            stats["events_deleted"] = deleted_events
            stats["days_rewritten_raw"] = deleted_days_count

            # Rebuild aggregates for the full rolling window to keep dashboard values deterministic.
            self._rebuild_aggregates_for_days(self._recent_local_days(self._settings.reconcile_days))
            return stats
        finally:
            self._persist_trakt_refresh_token()

    def _run_sync_window(
        self,
        job_name: str,
        start_at: datetime | None,
        end_at: datetime | None,
        update_cursor: bool,
        collect_remote_history_ids: bool = False,
    ) -> tuple[dict, set[date], set[int] | None]:
        started = datetime.now(timezone.utc)
        fetched = 0
        inserted = 0
        duplicates = 0
        parse_errors = 0

        batch: list[WatchEvent] = []
        affected_days: set[date] = set()
        cursor_candidate: WatchEvent | None = None
        remote_history_ids = set() if collect_remote_history_ids else None
        progress = _SyncProgress(job_name=job_name)

        self._logger.info(
            "sync_start",
            extra={
                "job_name": job_name,
                "start_at": _safe_iso(start_at),
                "end_at": _safe_iso(end_at),
            },
        )

        for payload in self._trakt.iter_history(
            start_at=start_at,
            end_at=end_at,
            page_callback=progress.on_page_loaded,
        ):
            fetched += 1

            try:
                event = parse_watch_event(payload)
            except Exception as error:  # noqa: BLE001
                parse_errors += 1
                history_id = payload.get("id") if isinstance(payload, dict) else None
                self._state.record_dead_letter(history_id=history_id, payload=payload, error=str(error))
                continue

            if remote_history_ids is not None:
                remote_history_ids.add(event.history_id)

            cursor_candidate = _latest_event(cursor_candidate, event)

            if self._state.has_processed(event.history_id):
                duplicates += 1
                continue

            batch.append(event)
            affected_days.add(event.watched_at.astimezone(self._timezone).date())

            if len(batch) >= 250:
                self._flush_batch(batch)
                inserted += len(batch)
                batch.clear()

        if batch:
            self._flush_batch(batch)
            inserted += len(batch)

        if affected_days:
            self._rebuild_aggregates_for_days(affected_days)

        if update_cursor and cursor_candidate is not None:
            self._state.set_cursor(cursor_candidate.watched_at, cursor_candidate.history_id)

        finished = datetime.now(timezone.utc)
        duration_ms = int((finished - started).total_seconds() * 1000)
        self._state.set_state("last_successful_run", finished.isoformat())

        stats = {
            "job_name": job_name,
            "status": "ok",
            "events_fetched": fetched,
            "events_inserted": inserted,
            "duplicates_skipped": duplicates,
            "parse_errors": parse_errors,
            "duration_ms": duration_ms,
        }
        progress.finish(stats)
        self._logger.info("sync_finished", extra=stats)
        return stats, affected_days, remote_history_ids

    def _flush_batch(self, events: list[WatchEvent]) -> None:
        self._influx.write_watch_events(events)
        self._state.mark_processed_many(events)

    def _rebuild_aggregates_for_days(self, days: set[date]) -> None:
        all_aggregates = []
        for local_day in sorted(days):
            day_start_local = datetime.combine(local_day, time.min, tzinfo=self._timezone)
            day_end_local = day_start_local + timedelta(days=1)

            day_start_utc = day_start_local.astimezone(timezone.utc)
            day_end_utc = day_end_local.astimezone(timezone.utc)
            rows = self._state.fetch_events_in_range(day_start_utc, day_end_utc)

            aggregates = build_daily_aggregates(rows=rows, day_start_utc=day_start_utc)
            all_aggregates.extend(aggregates)

        if all_aggregates:
            self._influx.write_daily_aggregates(all_aggregates)

    def _recent_local_days(self, days: int) -> set[date]:
        now_local = datetime.now(timezone.utc).astimezone(self._timezone)
        return {now_local.date() - timedelta(days=offset) for offset in range(days)}

    def _reconcile_hard_deletes(
        self,
        start_at: datetime,
        end_at: datetime,
        remote_history_ids: set[int],
    ) -> tuple[int, set[date]]:
        local_rows = self._state.fetch_events_in_range(start_at, end_at)
        local_history_ids = {row.history_id for row in local_rows}
        missing_history_ids = local_history_ids - remote_history_ids
        if not missing_history_ids:
            return 0, set()

        removed_rows = self._state.delete_processed_history_ids(missing_history_ids)
        affected_days = {row.watched_at.astimezone(self._timezone).date() for row in removed_rows}
        if affected_days:
            self._rewrite_raw_events_for_days(affected_days)

        self._logger.info(
            "reconcile_hard_deletes_applied",
            extra={
                "window_start": start_at.isoformat(),
                "window_end": end_at.isoformat(),
                "events_deleted": len(removed_rows),
                "days_rewritten_raw": len(affected_days),
            },
        )
        return len(removed_rows), affected_days

    def _rewrite_raw_events_for_days(self, days: set[date]) -> None:
        for local_day in sorted(days):
            day_start_local = datetime.combine(local_day, time.min, tzinfo=self._timezone)
            day_end_local = day_start_local + timedelta(days=1)

            day_start_utc = day_start_local.astimezone(timezone.utc)
            day_end_utc = day_end_local.astimezone(timezone.utc)

            self._influx.delete_watch_events_range(day_start_utc, day_end_utc)
            day_events = self._state.fetch_watch_events_in_range(day_start_utc, day_end_utc)
            self._influx.write_watch_events(day_events)

    def _persist_trakt_refresh_token(self) -> None:
        token = self._trakt.current_refresh_token()
        existing = self._state.get_trakt_refresh_token()
        if token and token != existing:
            self._state.set_trakt_refresh_token(token)



def _latest_event(left: WatchEvent | None, right: WatchEvent) -> WatchEvent:
    if left is None:
        return right
    if right.watched_at > left.watched_at:
        return right
    if right.watched_at == left.watched_at and right.history_id > left.history_id:
        return right
    return left



def _safe_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class _SyncProgress:
    def __init__(self, job_name: str) -> None:
        self._job_name = job_name
        self._enabled = sys.stdout.isatty()
        self._last_rendered_len = 0
        self._last_page = 0

    def on_page_loaded(self, page: int, page_count: int | None, total_items: int | None) -> None:
        self._last_page = page
        if not self._enabled:
            return

        items_str = f" ({total_items} items total)" if total_items is not None else ""

        if page_count is not None and page_count > 0:
            ratio = min(1.0, page / page_count)
            width = 24
            filled = max(0, min(width, int(round(ratio * width))))
            bar = ("#" * filled) + ("-" * (width - filled))
            line = f"   \033[90m↳\033[0m \033[36m{self._job_name}\033[0m: [{bar}] page {page}/{page_count}{items_str}"
        else:
            spinner = "|/-\\"[(page - 1) % 4]
            line = f"   \033[90m↳\033[0m \033[36m{self._job_name}\033[0m: {spinner} page {page}{items_str}"
        self._render(line)

    def finish(self, stats: dict) -> None:
        if not self._enabled:
            return
        self._clear_current_line()
        message = (
            f"   \033[90m↳\033[0m \033[92m{self._job_name} finished\033[0m | pages={self._last_page}, "
            f"fetched={stats['events_fetched']}, inserted={stats['events_inserted']}, "
            f"dup={stats['duplicates_skipped']}, err={stats['parse_errors']}"
        )
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()

    def _render(self, line: str) -> None:
        padded = line
        if self._last_rendered_len > len(line):
            padded = line + (" " * (self._last_rendered_len - len(line)))
        sys.stdout.write(f"\r{padded}")
        sys.stdout.flush()
        self._last_rendered_len = len(line)

    def _clear_current_line(self) -> None:
        if self._last_rendered_len <= 0:
            return
        sys.stdout.write("\r" + (" " * self._last_rendered_len) + "\r")
        sys.stdout.flush()
        self._last_rendered_len = 0
