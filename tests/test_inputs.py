"""Values from Home Assistant into EcoPulse (schema 5, #624, ADR-232 S5).

What must hold: a mapped entity's state reaches the gateway in the slot's unit, on change and on
the contract's re-post interval, only over the pinned TLS connection; a unit no converter turns
into the slot's is never sent (repair issue); a 403 stops the posts and raises a repair issue
without touching reading or starting a reauth; the options flow maps entities to the catalog's
slots and never offers EcoPulse's own entities.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ecopulse.const import (
    CONF_INPUTS,
    CONF_TLS_FINGERPRINT,
    CONF_TLS_PORT,
    DOMAIN,
    INPUTS_REPOST_SECONDS,
)
from custom_components.ecopulse.inputs import convert
from custom_components.ecopulse.models import CatalogInput

from .conftest import wait_until
from .fake_gateway import FakeGateway

TEMPERATURE_SLOT = "6a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
RAINING_SLOT = "7b3c4d5e-6f70-4b8c-9d0e-1f2a3b4c5d6e"
TEMPERATURE = "weather.outdoorTemperature"
RAINING = "weather.raining"
MAPPING = {TEMPERATURE_SLOT: "sensor.outdoor", RAINING_SLOT: "binary_sensor.rain"}

NUMBER_SLOT = CatalogInput(
    TEMPERATURE, TEMPERATURE_SLOT, "Outdoor temperature", "Weather", "°C", "number", 60
)
BOOLEAN_SLOT = CatalogInput(RAINING, RAINING_SLOT, "Raining", "Weather", None, "boolean", 60)


@pytest.fixture(autouse=True)
def fast_flush() -> Generator[None]:
    with patch("custom_components.ecopulse.inputs.CHANGE_FLUSH_DELAY_SECONDS", 0):
        yield


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
        options={CONF_INPUTS: MAPPING} if options is None else options,
    )


async def _load(
    hass: HomeAssistant, gateway: FakeGateway, entry: MockConfigEntry
) -> MockConfigEntry:
    hass.states.async_set("sensor.outdoor", "54.5", {"unit_of_measurement": "°F"})
    hass.states.async_set("binary_sensor.rain", "on")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await gateway.wait_for_stream(1)
    await wait_until(hass, lambda: entry.runtime_data.coordinator.connected)
    return entry


async def _repost(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=INPUTS_REPOST_SECONDS + 1))
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, issue: str, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{issue}_{entry.entry_id}")


# ---- the conversion (pure) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "attributes", "slot", "expected"),
    [
        ("54.5", {"unit_of_measurement": "°F"}, NUMBER_SLOT, (12.5, None)),
        ("21", {"unit_of_measurement": "°C"}, NUMBER_SLOT, (21.0, None)),
        ("21", {}, NUMBER_SLOT, (21.0, None)),  # an input_number without unit: the slot's
        ("1.5", {"unit_of_measurement": "kW"}, NUMBER_SLOT, (None, "kW")),
        ("unavailable", {"unit_of_measurement": "°C"}, NUMBER_SLOT, (None, None)),
        ("unknown", {}, NUMBER_SLOT, (None, None)),
        ("warm", {}, NUMBER_SLOT, (None, None)),
        ("on", {}, BOOLEAN_SLOT, (True, None)),
        ("off", {}, BOOLEAN_SLOT, (False, None)),
        ("unavailable", {}, BOOLEAN_SLOT, (None, None)),
    ],
)
def test_a_state_is_converted_into_the_slot_s_type_and_unit(
    state: str, attributes: dict[str, Any], slot: CatalogInput, expected: tuple[Any, str | None]
) -> None:
    converted = convert(State("sensor.x", state, attributes), slot)

    assert (converted.value, converted.unit_error) == expected


def test_kilowatts_become_watts_and_a_unit_on_a_unit_less_slot_is_refused() -> None:
    watts = CatalogInput("m.p", "u", "Power", "M", "W", "number", 60)
    unitless = CatalogInput("m.c", "v", "Count", "M", None, "number", 60)

    assert convert(State("sensor.p", "1.5", {"unit_of_measurement": "kW"}), watts).value == 1500
    assert convert(State("sensor.c", "3", {"unit_of_measurement": "%"}), unitless).unit_error == "%"


# ---- posting over the pinned connection ---------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_the_mapped_states_are_posted_converted_over_tls_and_again_on_the_repost(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    await _load(hass, tls_gateway, _entry(tls_gateway))

    await tls_gateway.wait_for_posts(1)
    assert tls_gateway.posted_inputs[0] == {
        "inputs": [
            {"key": TEMPERATURE, "value": 12.5, "unit": "°C"},
            {"key": RAINING, "value": True},
        ]
    }

    await _repost(hass)
    await tls_gateway.wait_for_posts(2)
    assert tls_gateway.posted_inputs[1] == tls_gateway.posted_inputs[0], (
        "a constant value is re-sent"
    )


@pytest.mark.usefixtures("fast_backoff")
async def test_a_state_change_is_posted_and_an_unavailable_entity_as_null(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    await _load(hass, tls_gateway, _entry(tls_gateway))
    await tls_gateway.wait_for_posts(1)

    hass.states.async_set("sensor.outdoor", "50", {"unit_of_measurement": "°F"})
    await tls_gateway.wait_for_posts(2)
    hass.states.async_set("binary_sensor.rain", "unavailable")
    await tls_gateway.wait_for_posts(3)

    assert tls_gateway.posted_inputs[1] == {
        "inputs": [{"key": TEMPERATURE, "value": 10.0, "unit": "°C"}]
    }
    assert tls_gateway.posted_inputs[2] == {"inputs": [{"key": RAINING, "value": None}]}


# A unit no converter turns into the slot's: that slot is never posted, the others are, and a
# repair issue names the entity; it goes away once the unit converts again.
@pytest.mark.usefixtures("fast_backoff")
async def test_a_unit_that_does_not_convert_is_not_sent_and_raises_a_repair_issue(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = _entry(tls_gateway)
    hass.states.async_set("sensor.outdoor", "54.5", {"unit_of_measurement": "°F"})
    await _load(hass, tls_gateway, entry)
    await tls_gateway.wait_for_posts(1)

    hass.states.async_set("sensor.outdoor", "3", {"unit_of_measurement": "kWh"})
    await _repost(hass)
    await tls_gateway.wait_for_posts(2)

    assert tls_gateway.posted_inputs[-1] == {"inputs": [{"key": RAINING, "value": True}]}
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"input_unit_{TEMPERATURE_SLOT}_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders == {
        "entity": "sensor.outdoor",
        "unit": "kWh",
        "slot": "Outdoor temperature",
        "slot_unit": "°C",
    }

    hass.states.async_set("sensor.outdoor", "21", {"unit_of_measurement": "°C"})
    await tls_gateway.wait_for_posts(3)
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"input_unit_{TEMPERATURE_SLOT}_{entry.entry_id}"
        )
        is None
    )


# Contract client.routeErrors: a 403 on /inputs disables posting and says why, but reading goes on
# and no reauth starts (the token is fine; the toggle in the web app is off).
@pytest.mark.usefixtures("fast_backoff")
async def test_a_403_stops_the_inputs_with_a_repair_issue_and_no_reauth(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    tls_gateway.inputs_status = 403
    entry = await _load(hass, tls_gateway, _entry(tls_gateway))
    await tls_gateway.wait_for_posts(1)
    await wait_until(hass, lambda: entry.runtime_data.inputs.disabled)

    await _repost(hass)
    hass.states.async_set("sensor.outdoor", "40", {"unit_of_measurement": "°F"})
    await hass.async_block_till_done()

    assert len(tls_gateway.posted_inputs) == 1, "nothing is posted after the 403"
    issue = _issue(hass, "inputs_refused", entry)
    assert issue is not None
    assert issue.translation_placeholders == {"name": "Haus Nord", "code": "insufficient_scope"}
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.connected, "reading goes on"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN), "no reauth"


# A rate-limited post is not a refusal: the next re-post sends again; a 202 clears an old refusal.
@pytest.mark.usefixtures("fast_backoff")
async def test_a_429_keeps_posting_on_the_next_repost(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    tls_gateway.inputs_status = 429
    tls_gateway.inputs_retry_after = 1
    entry = await _load(hass, tls_gateway, _entry(tls_gateway))
    await tls_gateway.wait_for_posts(1)

    tls_gateway.inputs_status = None
    await _repost(hass)
    await tls_gateway.wait_for_posts(2)

    assert not entry.runtime_data.inputs.disabled
    assert _issue(hass, "inputs_refused", entry) is None


# The scope works only over TLS: a plain-HTTP entry with a mapping sends nothing and says so.
@pytest.mark.usefixtures("fast_backoff")
async def test_a_plain_http_entry_posts_nothing_and_raises_a_repair_issue(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    entry = await _load(hass, gateway, _entry(gateway, pinned=False))

    await _repost(hass)

    assert gateway.posted_inputs == []
    assert _issue(hass, "inputs_need_tls", entry) is not None


# The positive half of the TLS rule's issue: an entry without a mapping posts nothing either, and
# is told nothing (it did not ask to send values).
@pytest.mark.usefixtures("fast_backoff")
async def test_without_a_mapping_nothing_is_posted_and_no_issue_is_raised(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    entry = await _load(hass, gateway, _entry(gateway, options={}, pinned=False))

    await _repost(hass)

    assert gateway.posted_inputs == []
    assert _issue(hass, "inputs_need_tls", entry) is None


# ---- the options flow ---------------------------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_the_inputs_step_maps_entities_to_the_catalog_s_slots_and_hides_ecopulse_s_own(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = await _load(hass, tls_gateway, _entry(tls_gateway, options={}))
    menu = await hass.config_entries.options.async_init(entry.entry_id)

    form = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "inputs"}
    )

    assert form["type"] is FlowResultType.FORM
    fields = {str(key): key for key in form["data_schema"].schema}
    assert list(fields) == ["Weather: Outdoor temperature (°C)", "Weather: Raining"]
    selector = form["data_schema"].schema[fields["Weather: Outdoor temperature (°C)"]].config
    own = {
        e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert own, "premise: the entry has entities of its own"
    assert set(selector["exclude_entities"]) == own
    assert selector["domain"] == ["sensor", "number", "input_number"]
    assert form["data_schema"].schema[fields["Weather: Raining"]].config["domain"] == [
        "binary_sensor",
        "input_boolean",
        "switch",
    ]

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"Weather: Outdoor temperature (°C)": "sensor.outdoor"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_INPUTS] == {TEMPERATURE_SLOT: "sensor.outdoor"}


@pytest.mark.usefixtures("fast_backoff")
async def test_a_gateway_without_slots_says_how_to_get_them(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    tls_gateway.catalog.pop("inputs")
    entry = await _load(hass, tls_gateway, _entry(tls_gateway, options={}))
    menu = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "inputs"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_inputs"


# A changed mapping needs new subscriptions: the entry reloads and posts the new mapping.
@pytest.mark.usefixtures("fast_backoff")
async def test_saving_a_new_mapping_reloads_and_posts_it(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = await _load(hass, tls_gateway, _entry(tls_gateway, options={}))
    hass.states.async_set("sensor.other", "20", {"unit_of_measurement": "°C"})

    hass.config_entries.async_update_entry(
        entry, options={CONF_INPUTS: {TEMPERATURE_SLOT: "sensor.other"}}
    )
    await hass.async_block_till_done()
    await tls_gateway.wait_for_posts(1)

    assert tls_gateway.posted_inputs[0] == {
        "inputs": [{"key": TEMPERATURE, "value": 20.0, "unit": "°C"}]
    }
