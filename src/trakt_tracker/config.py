from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import tomllib

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


@dataclass(frozen=True)
class Settings:
    trakt_client_id: str
    trakt_client_secret: str
    trakt_refresh_token: str | None
    trakt_auth_code: str | None
    influx_enabled: bool
    influx_url: str
    influx_token: str
    influx_org: str
    influx_bucket_raw: str
    influx_bucket_agg: str
    sync_cron: str
    reconcile_cron: str
    timezone: str
    overlap_hours: int
    reconcile_days: int
    state_db_path: str
    log_level: str
    trakt_max_retries: int
    trakt_retry_after_margin: float
    trakt_min_request_interval_seconds: float
    running_in_docker: bool
    config_path: str


def load_settings(require_influx: bool = True) -> Settings:
    running_in_docker = _env_bool("RUNNING_IN_DOCKER", False)

    # Local development convenience: auto-load .env only outside Docker.
    if not running_in_docker and load_dotenv is not None:
        load_dotenv(override=False)

    config_path = os.getenv("CONFIG_PATH") or _default_config_path(running_in_docker)
    config_values = _load_config(Path(config_path))

    trakt_client_id = _pick_str("TRAKT_CLIENT_ID", "trakt.client_id", config_values)
    trakt_client_secret = _pick_str("TRAKT_CLIENT_SECRET", "trakt.client_secret", config_values)

    if not trakt_client_id:
        raise RuntimeError("Missing required configuration value: TRAKT_CLIENT_ID")
    if not trakt_client_secret:
        raise RuntimeError("Missing required configuration value: TRAKT_CLIENT_SECRET")

    influx_enabled = _pick_bool("ENABLE_INFLUX", "influx.enabled", config_values, default=True)

    influx_url = _pick_str("INFLUX_URL", "influx.url", config_values, default="")
    influx_token = _pick_str("INFLUX_TOKEN", "influx.token", config_values, default="")
    influx_org = _pick_str("INFLUX_ORG", "influx.org", config_values, default="")

    if require_influx and influx_enabled:
        missing = [
            name
            for name, value in (
                ("INFLUX_URL", influx_url),
                ("INFLUX_TOKEN", influx_token),
                ("INFLUX_ORG", influx_org),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required Influx configuration values: {', '.join(missing)}")

    timezone = _pick_str("TIMEZONE", "sync.timezone", config_values, default="Europe/Berlin")
    ZoneInfo(timezone)

    default_state_path = "/data/state.db" if running_in_docker else str(Path.cwd() / ".data" / "state.db")

    return Settings(
        trakt_client_id=trakt_client_id,
        trakt_client_secret=trakt_client_secret,
        trakt_refresh_token=_pick_optional("TRAKT_REFRESH_TOKEN", "trakt.refresh_token", config_values),
        trakt_auth_code=_pick_optional("TRAKT_AUTH_CODE", "trakt.auth_code", config_values),
        influx_enabled=influx_enabled,
        influx_url=influx_url,
        influx_token=influx_token,
        influx_org=influx_org,
        influx_bucket_raw=_pick_str("INFLUX_BUCKET_RAW", "influx.bucket_raw", config_values, default="trakt_raw"),
        influx_bucket_agg=_pick_str("INFLUX_BUCKET_AGG", "influx.bucket_agg", config_values, default="trakt_agg"),
        sync_cron=_pick_str("SYNC_CRON", "sync.sync_cron", config_values, default="0 6,18 * * *"),
        reconcile_cron=_pick_str("RECONCILE_CRON", "sync.reconcile_cron", config_values, default="30 3 * * *"),
        timezone=timezone,
        overlap_hours=_pick_int("OVERLAP_HOURS", "sync.overlap_hours", config_values, default=24),
        reconcile_days=_pick_int("RECONCILE_DAYS", "sync.reconcile_days", config_values, default=7),
        state_db_path=_pick_str("STATE_DB_PATH", "runtime.state_db_path", config_values, default=default_state_path),
        log_level=_pick_str("LOG_LEVEL", "runtime.log_level", config_values, default="INFO"),
        trakt_max_retries=_pick_int("TRAKT_MAX_RETRIES", "runtime.trakt_max_retries", config_values, default=5),
        trakt_retry_after_margin=_pick_float(
            "TRAKT_RETRY_AFTER_MARGIN",
            "runtime.trakt_retry_after_margin",
            config_values,
            default=0.9,
        ),
        trakt_min_request_interval_seconds=_pick_float(
            "TRAKT_MIN_REQUEST_INTERVAL_SECONDS",
            "runtime.trakt_min_request_interval_seconds",
            config_values,
            default=0.0,
        ),
        running_in_docker=running_in_docker,
        config_path=config_path,
    )


def _default_config_path(running_in_docker: bool) -> str:
    if running_in_docker:
        return "/config/config.toml"
    return str(Path.cwd() / "config.toml")


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        parsed = tomllib.load(handle)
    return _flatten(parsed)


def _flatten(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, dotted))
        else:
            out[dotted] = value
    return out


def _pick_optional(env_key: str, cfg_key: str, cfg: dict[str, Any]) -> str | None:
    env_val = os.getenv(env_key)
    if env_val not in {None, ""}:
        return env_val
    cfg_val = cfg.get(cfg_key)
    if cfg_val in {None, ""}:
        return None
    return str(cfg_val)


def _pick_str(env_key: str, cfg_key: str, cfg: dict[str, Any], default: str | None = None) -> str:
    picked = _pick_optional(env_key, cfg_key, cfg)
    if picked is None:
        return "" if default is None else default
    return picked


def _pick_int(env_key: str, cfg_key: str, cfg: dict[str, Any], default: int) -> int:
    env_val = os.getenv(env_key)
    if env_val not in {None, ""}:
        return int(env_val)
    cfg_val = cfg.get(cfg_key)
    if cfg_val is None:
        return default
    return int(cfg_val)


def _pick_float(env_key: str, cfg_key: str, cfg: dict[str, Any], default: float) -> float:
    env_val = os.getenv(env_key)
    if env_val not in {None, ""}:
        return float(env_val)
    cfg_val = cfg.get(cfg_key)
    if cfg_val is None:
        return default
    return float(cfg_val)


def _pick_bool(env_key: str, cfg_key: str, cfg: dict[str, Any], default: bool) -> bool:
    env_val = os.getenv(env_key)
    if env_val not in {None, ""}:
        return _to_bool(env_val)
    cfg_val = cfg.get(cfg_key)
    if cfg_val is None:
        return default
    return _to_bool(cfg_val)


def _env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value in {None, ""}:
        return default
    return _to_bool(value)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    return lowered in {"1", "true", "yes", "on"}
