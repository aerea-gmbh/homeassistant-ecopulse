"""Entry lifecycle and the stream's resilience (ADR-230 §4 client actions, §6)."""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.api import (
    EcoPulseConnectionError,
    EcoPulseNotEnabledError,
    EcoPulseRateLimitedError,
)
from custom_components.ecopulse.const import DOMAIN
from custom_components.ecopulse.coordinator import next_backoff, wait_after_failure

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import (
    BATTERY_DEVICE,
    CHARGE_ENERGY,
    GRID_POWER,
    METER_DEVICE,
    SOC,
    device,
    entity_id,
    state,
)

NEW_REVISION = "4d2a8e61b7c3f095"
NEW_ENTITY = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _catalog_requests(gateway: FakeGateway) -> int:
    return sum(1 for r in gateway.requests if r.path.endswith("/catalog"))


def _change_catalog(gateway: FakeGateway) -> None:
    """A config push: the meter is gone, the battery lost one entity and gained another."""
    catalog = gateway.catalog
    catalog["catalogRevision"] = NEW_REVISION
    catalog["devices"] = [d for d in catalog["devices"] if d["id"] != METER_DEVICE]
    battery = catalog["devices"][0]
    battery["entities"] = [e for e in battery["entities"] if e["uniqueId"] != CHARGE_ENERGY]
    new_entity = copy.deepcopy(catalog["devices"][0]["entities"][0])
    new_entity.update(uniqueId=NEW_ENTITY, name="Battery power", unit="W", deviceClass="power")
    catalog["devices"][0]["entities"].append(new_entity)
    gateway.info["catalogRevision"] = NEW_REVISION
    gateway.snapshot["catalogRevision"] = NEW_REVISION
    gateway.snapshot["devices"] = [
        d for d in gateway.snapshot["devices"] if d["id"] != METER_DEVICE
    ]
    gateway.snapshot["values"] = [
        v for v in gateway.snapshot["values"] if v["id"] != GRID_POWER
    ] + [{"id": NEW_ENTITY, "value": 1500, "ts": "2026-10-08T09:20:00.000Z"}]


async def test_setup_and_unload(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    assert loaded_entry.state is ConfigEntryState.LOADED
    coordinator = loaded_entry.runtime_data.coordinator

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert coordinator.stream_task is not None
    assert coordinator.stream_task.done()
    assert state(hass, SOC).state == STATE_UNAVAILABLE


async def test_setup_with_a_rejected_token_starts_reauth(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry
) -> None:
    gateway.token = "ecp_ha_rotated"
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


@pytest.mark.parametrize(
    "setup",
    [
        lambda g: setattr(g, "enabled", False),
        lambda g: setattr(g, "rate_limited_for", 5),
        lambda g: setattr(g, "forced_status", 500),
        lambda g: g.catalog["gateway"].update(id="gw-other"),
    ],
    ids=["404-not-enabled", "429", "500", "another-gateway-at-this-address"],
)
async def test_setup_retries_when_the_gateway_is_not_ready(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, setup: Any
) -> None:
    setup(gateway)
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_reconnects_after_a_drop_and_applies_the_fresh_snapshot(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.snapshot["values"][0]["value"] = 42  # SOC changed while we were away
    gateway.drop_streams()

    await gateway.wait_for_stream(2)
    await wait_until(hass, lambda: state(hass, SOC).state == "42")
    assert loaded_entry.runtime_data.coordinator.connected


async def test_entities_are_unavailable_while_the_stream_is_down_and_404_retries_later(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.enabled = False  # HA disabled in the cloud: the edge closes streams, answers 404
    gateway.drop_streams()

    await wait_until(hass, lambda: state(hass, SOC).state == STATE_UNAVAILABLE)
    assert not loaded_entry.runtime_data.coordinator.connected
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows == []  # 404 is retry-later, never reauth

    gateway.enabled = True
    await gateway.wait_for_stream(2)
    await wait_until(hass, lambda: state(hass, SOC).state == "87")


async def test_a_rotated_token_on_the_stream_starts_reauth_and_stops_reconnecting(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.token = "ecp_ha_rotated"
    gateway.drop_streams()

    await wait_until(
        hass,
        lambda: (
            [
                f["context"]["source"]
                for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            ]
            == [SOURCE_REAUTH]
        ),
    )
    coordinator = loaded_entry.runtime_data.coordinator
    await wait_until(hass, coordinator.stream_task.done)
    stream_requests = sum(1 for r in gateway.requests if r.path.endswith("/stream"))
    assert stream_requests == 2  # the first stream and exactly one rejected reconnect
    assert state(hass, SOC).state == STATE_UNAVAILABLE


async def test_a_snapshot_with_a_new_revision_refetches_the_catalog_and_reloads(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # A config push while the stream was down sends no catalog event (ADR-230 r2 N2).
    _change_catalog(gateway)
    gateway.drop_streams()

    await wait_until(
        hass,
        lambda: er.async_get(hass).async_get_entity_id("sensor", DOMAIN, NEW_ENTITY) is not None,
    )
    await wait_until(hass, lambda: state(hass, NEW_ENTITY).state == "1500")
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, GRID_POWER) is None
    # an entity gone from a device that stays (removing the meter device took its entities along)
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, CHARGE_ENERGY) is None
    assert device(hass, BATTERY_DEVICE) is not None
    assert device(hass, METER_DEVICE) is None
    assert device(hass, "gw-7f3a9c") is not None


async def test_a_snapshot_with_the_loaded_revision_does_not_refetch(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    before = _catalog_requests(gateway)
    gateway.drop_streams()

    await gateway.wait_for_stream(2)
    await wait_until(hass, lambda: loaded_entry.runtime_data.coordinator.connected)

    assert _catalog_requests(gateway) == before


async def test_a_catalog_event_refetches_and_reloads(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway, contract: dict
) -> None:
    _change_catalog(gateway)
    gateway.push("catalog", contract["examples"]["stream"]["catalog"])

    await wait_until(
        hass,
        lambda: er.async_get(hass).async_get_entity_id("sensor", DOMAIN, NEW_ENTITY) is not None,
    )
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, GRID_POWER) is None


async def test_a_catalog_event_with_the_loaded_revision_is_ignored(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    before = _catalog_requests(gateway)
    gateway.push("catalog", {"catalogRevision": "9c1e4b7a2f05d863"})
    gateway.push("values", {"values": [{"id": GRID_POWER, "value": 3, "ts": "2026-10-08T09:16Z"}]})

    await wait_until(hass, lambda: state(hass, GRID_POWER).state == "3")
    assert _catalog_requests(gateway) == before


async def test_a_refetch_that_still_returns_the_loaded_catalog_does_not_reload_in_a_loop(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # The snapshot claims a revision /catalog does not (yet) serve: refetch once, apply, carry on.
    gateway.snapshot["catalogRevision"] = NEW_REVISION
    gateway.snapshot["values"][0]["value"] = 55
    gateway.drop_streams()

    await wait_until(hass, lambda: state(hass, SOC).state == "55")
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert gateway.stream_connections == 2


async def test_the_entity_registry_entry_survives_with_its_unique_id_after_reload(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    before = entity_id(hass, SOC)

    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    assert entity_id(hass, SOC) == before


def test_backoff_doubles_from_one_second_up_to_sixty() -> None:
    delays = [1.0]
    for _ in range(8):
        delays.append(next_backoff(delays[-1]))

    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (EcoPulseConnectionError("x"), 4.0),
        (None, 4.0),  # the stream simply ended
        (EcoPulseNotEnabledError("x"), 300.0),
        (EcoPulseRateLimitedError(17), 17.0),
        (EcoPulseRateLimitedError(None), 4.0),
    ],
    ids=["dropped", "ended", "404", "429-retry-after", "429-without-header"],
)
def test_wait_after_a_failure_follows_the_contract(
    error: Exception | None, expected: float
) -> None:
    assert wait_after_failure(error, backoff=4.0) == expected


async def test_a_snapshot_resets_the_backoff(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    backoffs: list[float] = []

    def record(error: Exception | None, backoff: float) -> float:
        backoffs.append(backoff)
        return wait_after_failure(error, backoff)

    with patch("custom_components.ecopulse.coordinator.wait_after_failure", side_effect=record):
        for connection in (2, 3, 4):
            gateway.drop_streams()
            await gateway.wait_for_stream(connection)
            await wait_until(hass, lambda: loaded_entry.runtime_data.coordinator.connected)

    # every drop came right after a snapshot, so every wait starts from the initial backoff
    assert backoffs == [0.01, 0.01, 0.01]


async def test_the_backoff_grows_while_reconnects_fail(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # Positive counterpart: without a snapshot in between, the waits double.
    backoffs: list[float] = []

    def record(error: Exception | None, backoff: float) -> float:
        backoffs.append(backoff)
        return wait_after_failure(error, backoff)

    with patch("custom_components.ecopulse.coordinator.wait_after_failure", side_effect=record):
        gateway.forced_status = 500
        gateway.drop_streams()
        await wait_until(hass, lambda: len(backoffs) >= 4)
        gateway.forced_status = None
        await gateway.wait_for_stream(2)

    assert backoffs[:4] == [0.01, 0.02, 0.04, 0.05]
