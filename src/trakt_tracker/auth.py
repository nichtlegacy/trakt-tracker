from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from urllib.parse import urlencode

import httpx

from trakt_tracker.config import Settings
from trakt_tracker.state_store import StateStore

TRAKT_AUTHORIZE_URL = "https://trakt.tv/oauth/authorize"
TRAKT_TOKEN_URL = "https://api.trakt.tv/oauth/token"
TRAKT_DEVICE_CODE_URL = "https://api.trakt.tv/oauth/device/code"
TRAKT_DEVICE_TOKEN_URL = "https://api.trakt.tv/oauth/device/token"
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


def ensure_refresh_token(
    settings: Settings,
    state_store: StateStore,
    logger: logging.Logger,
    auth_code: str | None = None,
    token_exchange: Callable[[Settings, str], str] | None = None,
    device_exchange: Callable[[Settings, logging.Logger], str] | None = None,
    prompt: Callable[[str], str] = input,
    is_tty: bool | None = None,
) -> str:
    persisted = state_store.get_trakt_refresh_token()
    if persisted:
        return persisted

    if settings.trakt_refresh_token:
        state_store.set_trakt_refresh_token(settings.trakt_refresh_token)
        return settings.trakt_refresh_token

    code = auth_code or settings.trakt_auth_code
    exchange = token_exchange or exchange_auth_code_for_refresh_token
    if code:
        refresh_token = exchange(settings, code.strip())
        state_store.set_trakt_refresh_token(refresh_token)
        return refresh_token

    device_fn = device_exchange or exchange_device_flow_for_refresh_token
    try:
        refresh_token = device_fn(settings, logger)
        state_store.set_trakt_refresh_token(refresh_token)
        logger.info("trakt_oauth_device_bootstrap_completed")
        return refresh_token
    except Exception:
        logger.exception("trakt_oauth_device_bootstrap_failed")

    interactive = sys.stdin.isatty() if is_tty is None else is_tty
    authorize_url = build_authorize_url(settings.trakt_client_id)
    if not interactive:
        raise RuntimeError(
            "No Trakt refresh token available. Device flow failed. "
            "Provide TRAKT_REFRESH_TOKEN or TRAKT_AUTH_CODE. "
            f"Authorization URL: {authorize_url}"
        )

    print("Trakt authorization is required for first startup.")
    print("1) Open this URL in your browser:")
    print(authorize_url)
    print("2) Approve access and copy the authorization code.")
    entered_code = prompt("Paste Trakt authorization code: ").strip()
    if not entered_code:
        raise RuntimeError("No Trakt authorization code provided.")

    refresh_token = exchange(settings, entered_code)
    state_store.set_trakt_refresh_token(refresh_token)
    logger.info("trakt_oauth_bootstrap_completed")
    return refresh_token


def build_authorize_url(client_id: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": OOB_REDIRECT_URI,
        }
    )
    return f"{TRAKT_AUTHORIZE_URL}?{query}"


def exchange_auth_code_for_refresh_token(settings: Settings, auth_code: str) -> str:
    with httpx.Client(timeout=30.0) as http:
        response = http.post(
            TRAKT_TOKEN_URL,
            json={
                "code": auth_code,
                "client_id": settings.trakt_client_id,
                "client_secret": settings.trakt_client_secret,
                "redirect_uri": OOB_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/json"},
        )

    if response.status_code >= 400:
        detail = response.text.strip()[:512] if response.text else "<empty>"
        raise RuntimeError(f"Trakt OAuth code exchange failed status={response.status_code}, detail={detail}")

    payload = response.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Trakt OAuth code exchange did not return refresh_token.")
    return str(refresh_token)


def exchange_device_flow_for_refresh_token(settings: Settings, logger: logging.Logger) -> str:
    with httpx.Client(timeout=30.0) as http:
        device_response = http.post(
            TRAKT_DEVICE_CODE_URL,
            json={"client_id": settings.trakt_client_id},
            headers={"Content-Type": "application/json"},
        )
        if device_response.status_code >= 400:
            detail = device_response.text.strip()[:512] if device_response.text else "<empty>"
            raise RuntimeError(
                f"Trakt device code request failed status={device_response.status_code}, detail={detail}"
            )

        payload = device_response.json()
        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_url = payload.get("verification_url")
        expires_in = int(payload.get("expires_in", 600))
        interval = int(payload.get("interval", 5))

        if not device_code or not user_code or not verification_url:
            raise RuntimeError("Trakt device code response missing required fields.")

        logger.warning(
            "trakt_device_authorization_required",
            extra={"verification_url": verification_url, "user_code": user_code, "expires_in": expires_in},
        )
        print("Trakt authorization required.")
        print(f"Open: {verification_url}")
        print(f"Enter code: {user_code}")

        deadline = time.monotonic() + expires_in
        poll_interval = max(1, interval)
        use_tty_progress = sys.stdout.isatty()
        poll_attempt = 0

        while time.monotonic() < deadline:
            poll_attempt += 1
            _render_device_waiting_status(
                enabled=use_tty_progress,
                poll_attempt=poll_attempt,
                poll_interval=poll_interval,
                seconds_left=int(max(0, deadline - time.monotonic())),
            )
            time.sleep(poll_interval)
            token_response = http.post(
                TRAKT_DEVICE_TOKEN_URL,
                json={
                    "code": device_code,
                    "client_id": settings.trakt_client_id,
                    "client_secret": settings.trakt_client_secret,
                },
                headers={"Content-Type": "application/json"},
            )

            if token_response.status_code == 200:
                token_payload = token_response.json()
                refresh_token = token_payload.get("refresh_token")
                if not refresh_token:
                    raise RuntimeError("Trakt device token response missing refresh_token.")
                _finish_device_waiting_status(enabled=use_tty_progress, message="Authorization confirmed.")
                return str(refresh_token)

            error_payload = {}
            try:
                error_payload = token_response.json()
            except Exception:  # noqa: BLE001
                pass
            error_code = str(error_payload.get("error", "unknown_error"))
            if error_code == "unknown_error":
                header_error = token_response.headers.get("X-Error-Type") or token_response.headers.get("x-error-type")
                if header_error:
                    error_code = str(header_error)
            detail = token_response.text.strip()[:512] if token_response.text else "<empty>"
            logger.debug(
                "trakt_device_token_poll",
                extra={"status": token_response.status_code, "error": error_code, "detail": detail},
            )

            if token_response.status_code == 400 and error_code in {"authorization_pending", "unknown_error"}:
                # Some clients return 400 with empty/no error body until user approves.
                continue
            if token_response.status_code == 400 and error_code == "slow_down":
                poll_interval = min(poll_interval + 5, 30)
                logger.info("trakt_device_token_poll_slow_down", extra={"poll_interval_s": poll_interval})
                continue
            if token_response.status_code == 400 and error_code in {"expired_token", "access_denied"}:
                _finish_device_waiting_status(enabled=use_tty_progress, message="Authorization failed.")
                raise RuntimeError(f"Trakt device authorization failed: {error_code}")

            _finish_device_waiting_status(enabled=use_tty_progress, message="Authorization failed.")
            raise RuntimeError(
                f"Trakt device token polling failed status={token_response.status_code}, detail={detail}"
            )

    _finish_device_waiting_status(enabled=use_tty_progress, message="Authorization timed out.")
    raise RuntimeError("Trakt device authorization timed out before approval.")


def _render_device_waiting_status(enabled: bool, poll_attempt: int, poll_interval: int, seconds_left: int) -> None:
    if not enabled:
        return
    spinner = "|/-\\"[(poll_attempt - 1) % 4]
    line = (
        "Waiting for Trakt approval "
        f"{spinner} (next check in {poll_interval}s, expires in {_format_mm_ss(seconds_left)})"
    )
    sys.stdout.write(f"\r{line}")
    sys.stdout.flush()


def _finish_device_waiting_status(enabled: bool, message: str) -> None:
    if not enabled:
        return
    clear_width = 96
    sys.stdout.write("\r" + (" " * clear_width) + "\r")
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _format_mm_ss(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"{minutes:02d}:{remainder:02d}"
