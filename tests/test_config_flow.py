"""Config flow: manual setup, zeroconf discovery, reauth (ADR-230 §4, §8)."""

from __future__ import annotations

from collections.abc import Generator
from ipaddress import ip_address
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.const import DOMAIN

from .fake_gateway import FakeGateway

WRONG_TOKEN = "ecp_ha_" + "x" * 43


@pytest.fixture(autouse=True)
def no_setup() -> Generator[None]:
    """Flows create entries; setting them up is covered in test_init."""
    with patch("custom_components.ecopulse.async_setup_entry", return_value=True):
        yield


async def _user_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


def _input(gateway: FakeGateway, token: str | None = None) -> dict:
    return {CONF_HOST: gateway.host, CONF_PORT: gateway.port, CONF_TOKEN: token or gateway.token}


async def test_user_flow_creates_an_entry_keyed_by_gateway_id(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    flow_id = await _user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, _input(gateway))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Haus Nord"
    assert result["data"] == _input(gateway)
    assert result["result"].unique_id == "gw-7f3a9c"
    assert gateway.requests[0].path == "/api/ha/v1/info"


async def test_whitespace_around_the_pasted_token_is_stripped(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    flow_id = await _user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, _input(gateway, token=f"  {gateway.token}\n")
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == gateway.token


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda g: None, "invalid_auth"),
        (lambda g: setattr(g, "forced_status", 403), "invalid_auth"),
        (lambda g: setattr(g, "enabled", False), "not_enabled"),
        (lambda g: setattr(g, "rate_limited_for", 30), "rate_limited"),
        (lambda g: setattr(g, "forced_status", 500), "cannot_connect"),
    ],
    ids=["401", "403", "404", "429", "500"],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, gateway: FakeGateway, setup, error: str
) -> None:
    flow_id = await _user_flow(hass)
    setup(gateway)
    token = WRONG_TOKEN if error == "invalid_auth" and gateway.forced_status is None else None

    result = await hass.config_entries.flow.async_configure(flow_id, _input(gateway, token))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    gateway.forced_status = None
    gateway.enabled = True
    gateway.rate_limited_for = None
    result = await hass.config_entries.flow.async_configure(flow_id, _input(gateway))
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unreachable_host_is_cannot_connect(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    flow_id = await _user_flow(hass)
    user_input = _input(gateway)
    await gateway.close()

    result = await hass.config_entries.flow.async_configure(flow_id, user_input)

    assert result["errors"] == {"base": "cannot_connect"}


async def test_a_token_without_the_prefix_is_rejected_before_any_request(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    # Each failed token check feeds the gateway's lockout (ADR-230 §3); a pasted web password
    # must not cost an attempt.
    flow_id = await _user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, _input(gateway, token="my-web-password")
    )

    assert result["errors"] == {CONF_TOKEN: "invalid_token_format"}
    assert gateway.requests == []


async def test_a_configured_gateway_aborts_and_takes_the_new_address_and_token(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    # e.g. the entry sits in reauth after a rotation and the user adds the gateway again
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gw-7f3a9c",
        data={CONF_HOST: "10.0.0.99", CONF_PORT: 8080, CONF_TOKEN: WRONG_TOKEN},
    )
    entry.add_to_hass(hass)
    flow_id = await _user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, _input(gateway))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == gateway.host
    assert entry.data[CONF_PORT] == gateway.port
    assert entry.data[CONF_TOKEN] == gateway.token


def _discovery(gateway: FakeGateway, **properties: str) -> ZeroconfServiceInfo:
    txt = {"id": "gw-7f3a9c", "api": "1", "path": "/api/ha/v1", **properties}
    return ZeroconfServiceInfo(
        ip_address=ip_address(gateway.host),
        ip_addresses=[ip_address(gateway.host)],
        port=gateway.port,
        hostname="ecopulse-gw.local.",
        type="_ecopulse._tcp.local.",
        name="ecopulse-gw._ecopulse._tcp.local.",
        properties=txt,
    )


async def test_zeroconf_prefills_the_host_and_asks_only_for_the_token(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_discovery(gateway)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert list(result["data_schema"].schema) == [CONF_TOKEN]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: WRONG_TOKEN}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: gateway.token}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "gw-7f3a9c"
    assert result["data"] == _input(gateway)


async def test_zeroconf_for_a_configured_gateway_updates_its_address(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gw-7f3a9c",
        data={CONF_HOST: "10.0.0.99", CONF_PORT: 8080, CONF_TOKEN: gateway.token},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_discovery(gateway)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == gateway.host
    assert entry.data[CONF_PORT] == gateway.port


@pytest.mark.parametrize("properties", [{"api": "2"}, {"id": ""}], ids=["api-2-only", "no-id"])
async def test_zeroconf_rejects_an_announcement_it_cannot_use(
    hass: HomeAssistant, gateway: FakeGateway, properties: dict[str, str]
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_discovery(gateway, **properties)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] in {"unsupported_api_version", "invalid_discovery_info"}


async def test_zeroconf_accepts_a_gateway_that_speaks_several_majors(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_discovery(gateway, api="1,2")
    )

    assert result["step_id"] == "zeroconf_confirm"


async def test_zeroconf_token_for_another_gateway_aborts(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    gateway.info["gatewayId"] = "gw-other"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_discovery(gateway)
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: gateway.token}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_gateway"


async def _start_reauth(hass: HomeAssistant, gateway: FakeGateway) -> tuple[MockConfigEntry, str]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gw-7f3a9c",
        data={CONF_HOST: gateway.host, CONF_PORT: gateway.port, CONF_TOKEN: WRONG_TOKEN},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    return entry, result["flow_id"]


async def test_reauth_stores_the_rotated_token(hass: HomeAssistant, gateway: FakeGateway) -> None:
    entry, flow_id = await _start_reauth(hass, gateway)

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_TOKEN: WRONG_TOKEN})
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_TOKEN: gateway.token})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == gateway.token


async def test_reauth_with_a_token_of_another_gateway_aborts(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    entry, flow_id = await _start_reauth(hass, gateway)
    gateway.info["gatewayId"] = "gw-other"

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_TOKEN: gateway.token})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_gateway"
    assert entry.data[CONF_TOKEN] == WRONG_TOKEN
