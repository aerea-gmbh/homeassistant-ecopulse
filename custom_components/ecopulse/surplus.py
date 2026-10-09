"""The decision behind "Surplus available" (#623), free of Home Assistant so it is unit-testable.

- **On** once ``solar_excess_power >= threshold`` held for the whole on-delay.
- **Off** once ``solar_excess_power < threshold - hysteresis`` held for the whole off-delay.
- Between the two thresholds the state holds (the hysteresis band). Leaving the switching
  condition before its delay ran out restarts the delay.
- **Unknown counts as no surplus** (fail-safe): ``None`` or a non-finite value never turns the
  sensor on and turns it off like a deficit, after the off-delay. Not at once, so a short stream
  gap or a single unreadable meter poll does not cycle a heat pump; the off-delay is the import
  the user already accepted for a real deficit.

Time is passed in; the caller owns the timer and asks again at the returned instant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Any

from .const import (
    CONF_SURPLUS_HYSTERESIS,
    CONF_SURPLUS_OFF_DELAY,
    CONF_SURPLUS_ON_DELAY,
    CONF_SURPLUS_THRESHOLD,
    DEFAULT_SURPLUS_HYSTERESIS_W,
    DEFAULT_SURPLUS_OFF_DELAY_SECONDS,
    DEFAULT_SURPLUS_ON_DELAY_SECONDS,
    DEFAULT_SURPLUS_THRESHOLD_W,
    SOLAR_EXCESS_POWER_KEY,
    SURPLUS_AVAILABLE_SUFFIX,
)
from .models import Catalog, CatalogEntity


@dataclass(frozen=True, slots=True)
class SurplusSettings:
    threshold_w: float
    hysteresis_w: float
    on_delay: timedelta
    off_delay: timedelta

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> SurplusSettings:
        """The entry's options; an entry that never opened the options form gets the defaults."""
        return cls(
            threshold_w=options.get(CONF_SURPLUS_THRESHOLD, DEFAULT_SURPLUS_THRESHOLD_W),
            hysteresis_w=options.get(CONF_SURPLUS_HYSTERESIS, DEFAULT_SURPLUS_HYSTERESIS_W),
            on_delay=timedelta(
                seconds=options.get(CONF_SURPLUS_ON_DELAY, DEFAULT_SURPLUS_ON_DELAY_SECONDS)
            ),
            off_delay=timedelta(
                seconds=options.get(CONF_SURPLUS_OFF_DELAY, DEFAULT_SURPLUS_OFF_DELAY_SECONDS)
            ),
        )


def surplus_source(catalog: Catalog) -> CatalogEntity | None:
    """The gateway's solar_excess_power sensor, if it serves one (only with a PV assistant)."""
    source = catalog.gateway_entity(SOLAR_EXCESS_POWER_KEY)
    return source if source is not None and source.platform == "sensor" else None


def surplus_available_unique_id(source: CatalogEntity) -> str:
    return f"{source.unique_id}:{SURPLUS_AVAILABLE_SUFFIX}"


class SurplusDetector:
    def __init__(self, settings: SurplusSettings, *, is_on: bool = False) -> None:
        self._settings = settings
        self._is_on = is_on
        self._value: float | None = None
        self._switching_since: datetime | None = None

    @property
    def is_on(self) -> bool:
        return self._is_on

    def update(self, value: object, now: datetime) -> datetime | None:
        """Take the current excess; returns when to call again while a switch is pending."""
        self._value = _usable(value)
        return self._evaluate(now)

    def reconfigure(self, settings: SurplusSettings, now: datetime) -> datetime | None:
        """New settings on the last value; a condition that still holds keeps its start."""
        self._settings = settings
        return self._evaluate(now)

    def _evaluate(self, now: datetime) -> datetime | None:
        if not self._wants_to_switch():
            self._switching_since = None
            return None
        if self._switching_since is None:
            self._switching_since = now
        due = self._switching_since + self._delay()
        if now < due:
            return due
        self._is_on = not self._is_on
        self._switching_since = None
        return None

    def _wants_to_switch(self) -> bool:
        if self._is_on:
            return self._value is None or self._value < self._off_threshold()
        return self._value is not None and self._value >= self._settings.threshold_w

    def _off_threshold(self) -> float:
        return self._settings.threshold_w - self._settings.hysteresis_w

    def _delay(self) -> timedelta:
        return self._settings.off_delay if self._is_on else self._settings.on_delay


def _usable(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None
