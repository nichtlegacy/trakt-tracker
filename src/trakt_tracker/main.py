from __future__ import annotations

import argparse
import asyncio
import logging
import platform
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from trakt_tracker.auth import ensure_refresh_token
from trakt_tracker.config import Settings, load_settings
from trakt_tracker.influx_writer import InfluxWriter
from trakt_tracker.noop_influx_writer import NoopInfluxWriter
from trakt_tracker.state_store import StateStore
from trakt_tracker.sync_engine import SyncEngine
from trakt_tracker.trakt_client import TraktClient


def main() -> None:
    _configure_event_loop_policy_for_windows()
    args = _parse_args()
    settings = load_settings(require_influx=not args.auth and not args.no_influx)
    _configure_logging(settings.log_level)
    logger = logging.getLogger("trakt_tracker")

    if settings.running_in_docker:
        logger.info(
            "runtime_docker_mode",
            extra={"config_path": settings.config_path, "state_db_path": settings.state_db_path},
        )

    if args.reset_state:
        import os
        logger.warning(f"\033[93m⚠\033[0m Resetting local state database: {settings.state_db_path}")
        if os.path.exists(settings.state_db_path):
            try:
                os.remove(settings.state_db_path)
            except OSError as e:
                logger.error(f"Failed to remove state database: {e}")

    state_store = StateStore(settings.state_db_path)
    trakt_client: TraktClient | None = None
    influx_writer: InfluxWriter | NoopInfluxWriter | None = None
    try:
        refresh_token = ensure_refresh_token(
            settings=settings,
            state_store=state_store,
            logger=logger,
            auth_code=args.auth_code,
        )
        if args.auth:
            logger.info("trakt_auth_ready")
            return

        trakt_client = TraktClient(
            settings=settings,
            logger=logger,
            refresh_token_override=refresh_token,
        )

        use_influx = settings.influx_enabled and not args.no_influx
        if not use_influx:
            reason = "flag" if args.no_influx else "config"
            logger.info("influx_disabled", extra={"reason": reason})

        if args.test_influx:
            if not use_influx:
                logger.error("Cannot test InfluxDB because it is disabled.")
                sys.exit(1)
            
            logger.info(f"Testing InfluxDB connection to {settings.influx_url}...")
            test_writer = InfluxWriter(settings=settings, logger=logger)
            try:
                if test_writer.ping():
                    logger.info("\033[92m✓\033[0m Successfully pinged InfluxDB.")
                else:
                    logger.error("\033[91mX\033[0m Failed to ping InfluxDB.")
                    sys.exit(1)

                # Test writing a dummy point to raw bucket
                from influxdb_client import Point
                import datetime

                logger.info(f"Testing write permissions to raw bucket '{settings.influx_bucket_raw}'...")
                test_point = Point("trakt_tracker_test").field("status", "ok").time(datetime.datetime.now(datetime.timezone.utc))
                test_writer._write_api.write(bucket=settings.influx_bucket_raw, org=settings.influx_org, record=test_point)
                logger.info("\033[92m✓\033[0m Successfully wrote to raw bucket.")

                logger.info(f"Testing write permissions to agg bucket '{settings.influx_bucket_agg}'...")
                test_writer._write_api.write(bucket=settings.influx_bucket_agg, org=settings.influx_org, record=test_point)
                logger.info("\033[92m✓\033[0m Successfully wrote to agg bucket.")
                
                logger.info(f"\033[92m★\033[0m InfluxDB test completed successfully! All permissions look good.")
            except Exception as e:
                logger.error(f"\033[91mX\033[0m InfluxDB test failed: {e}")
                sys.exit(1)
            finally:
                test_writer.close()
            return

        influx_writer = InfluxWriter(settings=settings, logger=logger) if use_influx else NoopInfluxWriter()

        engine = SyncEngine(
            settings=settings,
            trakt_client=trakt_client,
            influx_writer=influx_writer,
            state_store=state_store,
            logger=logger,
        )

        _print_header(settings, trakt_client, influx_writer)

        from trakt_tracker.exceptions import TraktAuthenticationError
        try:
            if args.once:
                _run_once(engine=engine, once_job=args.once, force_backfill=args.force_backfill)
                return

            _run_service(settings=settings, engine=engine, state_store=state_store, logger=logger)
        except TraktAuthenticationError as e:
            logger.error("trakt_auth_failed", extra={"reason": "Refresh token invalid or revoked"})
            state_store.set_trakt_refresh_token("")
            sys.exit(1)
    finally:
        if trakt_client is not None:
            trakt_client.close()
        if influx_writer is not None:
            influx_writer.close()
        state_store.close()


def _run_service(
    settings: Settings,
    engine: SyncEngine,
    state_store: StateStore,
    logger: logging.Logger,
) -> None:
    if not state_store.get_backfill_completed():
        logger.info("service_bootstrap_backfill")
        engine.run_backfill()

    logger.info("service_bootstrap_incremental")
    engine.run_incremental()

    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(
        engine.run_incremental,
        trigger=CronTrigger.from_crontab(settings.sync_cron, timezone=settings.timezone),
        id="incremental_sync",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        engine.run_reconcile,
        trigger=CronTrigger.from_crontab(settings.reconcile_cron, timezone=settings.timezone),
        id="daily_reconcile",
        coalesce=True,
        max_instances=1,
    )

    logger.info(
        "service_scheduler_started",
        extra={"sync_cron": settings.sync_cron, "reconcile_cron": settings.reconcile_cron},
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("service_shutdown")


def _run_once(engine: SyncEngine, once_job: str, force_backfill: bool) -> None:
    if once_job == "backfill":
        engine.run_backfill(force=force_backfill)
        return
    if once_job == "incremental":
        engine.run_incremental()
        return
    if once_job == "reconcile":
        engine.run_reconcile()
        return

    raise RuntimeError(f"Unsupported once job: {once_job}")


def _configure_logging(level: str) -> None:
    from trakt_tracker.logging_setup import configure_logging
    configure_logging(level)

def _print_header(
    settings: Settings,
    trakt_client: TraktClient | None = None,
    influx_writer: InfluxWriter | NoopInfluxWriter | None = None,
) -> None:
    try:
        from trakt_tracker import __version__
    except ImportError:
        __version__ = "1.0.0"

    trakt_user = "Unknown"
    if trakt_client:
        fetched_user = trakt_client.get_username()
        if fetched_user:
            trakt_user = fetched_user
            
    influx_status = "Disabled"
    if settings.influx_enabled:
        influx_status = f"{settings.influx_url}"
        if influx_writer and hasattr(influx_writer, 'ping'):
            try:
                if influx_writer.ping():
                    influx_status += " (\033[92mConnected\033[0m)"
                else:
                    influx_status += " (\033[91mDisconnected\033[0m)"
            except Exception:
                influx_status += " (\033[91mError\033[0m)"

    try:
        from apscheduler.triggers.cron import CronTrigger
        from datetime import datetime, timezone
        sync_trigger = CronTrigger.from_crontab(settings.sync_cron, timezone=settings.timezone)
        next_sync = sync_trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        next_sync_str = next_sync.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        next_sync_str = "Unknown"

    print()
    print("\033[94m" + "=" * 50 + "\033[0m")
    print(f"\033[1m   Trakt Tracker v{__version__}\033[0m")
    print("\033[94m" + "=" * 50 + "\033[0m")
    print()
    print(f"   \033[90mGitHub:\033[0m    https://github.com/nichtlegacy/trakt-tracker")
    print(f"   \033[90mUser:\033[0m      {trakt_user}")
    print(f"   \033[90mInfluxDB:\033[0m  {influx_status}")
    if settings.influx_enabled:
        print(f"   \033[90mBucket:\033[0m    {settings.influx_bucket_raw} (raw) / {settings.influx_bucket_agg} (agg)")
    print(f"   \033[90mSync cron:\033[0m {settings.sync_cron} (Next: {next_sync_str})")
    print()
    print("\033[94m" + "-" * 50 + "\033[0m")
    print()


def _configure_event_loop_policy_for_windows() -> None:
    if platform.system() != "Windows":
        return

    selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy is not None:
        asyncio.set_event_loop_policy(selector_policy())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trakt -> InfluxDB tracker")
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Run Trakt OAuth bootstrap only and exit.",
    )
    parser.add_argument(
        "--auth-code",
        help="Optional Trakt OAuth code for non-interactive bootstrap.",
    )
    parser.add_argument(
        "--no-influx",
        action="store_true",
        help="Run sync/state logic without writing to InfluxDB.",
    )
    parser.add_argument(
        "--once",
        choices=["backfill", "incremental", "reconcile"],
        help="Run one job and exit.",
    )
    parser.add_argument(
        "--force-backfill",
        action="store_true",
        help="Force backfill even if state indicates completed.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Delete the local state database before starting.",
    )
    parser.add_argument(
        "--test-influx",
        action="store_true",
        help="Test InfluxDB connection and bucket permissions.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
