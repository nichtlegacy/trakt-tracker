from __future__ import annotations

import logging
import pytest

from trakt_tracker.auth import ensure_refresh_token
from trakt_tracker.config import Settings
from trakt_tracker.state_store import StateStore



def _settings(db_path: str) -> Settings:
    return Settings(
        trakt_client_id="client-id",
        trakt_client_secret="client-secret",
        trakt_refresh_token=None,
        trakt_auth_code=None,
        influx_enabled=True,
        influx_url="",
        influx_token="",
        influx_org="",
        influx_bucket_raw="trakt_raw",
        influx_bucket_agg="trakt_agg",
        sync_cron="0 6,18 * * *",
        reconcile_cron="30 3 * * *",
        timezone="UTC",
        overlap_hours=24,
        reconcile_days=7,
        state_db_path=db_path,
        log_level="INFO",
        trakt_max_retries=5,
        trakt_retry_after_margin=0.9,
        trakt_min_request_interval_seconds=0.0,
        running_in_docker=False,
        config_path="",
    )



def test_ensure_refresh_token_prefers_persisted_state(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    store.set_trakt_refresh_token("persisted-token")

    settings = _settings(str(tmp_path / "state.db"))
    token = ensure_refresh_token(
        settings=settings,
        state_store=store,
        logger=logging.getLogger("test_auth"),
        token_exchange=lambda _settings, _code: "new-token",
    )

    assert token == "persisted-token"
    store.close()



def test_ensure_refresh_token_exchanges_auth_code(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    settings = _settings(str(tmp_path / "state.db"))

    token = ensure_refresh_token(
        settings=settings,
        state_store=store,
        logger=logging.getLogger("test_auth"),
        auth_code="auth-code-123",
        token_exchange=lambda _settings, code: f"refresh:{code}",
    )

    assert token == "refresh:auth-code-123"
    assert store.get_trakt_refresh_token() == "refresh:auth-code-123"
    store.close()



def test_ensure_refresh_token_uses_device_flow(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    settings = _settings(str(tmp_path / "state.db"))

    token = ensure_refresh_token(
        settings=settings,
        state_store=store,
        logger=logging.getLogger("test_auth"),
        device_exchange=lambda _settings, _logger: "device-flow-token",
    )

    assert token == "device-flow-token"
    assert store.get_trakt_refresh_token() == "device-flow-token"
    store.close()


def test_ensure_refresh_token_falls_back_to_prompt_if_device_fails(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    settings = _settings(str(tmp_path / "state.db"))

    token = ensure_refresh_token(
        settings=settings,
        state_store=store,
        logger=logging.getLogger("test_auth"),
        device_exchange=lambda _settings, _logger: (_ for _ in ()).throw(RuntimeError("boom")),
        token_exchange=lambda _settings, code: f"fallback:{code}",
        prompt=lambda _message: "manual-code",
        is_tty=True,
    )

    assert token == "fallback:manual-code"
    assert store.get_trakt_refresh_token() == "fallback:manual-code"
    store.close()


def test_ensure_refresh_token_non_interactive_errors_if_all_bootstrap_paths_fail(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    settings = _settings(str(tmp_path / "state.db"))

    with pytest.raises(RuntimeError, match="No Trakt refresh token available"):
        ensure_refresh_token(
            settings=settings,
            state_store=store,
            logger=logging.getLogger("test_auth"),
            device_exchange=lambda _settings, _logger: (_ for _ in ()).throw(RuntimeError("boom")),
            is_tty=False,
        )

    store.close()
