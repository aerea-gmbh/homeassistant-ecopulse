"""``binary_sensor`` "Surplus available": the options-driven switch on solar_excess_power (#623).

The stream is real, so values arrive through the fake gateway; the sensor's clock and timers are
the ``clock`` fixture's (see FakeClock for why not freezegun).
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.ecopulse.const import (
    CONF_SURPLUS_HYSTERESIS,
    CONF_SURPLUS_OFF_DELAY,
    CONF_SURPLUS_ON_DELAY,
    CONF_SURPLUS_THRESHOLD,
)

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import GATEWAY_DEVICE, SOLAR_EXCESS, SURPLUS_AVAILABLE, device, entity_id, state


@dataclass
class _Timer:
    due: datetime
    action: Callable[[datetime], None]
    cancelled: bool = False


class FakeClock:
    """Wall clock and point-in-time timers of the surplus sensor, and nothing else.

    freezegun freezes the event loop's monotonic clock as well, which the real stream I/O polls
    on (wait_until never returns), and HA's async_fire_time_changed fires every loop timer, the
    stream's read timeout included (the stream drops on every jump). So only the sensor's own
    seams are faked; HA's tracker itself is not under test here.
    """

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
        self._timers: list[_Timer] = []

    @property
    def created(self) -> int:
        return len(self._timers)

    @property
    def pending(self) -> list[datetime]:
        return [t.due for t in self._timers if not t.cancelled]

    def utcnow(self) -> datetime:
        return self.now

    def track_point_in_utc_time(
        self, hass: HomeAssistant, action: Callable[[datetime], None], point: datetime
    ) -> Callable[[], None]:
        timer = _Timer(point, action)
        self._timers.append(timer)

        def cancel() -> None:
            timer.cancelled = True

        return cancel

    async def advance(self, hass: HomeAssistant, seconds: float) -> None:
        target = self.now + timedelta(seconds=seconds)
        while due := sorted(
            (t for t in self._timers if not t.cancelled and t.due <= target), key=lambda t: t.due
        ):
            timer = due[0]
            timer.cancelled = True
            self.now = timer.due
            timer.action(timer.due)
        self.now = target
        await hass.async_block_till_done()


@pytest.fixture
def clock() -> Generator[FakeClock]:
    fake = FakeClock()
    with (
        patch("custom_components.ecopulse.binary_sensor.utcnow", fake.utcnow),
        patch(
            "custom_components.ecopulse.binary_sensor.async_track_point_in_utc_time",
            fake.track_point_in_utc_time,
        ),
    ):
        yield fake


_DEFAULT_OPTIONS = {
    CONF_SURPLUS_THRESHOLD: 500,
    CONF_SURPLUS_HYSTERESIS: 100,
    CONF_SURPLUS_ON_DELAY: 120,
    CONF_SURPLUS_OFF_DELAY: 120,
}


def _surplus(hass: HomeAssistant) -> State:
    return state(hass, SURPLUS_AVAILABLE, "binary_sensor")


async def _excess(
    hass: HomeAssistant, entry: MockConfigEntry, gateway: FakeGateway, value: Any
) -> None:
    gateway.push(
        "values", {"values": [{"id": SOLAR_EXCESS, "value": value, "ts": "2026-10-09T12:00:00Z"}]}
    )
    coordinator = entry.runtime_data.coordinator
    await wait_until(
        hass,
        lambda: (current := coordinator.value(SOLAR_EXCESS)) is not None and current.value == value,
    )


async def _advance(hass: HomeAssistant, clock: FakeClock, seconds: float) -> None:
    await clock.advance(hass, seconds)


async def _turn_on(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await _excess(hass, entry, gateway, 2160)
    await _advance(hass, clock, 120)
    assert _surplus(hass).state == STATE_ON


async def test_it_lives_on_the_gateway_device_and_starts_off(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    registry_entry = er.async_get(hass).async_get(
        entity_id(hass, SURPLUS_AVAILABLE, "binary_sensor")
    )
    gateway = device(hass, GATEWAY_DEVICE)
    assert registry_entry is not None
    assert gateway is not None
    assert registry_entry.device_id == gateway.id
    assert registry_entry.entity_id == "binary_sensor.haus_nord_surplus_available"

    surplus = _surplus(hass)
    assert surplus.state == STATE_OFF
    assert surplus.name == "Haus Nord Surplus available"
    assert surplus.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.POWER


async def test_on_only_after_the_excess_held_the_threshold_for_the_on_delay(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await _excess(hass, loaded_entry, gateway, 500)  # the default threshold, inclusive

    await _advance(hass, clock, 119)
    assert _surplus(hass).state == STATE_OFF
    await _advance(hass, clock, 1)
    assert _surplus(hass).state == STATE_ON


async def test_a_dip_restarts_the_on_delay(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await _excess(hass, loaded_entry, gateway, 800)
    await _advance(hass, clock, 100)
    await _excess(hass, loaded_entry, gateway, 300)
    await _advance(hass, clock, 1)
    await _excess(hass, loaded_entry, gateway, 800)

    await _advance(hass, clock, 119)
    assert _surplus(hass).state == STATE_OFF
    await _advance(hass, clock, 1)
    assert _surplus(hass).state == STATE_ON


async def test_off_only_below_the_hysteresis_band_for_the_off_delay(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await _turn_on(hass, loaded_entry, gateway, clock)

    await _excess(hass, loaded_entry, gateway, 400)  # 500 - 100: still inside the band
    await _advance(hass, clock, 600)
    assert _surplus(hass).state == STATE_ON

    await _excess(hass, loaded_entry, gateway, -200)
    await _advance(hass, clock, 119)
    assert _surplus(hass).state == STATE_ON
    await _advance(hass, clock, 1)
    assert _surplus(hass).state == STATE_OFF


async def test_an_unknown_excess_turns_it_off_after_the_off_delay(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await _turn_on(hass, loaded_entry, gateway, clock)

    await _excess(hass, loaded_entry, gateway, None)
    await _advance(hass, clock, 119)
    assert _surplus(hass).state == STATE_ON
    await _advance(hass, clock, 1)
    assert _surplus(hass).state == STATE_OFF


async def test_a_lost_stream_turns_it_off_and_it_stays_available(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    # Off, never unavailable: an automation listening for "to: off" must still fire.
    await _turn_on(hass, loaded_entry, gateway, clock)

    gateway.enabled = False
    gateway.drop_streams()
    await wait_until(hass, lambda: not loaded_entry.runtime_data.coordinator.connected)
    await _advance(hass, clock, 119)
    assert _surplus(hass).state == STATE_ON
    await _advance(hass, clock, 1)
    assert _surplus(hass).state == STATE_OFF


async def test_a_restored_on_state_survives_a_restart_and_is_held_by_the_off_rule(
    hass: HomeAssistant,
    gateway: FakeGateway,
    config_entry: MockConfigEntry,
    fast_backoff: Any,
    clock: FakeClock,
) -> None:
    mock_restore_cache(hass, [State("binary_sensor.haus_nord_surplus_available", STATE_ON)])
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)
    assert _surplus(hass).state == STATE_ON

    await _excess(hass, config_entry, gateway, 450)  # inside the band: stays on
    await _advance(hass, clock, 600)
    assert _surplus(hass).state == STATE_ON

    await _excess(hass, config_entry, gateway, 0)
    await _advance(hass, clock, 120)
    assert _surplus(hass).state == STATE_OFF


async def test_a_restored_on_state_without_any_reading_turns_off_after_the_off_delay(
    hass: HomeAssistant,
    gateway: FakeGateway,
    config_entry: MockConfigEntry,
    fast_backoff: Any,
    clock: FakeClock,
) -> None:
    mock_restore_cache(hass, [State("binary_sensor.haus_nord_surplus_available", STATE_ON)])
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)

    await _advance(hass, clock, 119)  # the snapshot's excess is null
    assert _surplus(hass).state == STATE_ON
    await _advance(hass, clock, 1)
    assert _surplus(hass).state == STATE_OFF


async def test_changed_options_apply_live_without_a_reload(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await _turn_on(hass, loaded_entry, gateway, clock)

    hass.config_entries.async_update_entry(
        loaded_entry,
        options={
            CONF_SURPLUS_THRESHOLD: 3000,
            CONF_SURPLUS_HYSTERESIS: 100,
            CONF_SURPLUS_ON_DELAY: 60,
            CONF_SURPLUS_OFF_DELAY: 30,
        },
    )
    await hass.async_block_till_done()

    await _advance(hass, clock, 29)  # 2160 < 3000 - 100 now
    assert _surplus(hass).state == STATE_ON
    await _advance(hass, clock, 1)
    assert _surplus(hass).state == STATE_OFF

    await _excess(hass, loaded_entry, gateway, 3000)
    await _advance(hass, clock, 60)
    assert _surplus(hass).state == STATE_ON
    assert gateway.stream_connections == 1  # no reload, no reconnect


async def test_a_value_every_tick_keeps_one_timer_and_a_recovery_cancels_it(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await _excess(hass, loaded_entry, gateway, 700)
    for value in (800, 900, 750):
        await _advance(hass, clock, 2)
        await _excess(hass, loaded_entry, gateway, value)

    assert clock.pending == [datetime(2026, 10, 9, 12, 2, tzinfo=UTC)]
    assert clock.created == 1  # not one timer per values event

    await _excess(hass, loaded_entry, gateway, 100)
    assert clock.pending == []


async def test_unloading_cancels_a_pending_switch(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await _excess(hass, loaded_entry, gateway, 700)
    assert len(clock.pending) == 1

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    assert clock.pending == []


async def test_the_off_delay_runs_on_home_assistants_own_time_tracker(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # No FakeClock: the one test that relies on async_track_point_in_utc_time itself (callback
    # signature, fires at the point). With the stream down no read timeout can interfere when
    # async_fire_time_changed fires every due loop timer; the on-delay is 0 so switching on
    # needs no timer either.
    hass.config_entries.async_update_entry(
        loaded_entry, options={**_DEFAULT_OPTIONS, CONF_SURPLUS_ON_DELAY: 0}
    )
    await _excess(hass, loaded_entry, gateway, 2160)
    assert _surplus(hass).state == STATE_ON

    gateway.enabled = False
    gateway.drop_streams()
    await wait_until(hass, lambda: not loaded_entry.runtime_data.coordinator.connected)
    lost_at = dt_util.utcnow()

    async_fire_time_changed(hass, lost_at + timedelta(seconds=110))
    await hass.async_block_till_done()
    assert _surplus(hass).state == STATE_ON

    async_fire_time_changed(hass, lost_at + timedelta(seconds=125))
    await hass.async_block_till_done()
    assert _surplus(hass).state == STATE_OFF
