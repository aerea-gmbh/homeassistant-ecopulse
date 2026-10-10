"""EcoPulse switches Home Assistant loads (schema 6, #625, ADR-232 S6).

What must hold: a ``command`` on the pinned TLS stream switches the mapped entity with its domain's
``turn_on``/``turn_off`` and is acknowledged with the entity's state; an unmapped load or a failing
service is acknowledged ``ok: false`` with the reason, at once; a plain-HTTP entry never acts on a
command; a 403 on the ack stops switching (repair issue, no reauth); the handler goes with the
entry; the options flow maps entities to the catalog's loads and never offers EcoPulse's own.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.const import (
    ACK_MAX_ERROR_LENGTH,
    CONF_LOADS,
    CONF_TLS_FINGERPRINT,
    CONF_TLS_PORT,
    DOMAIN,
)
from custom_components.ecopulse.loads import NOT_MAPPED, error_text

from .conftest import wait_until
from .fake_gateway import FakeGateway

POOL_PUMP = "8c4d5e6f-7a81-4c9d-8e0f-2a3b4c5d6e7f"  # catalog example, channel 1, 1200 W
HEATING_ROD = "9d5e6f70-8b92-4dae-9f10-3b4c5d6e7f80"  # channel 0, 3000 W
COMMAND_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
MAPPING = {POOL_PUMP: "input_boolean.pool_pump"}


def _entry(
    gateway: FakeGateway, options: dict[str, Any] | None = None, *, pinned: bool = True
) -> MockConfigEntry:
    data: dict[str, Any] = {
        CONF_HOST: gateway.host,
        CONF_PORT: gateway.port,
        CONF_TOKEN: gateway.token,
    }
    if pinned:
        data |= {CONF_TLS_PORT: gateway.port, CONF_TLS_FINGERPRINT: gateway.fingerprint}
    return MockConfigEntry(
        domain=DOMAIN,
        title="Haus Nord",
        unique_id="gw-7f3a9c",
        data=data,
        options={CONF_LOADS: MAPPING} if options is None else options,
    )


async def _load(
    hass: HomeAssistant, gateway: FakeGateway, entry: MockConfigEntry
) -> MockConfigEntry:
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"pool_pump": None}}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await gateway.wait_for_stream(1)
    await wait_until(hass, lambda: entry.runtime_data.coordinator.connected)
    return entry


def _command(load: str, action: str, command_id: str = COMMAND_ID) -> dict[str, Any]:
    return {
        "commandId": command_id,
        "load": load,
        "action": action,
        "issuedAt": "2026-10-10T12:00:00Z",
    }


def _issue(hass: HomeAssistant, issue: str, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{issue}_{entry.entry_id}")


# ---- switching over the pinned connection --------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_a_command_switches_the_mapped_entity_and_acknowledges_its_state(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    await _load(hass, tls_gateway, _entry(tls_gateway))

    tls_gateway.push("command", _command(POOL_PUMP, "on"))
    await tls_gateway.wait_for_acks(1)

    assert hass.states.get("input_boolean.pool_pump").state == "on"
    assert tls_gateway.acks[0] == (COMMAND_ID, {"ok": True, "state": "on"})

    tls_gateway.push("command", _command(POOL_PUMP, "off", "a" * 32))
    await tls_gateway.wait_for_acks(2)

    assert hass.states.get("input_boolean.pool_pump").state == "off"
    assert tls_gateway.acks[1] == ("a" * 32, {"ok": True, "state": "off"})


# The gateway's apply waits up to its timeout for an ack; a load nobody mapped is answered at once.
@pytest.mark.usefixtures("fast_backoff")
async def test_a_command_for_an_unmapped_load_is_acknowledged_as_failed_at_once(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    await _load(hass, tls_gateway, _entry(tls_gateway))

    tls_gateway.push("command", _command(HEATING_ROD, "on"))
    await tls_gateway.wait_for_acks(1)

    assert tls_gateway.acks[0] == (COMMAND_ID, {"ok": False, "state": None, "error": NOT_MAPPED})


# A service that fails is reported with its reason (cut to the contract's length), never as done.
@pytest.mark.usefixtures("fast_backoff")
async def test_a_failing_service_is_acknowledged_as_failed_with_its_reason(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    async def broken(_call: ServiceCall) -> None:
        raise HomeAssistantError("The relay does not answer " + "x" * 400)

    hass.services.async_register("switch", "turn_on", broken)
    await _load(hass, tls_gateway, _entry(tls_gateway, {CONF_LOADS: {POOL_PUMP: "switch.broken"}}))

    tls_gateway.push("command", _command(POOL_PUMP, "on"))
    await tls_gateway.wait_for_acks(1)

    ack = tls_gateway.acks[0][1]
    assert ack["ok"] is False
    assert ack["error"].startswith("The relay does not answer")
    assert len(ack["error"]) == ACK_MAX_ERROR_LENGTH


@pytest.mark.usefixtures("fast_backoff")
async def test_a_command_without_a_valid_id_is_ignored_and_an_unknown_action_is_refused(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    await _load(hass, tls_gateway, _entry(tls_gateway))

    tls_gateway.push("command", _command(POOL_PUMP, "on", "not-an-id"))
    tls_gateway.push("command", _command(POOL_PUMP, "toggle"))
    await tls_gateway.wait_for_acks(1)
    await hass.async_block_till_done()

    assert tls_gateway.acks == [
        (COMMAND_ID, {"ok": False, "state": None, "error": "Unknown action 'toggle'."})
    ]
    assert hass.states.get("input_boolean.pool_pump").state == "off"


# ---- the TLS rule -------------------------------------------------------------------------------


# A command on a stream without the pinned certificate is not the gateway's: nothing is switched,
# nothing acknowledged, and a mapping that cannot work says so (the positive half: the first test).
@pytest.mark.usefixtures("fast_backoff")
async def test_a_plain_http_entry_never_acts_on_a_command_and_raises_a_repair_issue(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    entry = await _load(hass, gateway, _entry(gateway, pinned=False))

    gateway.push("command", _command(POOL_PUMP, "on"))
    gateway.push("values", {"values": []})  # a later event: the command was read and passed over
    await hass.async_block_till_done()

    assert hass.states.get("input_boolean.pool_pump").state == "off"
    assert gateway.acks == []
    assert _issue(hass, "loads_need_tls", entry) is not None


# ---- refusals ------------------------------------------------------------------------------------


# Contract client.routeErrors: a 403 on the ack stops switching and says why; reading goes on and no
# reauth starts.
@pytest.mark.usefixtures("fast_backoff")
async def test_a_403_on_the_ack_stops_switching_with_a_repair_issue_and_no_reauth(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    tls_gateway.ack_status = 403
    entry = await _load(hass, tls_gateway, _entry(tls_gateway))

    tls_gateway.push("command", _command(POOL_PUMP, "on"))
    await wait_until(hass, lambda: entry.runtime_data.loads.disabled)
    tls_gateway.push("command", _command(POOL_PUMP, "off", "b" * 32))
    await hass.async_block_till_done()

    assert len(tls_gateway.acks) == 1, "no command is handled after the 403"
    assert hass.states.get("input_boolean.pool_pump").state == "on"
    issue = _issue(hass, "loads_refused", entry)
    assert issue is not None
    assert issue.translation_placeholders == {"name": "Haus Nord", "code": "insufficient_scope"}
    assert entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN), "no reauth"


# Another Home Assistant answered first (or the command timed out): dropped, nothing raised.
@pytest.mark.usefixtures("fast_backoff")
async def test_an_ack_the_gateway_no_longer_waits_for_is_dropped(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    tls_gateway.ack_status = 404
    entry = await _load(hass, tls_gateway, _entry(tls_gateway))

    tls_gateway.push("command", _command(POOL_PUMP, "on"))
    await tls_gateway.wait_for_acks(1)
    await hass.async_block_till_done()

    assert not entry.runtime_data.loads.disabled
    assert _issue(hass, "loads_refused", entry) is None


# ---- lifecycle ----------------------------------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_unloading_the_entry_removes_the_command_handler(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = await _load(hass, tls_gateway, _entry(tls_gateway))
    coordinator = entry.runtime_data.coordinator
    assert coordinator._command_handler is not None, "premise: the handler is registered"

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert coordinator._command_handler is None


# ---- the options flow ---------------------------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_the_loads_step_maps_switchable_entities_to_the_loads_and_hides_ecopulse_s_own(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = await _load(hass, tls_gateway, _entry(tls_gateway, options={}))
    menu = await hass.config_entries.options.async_init(entry.entry_id)

    form = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "loads"}
    )

    assert form["type"] is FlowResultType.FORM
    fields = {str(key): key for key in form["data_schema"].schema}
    assert list(fields) == ["Heating rod (3000 W)", "Pool pump (1200 W)"]
    selector = form["data_schema"].schema[fields["Pool pump (1200 W)"]].config
    own = {
        e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert own, "premise: the entry has entities of its own"
    assert set(selector["exclude_entities"]) == own
    assert selector["domain"] == ["switch", "input_boolean", "light", "fan"]

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"Pool pump (1200 W)": "input_boolean.pool_pump"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_LOADS] == {POOL_PUMP: "input_boolean.pool_pump"}


@pytest.mark.usefixtures("fast_backoff")
async def test_a_gateway_without_loads_says_how_to_get_them(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    tls_gateway.catalog.pop("loads")
    entry = await _load(hass, tls_gateway, _entry(tls_gateway, options={}))
    menu = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "loads"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_loads"


# A changed mapping reloads the entry; the new mapping switches.
@pytest.mark.usefixtures("fast_backoff")
async def test_saving_a_new_mapping_reloads_and_switches_it(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = await _load(hass, tls_gateway, _entry(tls_gateway, options={}))

    hass.config_entries.async_update_entry(entry, options={CONF_LOADS: MAPPING})
    await hass.async_block_till_done()
    await tls_gateway.wait_for_stream(2)
    tls_gateway.push("command", _command(POOL_PUMP, "on"))
    await tls_gateway.wait_for_acks(1)

    assert tls_gateway.acks[0] == (COMMAND_ID, {"ok": True, "state": "on"})


# The ack must fit the gateway's body cap: 256 characters of emoji would not.
def test_an_error_text_fits_the_ack_body_whatever_its_characters() -> None:
    text = error_text(HomeAssistantError("\U0001f525" * 400))

    body = json.dumps({"ok": False, "state": "off", "error": text}, ensure_ascii=False)
    assert len(body.encode()) <= 1024
    assert text.startswith("\U0001f525")
    assert error_text(HomeAssistantError("")) == "HomeAssistantError"
