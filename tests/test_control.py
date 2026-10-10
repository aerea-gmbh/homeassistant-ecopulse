"""Home Assistant controls EcoPulse's assistants (schema 7, #627, ADR-232 S8).

What must hold: the select/switch/number entities of the catalog's ``controls`` exist only while
the token has the ``control`` scope (``/info.scopes``) AND the entry is pinned (TLS); their state
is the stream value named by ``stateValueId``; an action posts ``{action, assistantId, value}`` to
``POST /control`` over the pinned connection and never in the clear; a refusal (422, 503, 400) or a
rate limit (429) is a clear error of that action, never a retry; a 403 disables the route (repair
issue, entities unavailable, no reauth); an entry that loses the scope loses the entities.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import aiohttp
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.api import EcoPulseApiClient, EcoPulseControlNeedsTlsError
from custom_components.ecopulse.const import (
    CONF_TLS_FINGERPRINT,
    CONF_TLS_PORT,
    DOMAIN,
    ISSUE_CONTROL_REFUSED,
)

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import device

ASSISTANT = "5b6c7d8e-9f01-4a2b-8c3d-4e5f60718293"  # catalog example: the PV wallbox assistant
MODE = f"{ASSISTANT}:mode"
ENABLED = f"{ASSISTANT}:enabled"
START = f"{ASSISTANT}:battery_support_start_soc"
STOP = f"{ASSISTANT}:battery_support_stop_soc"
CONTROL_PLATFORMS = ("select", "switch", "number")


def _entry(gateway: FakeGateway, *, pinned: bool = True) -> MockConfigEntry:
    data: dict[str, Any] = {
        CONF_HOST: gateway.host,
        CONF_PORT: gateway.port,
        CONF_TOKEN: gateway.token,
    }
    if pinned:
        data |= {CONF_TLS_PORT: gateway.port, CONF_TLS_FINGERPRINT: gateway.fingerprint}
    return MockConfigEntry(domain=DOMAIN, title="Haus Nord", unique_id="gw-7f3a9c", data=data)


async def _load(hass: HomeAssistant, gateway: FakeGateway, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await gateway.wait_for_stream(1)
    await wait_until(hass, lambda: entry.runtime_data.coordinator.connected)


def _control_entities(hass: HomeAssistant, entry: MockConfigEntry) -> set[tuple[str, str]]:
    return {
        (e.domain, e.unique_id)
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain in CONTROL_PLATFORMS
    }


def _entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found is not None, (platform, unique_id)
    return found


def _with_control_values(gateway: FakeGateway) -> None:
    gateway.snapshot["values"] = [
        *gateway.snapshot["values"],
        {"id": MODE, "value": "pv-excess", "ts": "2026-10-10T12:00:00.000Z"},
        {"id": ENABLED, "value": True, "ts": "2026-10-10T12:00:00.000Z"},
        {"id": START, "value": 80, "ts": "2026-10-10T12:00:00.000Z"},
        {"id": STOP, "value": 60, "ts": "2026-10-10T12:00:00.000Z"},
    ]


# ---- which entities exist ------------------------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_the_controls_are_entities_with_the_scope_over_the_pinned_connection(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = _entry(tls_gateway)
    await _load(hass, tls_gateway, entry)

    assert _control_entities(hass, entry) == {
        ("select", MODE),
        ("switch", ENABLED),
        ("number", START),
        ("number", STOP),
    }
    # The assistant's device is not among the catalog's devices: registered from deviceName.
    assistant = device(hass, ASSISTANT)
    assert assistant is not None
    assert assistant.name == "Wallbox PV charging"
    gateway_device = device(hass, "gw-7f3a9c")
    assert gateway_device is not None
    assert assistant.via_device_id == gateway_device.id


@pytest.mark.usefixtures("fast_backoff")
async def test_no_control_entity_without_the_control_scope(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    tls_gateway.info["scopes"] = ["read", "inputs", "loads"]
    entry = _entry(tls_gateway)
    await _load(hass, tls_gateway, entry)

    assert _control_entities(hass, entry) == set()


@pytest.mark.usefixtures("fast_backoff")
async def test_no_control_entity_on_an_entry_without_the_pin(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    entry = _entry(gateway, pinned=False)
    await _load(hass, gateway, entry)

    assert _control_entities(hass, entry) == set()
    assert not [r for r in gateway.requests if r.path.endswith("/control")]


# The plugin has none of the control entities once the scope is gone (the plant's opt-in is off).
@pytest.mark.usefixtures("fast_backoff")
async def test_a_reload_without_the_scope_removes_the_control_entities(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = _entry(tls_gateway)
    await _load(hass, tls_gateway, entry)
    assert len(_control_entities(hass, entry)) == 4

    tls_gateway.info["scopes"] = ["read"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _control_entities(hass, entry) == set()
    assert device(hass, ASSISTANT) is None


# ---- state from the stream -----------------------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_the_state_is_the_stream_value_of_the_state_value_id(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    _with_control_values(tls_gateway)
    entry = _entry(tls_gateway)
    await _load(hass, tls_gateway, entry)

    mode = _entity_id(hass, "select", MODE)
    enabled = _entity_id(hass, "switch", ENABLED)
    stop = _entity_id(hass, "number", STOP)
    assert hass.states.get(mode).state == "pv-excess"
    assert hass.states.get(mode).attributes["options"] == ["off", "fast", "pv-excess"]
    assert hass.states.get(enabled).state == "on"
    assert float(hass.states.get(stop).state) == 60

    tls_gateway.push(
        "values",
        {
            "values": [
                {"id": MODE, "value": "fast", "ts": "2026-10-10T12:01:00.000Z"},
                {"id": ENABLED, "value": False, "ts": "2026-10-10T12:01:00.000Z"},
                {"id": STOP, "value": 45, "ts": "2026-10-10T12:01:00.000Z"},
            ]
        },
    )
    await wait_until(hass, lambda: hass.states.get(mode).state == "fast")
    assert hass.states.get(enabled).state == "off"
    assert float(hass.states.get(stop).state) == 45


@pytest.mark.usefixtures("fast_backoff")
async def test_a_number_takes_its_bounds_from_the_catalog(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    tls_gateway.catalog["controls"][3]["min"] = 20  # the gateway folded the plant's floor in
    entry = _entry(tls_gateway)
    await _load(hass, tls_gateway, entry)

    attributes = hass.states.get(_entity_id(hass, "number", STOP)).attributes
    assert (attributes["min"], attributes["max"], attributes["step"]) == (20, 100, 1)
    assert attributes["unit_of_measurement"] == "%"


# ---- actions -------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_each_action_posts_its_control_over_the_pinned_connection(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    _with_control_values(tls_gateway)
    entry = _entry(tls_gateway)
    await _load(hass, tls_gateway, entry)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": _entity_id(hass, "select", MODE), "option": "fast"},
        blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _entity_id(hass, "switch", ENABLED)}, blocking=True
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _entity_id(hass, "number", STOP), "value": 40},
        blocking=True,
    )

    assert tls_gateway.controls == [
        {"action": "mode", "assistantId": "pv-wallbox-1", "value": "fast"},
        {"action": "enabled", "assistantId": "pv-wallbox-1", "value": False},
        {"action": "battery_support_stop_soc", "assistantId": "pv-wallbox-1", "value": 40},
    ]
    # The 202's state is shown at once; the stream confirms it later.
    assert hass.states.get(_entity_id(hass, "select", MODE)).state == "fast"
    assert hass.states.get(_entity_id(hass, "switch", ENABLED)).state == "off"
    posts = [r for r in tls_gateway.requests if r.path.endswith("/control")]
    assert all(r.authorization == f"Bearer {tls_gateway.token}" for r in posts)


@pytest.mark.parametrize(
    ("status", "code", "retry_after"),
    [
        (422, "out_of_bounds", None),
        (422, "unknown_target", None),
        (422, "not_allowed", None),
        (503, "unavailable", None),
        (400, "invalid_request", None),
        (429, "rate_limited", 1),
    ],
)
@pytest.mark.usefixtures("fast_backoff")
async def test_a_refused_action_is_an_error_of_that_action_and_never_retried(
    hass: HomeAssistant,
    tls_gateway: FakeGateway,
    status: int,
    code: str,
    retry_after: int | None,
) -> None:
    _with_control_values(tls_gateway)
    entry = _entry(tls_gateway)
    await _load(hass, tls_gateway, entry)
    tls_gateway.control_status, tls_gateway.control_code = status, code
    tls_gateway.control_retry_after = retry_after
    stop = _entity_id(hass, "number", STOP)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": stop, "value": 40}, blocking=True
        )
    await hass.async_block_till_done()

    assert len(tls_gateway.controls) == 1  # no retry
    assert float(hass.states.get(stop).state) == 60  # the gateway's value stays
    assert hass.states.get(stop).state != "unavailable"
    assert _issue(hass, entry) is None


# Contract client.routeErrors: a 403 on /control disables the route, never the entry.
@pytest.mark.usefixtures("fast_backoff")
async def test_a_403_disables_control_raises_a_repair_issue_and_keeps_reading(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    _with_control_values(tls_gateway)
    entry = _entry(tls_gateway)
    await _load(hass, tls_gateway, entry)
    tls_gateway.control_status, tls_gateway.control_code = 403, "insufficient_scope"
    mode = _entity_id(hass, "select", MODE)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": mode, "option": "fast"}, blocking=True
        )
    await hass.async_block_till_done()

    assert _issue(hass, entry) is not None
    assert hass.states.get(mode).state == "unavailable"
    assert entry.runtime_data.coordinator.connected  # reading goes on
    assert not hass.config_entries.flow.async_progress()  # no reauth
    enabled = _entity_id(hass, "switch", ENABLED)
    assert hass.states.get(enabled).state == "unavailable"
    # Home Assistant skips (or, on some versions, refuses) a call on an unavailable entity.
    with suppress(HomeAssistantError):
        await hass.services.async_call("switch", "turn_on", {"entity_id": enabled}, blocking=True)
    assert len(tls_gateway.controls) == 1  # nothing more is posted


# ---- never in the clear --------------------------------------------------------------------------


async def test_the_client_never_posts_a_control_without_the_pin(gateway: FakeGateway) -> None:
    async with aiohttp.ClientSession() as session:
        client = EcoPulseApiClient(session, gateway.host, gateway.port, gateway.token)
        with pytest.raises(EcoPulseControlNeedsTlsError):
            await client.async_post_control("mode", "pv-wallbox-1", "fast")

    assert gateway.requests == []


async def test_the_pinned_client_posts_and_reads_the_answer(tls_gateway: FakeGateway) -> None:
    async with aiohttp.ClientSession() as session:
        client = EcoPulseApiClient(
            session,
            tls_gateway.host,
            tls_gateway.port,
            tls_gateway.token,
            fingerprint=tls_gateway.fingerprint,
        )
        result = await client.async_post_control("enabled", "pv-wallbox-1", True)

    assert (result.applied, result.state) == (True, True)
    assert tls_gateway.controls == [
        {"action": "enabled", "assistantId": "pv-wallbox-1", "value": True}
    ]


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_CONTROL_REFUSED}_{entry.entry_id}")
