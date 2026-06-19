"""DataUpdateCoordinator for Mi Fitness."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MiFitnessApiError, MiFitnessAuthError, MiFitnessClient
from .auth import try_silent_token_refresh
from .const import (
    CONF_PASS_TOKEN,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    DEFAULT_START_WATERMARK,
    DOMAIN,
    KEY_CALORIES,
    KEY_HEART_RATE,
    KEY_RESTING_HEART_RATE,
    KEY_SLEEP,
    KEY_SPO2,
    KEY_STEPS,
    KEY_VITALITY,
    KEY_VALID_STAND,
    KEY_WEIGHT,
    SCAN_INTERVAL_MINUTES,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=SCAN_INTERVAL_MINUTES)

# Sleep stage state codes from the API
_SLEEP_STATE_NAMES = {2: "light", 3: "deep", 4: "rem", 5: "awake"}


def _day_start_ts() -> int:
    """Unix timestamp of 00:00 local time today."""
    now = datetime.now()
    return int(datetime(now.year, now.month, now.day).timestamp())


def _local_day_num() -> int:
    """Calendar day number in LOCAL time (so daily sensors reset at local
    midnight, not UTC midnight)."""
    return datetime.now().toordinal()


def _parse_value(item: dict) -> Any:
    """Parse 'value' field from a data_list item (JSON string or raw)."""
    raw = item.get("value", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _sleep_quality_score(
    duration: int,
    deep: int,
    rem: int,
    awake_count: int,
) -> int:
    """
    Calculate sleep quality score 0-100.

    Weights:
      30 % — duration proximity to 8h optimum
      35 % — deep sleep ratio (target ≥20 %)
      20 % — REM ratio (target ≥20 %)
      15 % — continuity (awake interruptions penalty)
    """
    if not duration or duration < 60:
        return 0

    # Duration score: best at 480 min (8h), -1 pt per 5 min away
    dur_score = max(0, 100 - abs(duration - 480) // 5)

    # Deep score: 20 % = 100 pts, linear
    deep_ratio = deep / duration if duration else 0
    deep_score = min(100, int(deep_ratio * 500))

    # REM score
    rem_ratio = rem / duration if duration else 0
    rem_score = min(100, int(rem_ratio * 500))

    # Continuity: lose 8 pts per awakening, min 40 pts
    continuity = max(40, 100 - (awake_count or 0) * 8)

    total = (
        dur_score  * 0.30
        + deep_score * 0.35
        + rem_score  * 0.20
        + continuity * 0.15
    )
    return round(total)


def _sleep_chronotype_6(history: list[dict]) -> str:
    """
    Classify into one of Xiaomi's 6 sleep animals using multi-night history.

    Based on actual Mi Fitness app descriptions:
      sheep      — early bed, sleeps VERY LITTLE (short duration, gets tired)
      penguin    — early bed, sleeps ok but EASILY AWAKENED (high awake count)
      brown_bear — healthy/normal schedule (22-23h), plenty of restful sleep
      koala      — stays up late, sleeps SOUNDLY once asleep (low awake, good deep)
      night_owl  — stays up late, sleeps RESTLESSLY (high awake count)
      shark      — very late and/or sleeps LITTLE (severely deprived)

    Uses averages over available history (ideally ≥7 nights).
    """
    records = [r for r in history if r.get("bedtime") and r.get("duration", 0) >= 120]
    if not records:
        return "brown_bear"

    bed_hours = []
    for r in records:
        bed_dt = datetime.fromtimestamp(r["bedtime"])
        h = bed_dt.hour + bed_dt.minute / 60
        if h < 7:            # normalise midnight crossings: 00:30 → 24.5
            h += 24
        bed_hours.append(h)

    avg_bed      = sum(bed_hours) / len(bed_hours)
    avg_awake    = sum(r.get("awake_count", 0) for r in records) / len(records)
    avg_duration = sum(r.get("duration", 0) for r in records) / len(records)

    peaceful   = avg_awake < 1.5    # ≤1 awakening/night on average
    enough     = avg_duration >= 390  # ≥6.5h
    very_short = avg_duration < 300   # <5h — "sleeps very little"

    # Very late (after 01:30) or critically short sleep → Shark
    if avg_bed >= 25.5 or very_short:
        return "shark"

    # Late (23:00 – 01:30)
    if avg_bed >= 23.0:
        if not peaceful:
            return "night_owl"  # late + restless
        return "koala"          # late but sleeps soundly

    # Normal schedule (22:00 – 23:00)
    if avg_bed >= 22.0:
        if peaceful:
            return "brown_bear"
        return "night_owl"      # normal time but fragmented sleep

    # Early (<22:00)
    if not peaceful:
        return "penguin"        # early but easily awakened
    return "sheep"              # early + short/little sleep


def _stages_to_list(items: list[dict]) -> list[dict]:
    """
    Convert raw API sleep items into clean stage dicts.
    Each item: {start, end, state, label}
    """
    out = []
    for seg in items:
        state = seg.get("state", 0)
        out.append({
            "start": seg.get("start_time", 0),
            "end":   seg.get("end_time",   0),
            "state": state,
            "label": _SLEEP_STATE_NAMES.get(state, "unknown"),
        })
    return out


class MiFitnessCoordinator(DataUpdateCoordinator):
    """Polls Mi Fitness API and aggregates sensor data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: MiFitnessClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._client = client
        self._entry = entry
        self._entry_id = entry.entry_id
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
        self._watermark: int = 0
        self._day_wm: int = 0       # watermark saved at start of each new day
        self._day_num: int = 0      # calendar day when _day_wm was recorded
        self._sensor_data: dict[str, Any] = {}
        self._sleep_history: dict[str, dict] = {}
        self._steps_history: dict[str, dict] = {}
        # Per-day step records keyed by timestamp: {date_str: {ts: {...}}}.
        # The server re-sends the same interval with a new watermark as data
        # accumulates, so we DEDUPE by ts (replace) instead of summing.
        self._step_buckets: dict[str, dict] = {}
        self._initial_load_done: bool = False

    async def async_load_stored_state(self):
        """Load persisted state. Called once before the first refresh.

        Deliberately NOT named ``_async_setup`` — that is a DataUpdateCoordinator
        hook (HA 2024.8+) auto-invoked by ``async_config_entry_first_refresh``,
        so reusing the name would run this twice.
        """
        stored = await self._store.async_load()
        if stored:
            self._watermark     = stored.get("watermark", 0)
            self._day_wm        = stored.get("day_wm", 0)
            self._day_num       = stored.get("day_num", 0)
            self._sleep_history = stored.get("sleep_history", {})
            self._steps_history = stored.get("steps_history", {})
            self._step_buckets  = stored.get("step_buckets", {})
            self._sensor_data   = stored.get("sensor_data", {})
            # On restart roll back to start-of-today so we re-fetch today's data.
            # (watermark sits at end-of-data; day_wm is start-of-today.)
            if self._day_wm:
                self._watermark = self._day_wm
                # Clear today's step totals — they'll be rebuilt from re-fetched records.
                # (Avoids double-counting since we accumulate into _steps_history.)
                today_str = datetime.now().strftime("%Y-%m-%d")
                self._steps_history.pop(today_str, None)
                for _k in ("steps_today", "distance_today", "step_calories_today", "steps_hourly"):
                    self._sensor_data.pop(_k, None)
            elif self._watermark == 0:
                # First-ever install: skip ancient history, start ~30 days ago
                self._watermark = DEFAULT_START_WATERMARK

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch new data from API and merge into sensor state."""

        # Reset daily metrics when calendar day changes.
        # Without this, yesterday's vitality/steps/hr values show as today's
        # until the watch syncs for the first time each day.
        today_day = _local_day_num()
        if self._initial_load_done and today_day != self._day_num and self._day_num != 0:
            _LOGGER.debug("Mi Fitness: new day — clearing daily sensor accumulators")
            for _k in (
                "steps_today", "distance_today", "step_calories_today", "steps_hourly",
                "vitality_high", "vitality_medium", "vitality_low", "vitality_ts",
                "calories_today",
                "heart_rate_avg_today", "heart_rate_max_today", "heart_rate_min_today",
                "stand_hours_today", "stand_hours_list",
            ):
                self._sensor_data.pop(_k, None)

        # First refresh: fetch only 1 page to verify connectivity fast
        # (avoids CancelledError timeout during async_config_entry_first_refresh)
        # Subsequent polls: 30 pages to catch up through historical data quickly
        max_pages = 1 if not self._initial_load_done else 30
        self._initial_load_done = True

        try:
            items, new_wm = await self.hass.async_add_executor_job(
                self._client.fetch_all_since,
                self._watermark,
                max_pages,
            )
        except MiFitnessAuthError as exc:
            # ── Try silent serviceToken refresh using stored passToken ──────────
            # passToken-based auth skips 2FA (token was issued after completed 2FA).
            # If it works, we update credentials and retry on the next poll —
            # no user interaction required.
            pass_token = self._entry.data.get(CONF_PASS_TOKEN, "")
            user_id    = self._entry.data.get(CONF_USER_ID, "")
            if pass_token and user_id:
                _LOGGER.warning(
                    "serviceToken expired — attempting silent refresh with passToken"
                )
                creds = await self.hass.async_add_executor_job(
                    try_silent_token_refresh, pass_token, user_id
                )
                if creds.get("service_token"):
                    new_data = {**self._entry.data, CONF_SERVICE_TOKEN: creds["service_token"]}
                    if creds.get("ssecurity"):
                        new_data[CONF_SSECURITY] = creds["ssecurity"]
                        self._client.ssecurity = creds["ssecurity"]
                    # Do NOT update passToken — server rotates it on each exchange,
                    # and the new passToken gives different (wrong) ssecurity.
                    self._client.update_service_token(creds["service_token"])
                    self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                    _LOGGER.warning("Silent refresh succeeded — resuming on next poll")
                    raise UpdateFailed(
                        "Credentials refreshed silently — data updates on next poll"
                    ) from exc
                _LOGGER.warning(
                    "Silent token refresh failed — passToken may have expired; "
                    "triggering user re-auth"
                )
            # Silent refresh unavailable or failed — ask the user
            self._entry.async_start_reauth(self.hass)
            raise UpdateFailed(f"Auth error (re-auth triggered): {exc}") from exc
        except MiFitnessApiError as exc:
            raise UpdateFailed(str(exc)) from exc

        if new_wm and new_wm != self._watermark:
            self._watermark = new_wm
            today = _local_day_num()
            if today != self._day_num:
                # New day — snapshot watermark so restart can roll back to here
                self._day_wm  = new_wm
                self._day_num = today
            await self._store.async_save({
                "watermark":     new_wm,
                "day_wm":        self._day_wm,
                "day_num":       self._day_num,
                "sleep_history": self._sleep_history,
                "steps_history": self._steps_history,
                "step_buckets":  self._step_buckets,
                "sensor_data":   self._sensor_data,
            })

        # Dynamic interval: catching up → 30 s, up to date → 15 min
        if items:
            self._merge_items(items)
            latest_ts = max(
                (item.get("time") or item.get("update_time") or 0) for item in items
            )
            catching_up = latest_ts < (time.time() - 2 * 86400)
        else:
            # No new items → fully caught up
            catching_up = False
            latest_ts = 0

        if catching_up:
            self.update_interval = timedelta(seconds=30)
            _LOGGER.debug("Mi Fitness: catching up — latest_item=%s", datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d") if latest_ts else "—")
        else:
            if self.update_interval != SCAN_INTERVAL:
                self.update_interval = SCAN_INTERVAL
                _LOGGER.debug("Mi Fitness: caught up, switching to %s min interval", SCAN_INTERVAL_MINUTES)
        return dict(self._sensor_data)

    def _merge_items(self, items: list[dict]) -> None:
        """Merge new data_list items into aggregated sensor state."""
        now       = time.time()
        today     = _day_start_ts()
        tomorrow  = today + 86400

        # Cutoffs: ignore stale data for sensor VALUES.
        # The watermark still advances so we never re-fetch these items.
        # "recent" sensors (HR, sleep, vitality): 14 days
        # "infrequent" sensors (weight, SpO2): 90 days
        recent_cutoff = now - 14 * 86400
        slow_cutoff   = now - 90 * 86400

        touched_step_dates: set[str] = set()
        calories_today: list[dict] = []
        hr_readings: list[dict] = []
        stand_hours_today_set: set[int] = set()

        for item in items:
            key = item.get("key", "")
            ts  = item.get("time") or item.get("update_time") or 0
            val = _parse_value(item)

            # ── Steps ─────────────────────────────────────────────────────
            if key == KEY_STEPS:
                if ts >= recent_cutoff and isinstance(val, dict):
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    hour     = datetime.fromtimestamp(ts).hour
                    _LOGGER.debug(
                        "Mi Fitness STEP: date=%s h=%02d steps=%s dist=%s",
                        date_str, hour, val.get("steps"), val.get("distance"),
                    )
                    # Dedupe by timestamp: the server re-sends the same interval
                    # with a new watermark as data accumulates. REPLACE (not add),
                    # otherwise a day inflates by the number of re-sends.
                    day = self._step_buckets.setdefault(date_str, {})
                    day[str(ts)] = {
                        "steps":    val.get("steps", 0),
                        "distance": val.get("distance", 0),
                        "calories": val.get("calories", 0),
                        "hour":     hour,
                    }
                    touched_step_dates.add(date_str)

            # ── Heart rate ────────────────────────────────────────────────
            elif key == KEY_HEART_RATE:
                if ts < recent_cutoff:
                    continue
                prev = self._sensor_data.get("heart_rate_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("bpm"):
                    self._sensor_data["heart_rate"]      = val["bpm"]
                    self._sensor_data["heart_rate_ts"]   = ts
                    self._sensor_data["heart_rate_type"] = val.get("type", 0)
                if today <= ts < tomorrow:
                    hr_readings.append(val)

            # ── Resting heart rate ────────────────────────────────────────
            elif key == KEY_RESTING_HEART_RATE:
                if ts < recent_cutoff:
                    continue
                prev = self._sensor_data.get("resting_hr_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("bpm"):
                    self._sensor_data["resting_heart_rate"] = val["bpm"]
                    self._sensor_data["resting_hr_ts"]      = ts

            # ── Sleep ─────────────────────────────────────────────────────
            elif key == KEY_SLEEP:
                if ts >= recent_cutoff:
                    self._process_sleep(val, ts)

            # ── Calories ──────────────────────────────────────────────────
            elif key == KEY_CALORIES:
                if today <= ts < tomorrow:
                    calories_today.append(val)

            # ── Weight ────────────────────────────────────────────────────
            elif key == KEY_WEIGHT:
                if ts < slow_cutoff:
                    continue
                prev = self._sensor_data.get("weight_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("weight"):
                    self._sensor_data["weight"]    = val["weight"]
                    self._sensor_data["weight_ts"] = ts

            # ── SpO2 ──────────────────────────────────────────────────────
            elif key == KEY_SPO2:
                if ts < slow_cutoff:
                    continue
                prev = self._sensor_data.get("spo2_ts", 0)
                if ts > prev and isinstance(val, dict):
                    spo2_val = val.get("spo2") or val.get("value") or val.get("blood_oxygen")
                    if spo2_val:
                        self._sensor_data["spo2"]    = spo2_val
                        self._sensor_data["spo2_ts"] = ts

            # ── Vitality ──────────────────────────────────────────────────
            elif key == KEY_VITALITY:
                if ts < recent_cutoff:
                    continue
                prev_day = self._sensor_data.get("vitality_ts", 0)
                if ts > prev_day and isinstance(val, dict):
                    self._sensor_data["vitality_high"]   = val.get("daily_high_intensity_vitality", 0)
                    self._sensor_data["vitality_medium"] = val.get("daily_medium_intensity_vitality", 0)
                    self._sensor_data["vitality_low"]    = val.get("daily_low_intensity_vitality", 0)
                    self._sensor_data["vitality_ts"]     = ts

            # ── Valid stand ───────────────────────────────────────────────
            elif key == KEY_VALID_STAND:
                if today <= ts < tomorrow:
                    stand_hours_today_set.add(datetime.fromtimestamp(ts).hour)

        # Steps: rebuild day totals from deduped buckets (sum of unique-ts values)
        if touched_step_dates:
            # prune buckets older than 14 days
            cutoff_str = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            self._step_buckets = {
                d: b for d, b in self._step_buckets.items() if d >= cutoff_str
            }
            # derive per-day totals from the deduped buckets
            self._steps_history = {}
            for date_str, buckets in self._step_buckets.items():
                self._steps_history[date_str] = {
                    "date":     date_str,
                    "steps":    sum(b["steps"]    for b in buckets.values()),
                    "distance": sum(b["distance"] for b in buckets.values()),
                    "calories": sum(b["calories"] for b in buckets.values()),
                }
            today_str = datetime.fromtimestamp(today).strftime("%Y-%m-%d")
            tb = self._step_buckets.get(today_str)
            if tb:
                self._sensor_data["steps_today"]         = sum(b["steps"]    for b in tb.values())
                self._sensor_data["distance_today"]      = sum(b["distance"] for b in tb.values())
                self._sensor_data["step_calories_today"] = sum(b["calories"] for b in tb.values())
                hourly: dict[int, dict] = {}
                for b in tb.values():
                    he = hourly.setdefault(b["hour"], {"steps": 0, "distance": 0})
                    he["steps"]    += b["steps"]
                    he["distance"] += b["distance"]
                self._sensor_data["steps_hourly"] = [
                    {"hour": h, "steps": v["steps"], "distance": v["distance"]}
                    for h, v in sorted(hourly.items())
                ]
            self._sensor_data["steps_history_14d"] = sorted(
                self._steps_history.values(), key=lambda x: x["date"], reverse=True
            )[:14]

        if calories_today:
            self._sensor_data["calories_today"] = sum(
                v.get("calories", 0) for v in calories_today if isinstance(v, dict)
            )

        if hr_readings:
            bpms = [v["bpm"] for v in hr_readings if isinstance(v, dict) and v.get("bpm")]
            if bpms:
                self._sensor_data["heart_rate_avg_today"] = round(sum(bpms) / len(bpms))
                self._sensor_data["heart_rate_max_today"] = max(bpms)
                self._sensor_data["heart_rate_min_today"] = min(bpms)

        if stand_hours_today_set:
            prev_set = set(self._sensor_data.get("stand_hours_list", []))
            merged   = sorted(prev_set | stand_hours_today_set)
            self._sensor_data["stand_hours_list"]  = merged
            self._sensor_data["stand_hours_today"] = len(merged)

        # Rebuild history + compute chronotype from full history (not single night)
        history_list = self._build_sleep_history_list()
        self._sensor_data["sleep_history_14d"] = history_list
        if history_list:
            self._sensor_data["sleep_chronotype"] = _sleep_chronotype_6(history_list)

    # ── Sleep processing ──────────────────────────────────────────────────────

    def _process_sleep(self, val: Any, ts: int) -> None:
        """Extract all sleep fields, compute quality/efficiency, update history."""
        if not isinstance(val, dict):
            return
        bedtime = val.get("bedtime", 0)
        if not bedtime:
            return

        # Only update current sleep record if this is newer
        prev_bedtime = self._sensor_data.get("sleep_bedtime", 0)
        if bedtime <= prev_bedtime:
            return

        wakeup      = val.get("wake_up_time", 0)
        duration    = val.get("duration", 0)
        deep        = val.get("sleep_deep_duration", 0)
        light       = val.get("sleep_light_duration", 0)
        rem         = val.get("sleep_rem_duration", val.get("rem_duration", 0))
        avg_hr      = val.get("avg_hr", 0)
        max_hr      = val.get("max_hr", 0)
        min_hr      = val.get("min_hr", 0)
        awake_count = val.get("awake_count", 0)
        stages_raw  = val.get("items", [])

        quality    = _sleep_quality_score(duration, deep, rem, awake_count)
        stages     = _stages_to_list(stages_raw) if stages_raw else []
        time_in_bed = (wakeup - bedtime) / 60 if wakeup > bedtime else duration
        efficiency  = round(duration / time_in_bed * 100) if time_in_bed > 0 else 0

        self._sensor_data.update({
            "sleep_bedtime":     bedtime,
            "sleep_wakeup":      wakeup,
            "sleep_duration":    duration,
            "sleep_deep":        deep,
            "sleep_light":       light,
            "sleep_rem":         rem,
            "sleep_avg_hr":      avg_hr,
            "sleep_max_hr":      max_hr,
            "sleep_min_hr":      min_hr,
            "sleep_awake_count": awake_count,
            "sleep_ts":          ts,
            "sleep_quality":     quality,
            "sleep_efficiency":  efficiency,
            "sleep_stages":      stages,
        })

        # Update 14-day history dict (keyed by local date of bedtime)
        date_str = datetime.fromtimestamp(bedtime).strftime("%Y-%m-%d")
        self._sleep_history[date_str] = {
            "date":        date_str,
            "bedtime":     bedtime,
            "wakeup":      wakeup,
            "duration":    duration,
            "deep":        deep,
            "light":       light,
            "rem":         rem,
            "awake_count": awake_count,
            "quality":     quality,
            "efficiency":  efficiency,
        }

        # Prune to 14 days
        cutoff = datetime.now().strftime("%Y-%m-%d")
        cutoff_dt = datetime.now() - timedelta(days=14)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%d")
        self._sleep_history = {
            k: v for k, v in self._sleep_history.items() if k >= cutoff_str
        }

    def _build_sleep_history_list(self) -> list[dict]:
        """Return sleep history sorted newest-first (max 14 entries)."""
        return sorted(
            self._sleep_history.values(),
            key=lambda x: x["date"],
            reverse=True,
        )[:14]
