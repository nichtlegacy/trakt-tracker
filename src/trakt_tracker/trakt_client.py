from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Generator

import httpx

from trakt_tracker.config import Settings


class TraktClient:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        refresh_token_override: str | None = None,
    ) -> None:
        self._settings = settings
        self._logger = logger
        self._http = httpx.Client(base_url="https://api.trakt.tv", timeout=30.0)

        self._refresh_token = refresh_token_override if refresh_token_override is not None else settings.trakt_refresh_token
        self._access_token: str | None = None
        self._access_token_expires_at: datetime = datetime.now(timezone.utc)
        self._last_request_monotonic: float | None = None

    def close(self) -> None:
        self._http.close()

    def current_refresh_token(self) -> str | None:
        return self._refresh_token

    def get_username(self) -> str | None:
        try:
            response = self._request("GET", "/users/settings")
            return response.json().get("user", {}).get("username")
        except Exception as e:
            self._logger.debug(f"Failed to fetch Trakt username: {e}")
            return None

    def iter_history(
        self,
        start_at: datetime | None,
        end_at: datetime | None,
        per_page: int = 100,
        page_callback: Callable[[int, int | None, int | None], None] | None = None,
    ) -> Generator[dict, None, None]:
        page = 1

        while True:
            params = {
                "page": page,
                "limit": per_page,
                "extended": "full",
            }
            if start_at:
                params["start_at"] = _to_trakt_iso(start_at)
            if end_at:
                params["end_at"] = _to_trakt_iso(end_at)

            response = self._request("GET", "/sync/history", params=params)
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError("Unexpected Trakt response format for /sync/history")

            page_count = _parse_page_count(response)
            total_items = _parse_item_count(response)
            if not payload:
                break

            if page_callback is not None:
                page_callback(page, page_count, total_items)

            for item in payload:
                yield item

            if page_count is not None and page >= page_count:
                break
            if page_count is None and len(payload) < per_page:
                break

            page += 1

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_payload: dict | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        retry_count = self._settings.trakt_max_retries if max_retries is None else max_retries

        for attempt in range(retry_count + 1):
            self._ensure_access_token()
            headers = self._headers()
            self._throttle_requests()

            try:
                response = self._http.request(
                    method,
                    path,
                    headers=headers,
                    params=params,
                    json=json_payload,
                )
            except (httpx.RequestError, httpx.TimeoutException, httpx.RemoteProtocolError) as error:
                if attempt >= retry_count:
                    raise RuntimeError(f"Trakt request failed after retries: {error}") from error
                sleep_s = _backoff_seconds(attempt)
                self._logger.warning(
                    "trakt_request_network_retry",
                    extra={"attempt": attempt + 1, "sleep_s": sleep_s, "error": str(error)},
                )
                time.sleep(sleep_s)
                continue

            request_id = response.headers.get("X-Request-Id")

            if response.status_code == 401:
                if attempt >= retry_count:
                    response.raise_for_status()
                self._logger.warning(
                    "trakt_unauthorized_refreshing_token",
                    extra={"attempt": attempt + 1, "request_id": request_id},
                )
                self._refresh_access_token(force=True)
                continue

            if response.status_code == 429:
                if attempt >= retry_count:
                    response.raise_for_status()
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                sleep_s = max(1.0, retry_after + self._settings.trakt_retry_after_margin)
                self._logger.warning(
                    "trakt_rate_limited",
                    extra={"attempt": attempt + 1, "sleep_s": sleep_s, "request_id": request_id},
                )
                time.sleep(sleep_s)
                continue

            if response.status_code >= 500:
                if attempt >= retry_count:
                    response.raise_for_status()
                sleep_s = _backoff_seconds(attempt)
                self._logger.warning(
                    "trakt_server_retry",
                    extra={
                        "attempt": attempt + 1,
                        "sleep_s": sleep_s,
                        "status": response.status_code,
                        "request_id": request_id,
                    },
                )
                time.sleep(sleep_s)
                continue

            if response.status_code >= 400:
                detail = _response_detail(response)
                raise RuntimeError(
                    f"Trakt API request failed status={response.status_code}, request_id={request_id}, detail={detail}"
                )

            return response

        raise RuntimeError("Unreachable request retry state")

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            raise RuntimeError("Missing Trakt access token")
        return {
            "Content-Type": "application/json",
            "User-Agent": "trakt-influx-tracker/0.1",
            "trakt-api-key": self._settings.trakt_client_id,
            "trakt-api-version": "2",
            "Authorization": f"Bearer {self._access_token}",
        }

    def _ensure_access_token(self) -> None:
        if not self._access_token or datetime.now(timezone.utc) >= self._access_token_expires_at:
            self._refresh_access_token(force=False)

    def _throttle_requests(self) -> None:
        min_interval = max(0.0, self._settings.trakt_min_request_interval_seconds)
        if min_interval <= 0:
            return

        now = time.monotonic()
        if self._last_request_monotonic is not None:
            elapsed = now - self._last_request_monotonic
            wait_s = min_interval - elapsed
            if wait_s > 0:
                time.sleep(wait_s)
        self._last_request_monotonic = time.monotonic()

    def _refresh_access_token(self, force: bool) -> None:
        if not force and self._access_token and datetime.now(timezone.utc) < self._access_token_expires_at:
            return
        if not self._refresh_token:
            raise RuntimeError("Trakt refresh token is missing. Run OAuth bootstrap first.")

        try:
            response = self._http.post(
                "https://api.trakt.tv/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._settings.trakt_client_id,
                    "client_secret": self._settings.trakt_client_secret,
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            detail = _response_detail(error.response)
            if status in {400, 401}:
                from trakt_tracker.exceptions import TraktAuthenticationError
                raise TraktAuthenticationError(
                    "Trakt token refresh failed with auth error. Refresh token may be invalid or revoked."
                ) from error
            raise RuntimeError(f"Trakt token refresh failed status={status}, detail={detail}") from error
        except (httpx.RequestError, httpx.TimeoutException, httpx.RemoteProtocolError) as error:
            raise RuntimeError(f"Trakt token refresh failed due to network error: {error}") from error

        token = response.json()
        self._access_token = token["access_token"]
        expires_in = int(token.get("expires_in", 3600))
        # Refresh a little before hard expiry to reduce edge failures.
        self._access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, expires_in - 60))

        maybe_new_refresh = token.get("refresh_token")
        if maybe_new_refresh:
            self._refresh_token = str(maybe_new_refresh)


def _to_trakt_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_page_count(response: httpx.Response) -> int | None:
    raw = response.headers.get("X-Pagination-Page-Count")
    if raw is None:
        return None
    try:
        page_count = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, page_count)


def _parse_item_count(response: httpx.Response) -> int | None:
    raw = response.headers.get("X-Pagination-Item-Count")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _parse_retry_after(value: str | None) -> float:
    if value is None:
        return 1.0
    try:
        retry_after = float(value)
    except ValueError:
        return 1.0
    return max(1.0, retry_after)


def _response_detail(response: httpx.Response) -> str:
    text = response.text.strip()
    if not text:
        return "<empty>"
    return text[:512]


def _backoff_seconds(attempt: int) -> float:
    # capped exponential backoff with small jitter to avoid synchronized retries
    base = min(2**attempt, 30)
    return base + random.uniform(0.0, 0.25)
