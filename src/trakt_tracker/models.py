from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class WatchEvent:
    history_id: int
    watched_at: datetime
    media_type: str
    trakt_id: int
    show_trakt_id: int | None
    season_number: int | None
    episode_number: int | None
    runtime_min: float
    year: int | None
    title: str
    show_title: str | None
    is_rewatch: bool

    @property
    def title_key(self) -> str:
        if self.media_type == "episode":
            show_part = self.show_trakt_id if self.show_trakt_id is not None else self.show_title or "unknown_show"
            season = self.season_number if self.season_number is not None else 0
            episode = self.episode_number if self.episode_number is not None else 0
            return f"episode:{show_part}:s{season}:e{episode}"
        return f"movie:{self.trakt_id}"



def _parse_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)



def _extract_runtime(payload: dict[str, Any], media_type: str) -> float:
    if media_type == "movie":
        movie = payload.get("movie", {})
        runtime = movie.get("runtime")
        return float(runtime) if runtime is not None else 0.0

    episode = payload.get("episode", {})
    runtime = episode.get("runtime")
    return float(runtime) if runtime is not None else 0.0



def _extract_primary(media_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = "movie" if media_type == "movie" else "episode"
    return payload.get(key, {})



def parse_watch_event(payload: dict[str, Any]) -> WatchEvent:
    media_type = payload.get("type")
    if media_type not in {"movie", "episode"}:
        raise ValueError(f"Unsupported media type: {media_type}")

    primary = _extract_primary(media_type, payload)
    ids = primary.get("ids", {})
    trakt_id = ids.get("trakt")
    if trakt_id is None:
        raise ValueError("Missing trakt id in payload")

    show = payload.get("show", {}) if media_type == "episode" else {}
    show_ids = show.get("ids", {})

    watched_at_raw = payload.get("watched_at")
    if not watched_at_raw:
        raise ValueError("Missing watched_at in payload")

    history_id = payload.get("id")
    if history_id is None:
        raise ValueError("Missing history id in payload")

    return WatchEvent(
        history_id=int(history_id),
        watched_at=_parse_datetime(watched_at_raw),
        media_type=media_type,
        trakt_id=int(trakt_id),
        show_trakt_id=int(show_ids["trakt"]) if show_ids.get("trakt") is not None else None,
        season_number=int(primary["season"]) if primary.get("season") is not None else None,
        episode_number=int(primary["number"]) if primary.get("number") is not None else None,
        runtime_min=_extract_runtime(payload, media_type),
        year=int(primary["year"]) if primary.get("year") is not None else None,
        title=str(primary.get("title") or "Unknown"),
        show_title=str(show.get("title")) if show.get("title") else None,
        is_rewatch=bool(payload.get("rewatched", False)),
    )
