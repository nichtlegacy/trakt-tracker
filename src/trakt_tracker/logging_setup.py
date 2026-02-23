import logging
import sys

class ColorFormatter(logging.Formatter):
    """Custom formatter with colors for terminal output."""

    COLORS = {
        "DEBUG": "\033[90m",
        "INFO": "\033[97m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[95m",
    }
    RESET = "\033[0m"

    ICONS = {
        "DEBUG": "   ",
        "INFO": " \033[94m>\033[0m ",
        "WARNING": " \033[93m!\033[0m ",
        "ERROR": " \033[91mX\033[0m ",
        "CRITICAL": " \033[95m!!\033[0m ",
    }

    IGNORE_LOGGERS = {"apscheduler.scheduler", "apscheduler.executors.default"}

    def format(self, record):
        if hasattr(record, "name") and record.name in self.IGNORE_LOGGERS and record.levelname == "INFO":
            return None

        msg = record.getMessage()

        if msg == "service_bootstrap_backfill":
            return f"\033[36m🔄\033[0m Starting initial backfill sync..."
        elif msg == "service_bootstrap_incremental":
            return f"\033[36m🔄\033[0m Starting incremental sync..."
        elif msg == "sync_start":
            return None
        elif msg == "sync_finished":
            return None
        elif msg == "service_scheduler_started":
            return f"\033[92m●\033[0m Scheduler started. Monitoring active."
        elif msg == "runtime_docker_mode":
            return f"\033[94m🐳\033[0m Running in Docker mode"
        elif msg == "trakt_auth_ready":
            return f"\033[92m✓\033[0m Trakt authentication ready"
        elif msg == "influx_disabled":
            return f"\033[93m⚠\033[0m InfluxDB is disabled"
        elif msg == "service_shutdown":
            print()
            print("\033[94m" + "-" * 50 + "\033[0m")
            print()
            return f"\033[90m   Stopped gracefully.\033[0m\n"
        elif msg == "backfill_already_completed":
            return f"\033[92m✓\033[0m Backfill already completed, skipping"
        elif msg == "reconcile_hard_deletes_applied":
            deleted = getattr(record, "events_deleted", 0)
            return f"\033[35m🗑️\033[0m Reconcile applied (Deleted: {deleted} events)"
        elif msg == "influx_exported_watch_events":
            count = getattr(record, "count", 0)
            bucket = getattr(record, "bucket", "unknown")
            return f"\033[96m📤\033[0m Exported \033[1m{count}\033[0m raw events to \033[90m{bucket}\033[0m"
        elif msg == "influx_exported_aggregates":
            count = getattr(record, "count", 0)
            bucket = getattr(record, "bucket", "unknown")
            return f"\033[96m📤\033[0m Exported \033[1m{count}\033[0m daily aggregates to \033[90m{bucket}\033[0m"
        elif msg == "auth_refresh_token_missing":
            return f"\033[93m⚠\033[0m Trakt auth: Refresh token missing"
        elif msg == "trakt_auth_failed":
            reason = getattr(record, "reason", "Unknown error")
            return f"\033[91mX\033[0m Trakt Authentication Failed: {reason}\n   \033[93mPlease run 'trakt-tracker --auth' to re-authenticate.\033[0m"
        elif msg == "authenticating":
            return f"\033[36m🔄\033[0m Authenticating with Trakt..."
        elif msg == "authenticated":
            return f"\033[92m✓\033[0m Authenticated successfully"
        elif msg == "auth_code_exchange":
            return f"\033[36m🔄\033[0m Exchanging auth code..."

        icon = self.ICONS.get(record.levelname, "   ")
        return f"{icon}{msg}"


class NoNoneFilter(logging.Filter):
    def filter(self, record):
        formatted = ColorFormatter().format(record)
        return formatted is not None

def configure_logging(level: str) -> None:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColorFormatter())
    console_handler.addFilter(NoNoneFilter())

    logging.root.handlers = []
    logging.root.addHandler(console_handler)
    logging.root.setLevel(getattr(logging, level.upper(), logging.INFO))

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
