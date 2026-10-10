"""The pinned TLS connection (#610 S8-TLS, ADR-232): the client, the config flow and setup.

What must hold: the token goes out only over a connection whose certificate is the pinned one;
the first pin is the fingerprint read from the handshake and confirmed by the user before the
token is sent; another certificate means no request and a reauth; a pinned entry never speaks
plain HTTP again.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.ssl import get_default_no_verify_context
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.api import (
    EcoPulseApiClient,
    EcoPulseCertificateMismatchError,
    EcoPulseConnectionError,
    async_fetch_certificate_fingerprint,
    client_for,
)
from custom_components.ecopulse.const import (
    CONF_FINGERPRINT_MATCHES,
    CONF_TLS_FINGERPRINT,
    CONF_TLS_PORT,
    CONF_USE_TLS,
    DOMAIN,
)

from .conftest import wait_until
from .fake_gateway import FakeGateway

OTHER_FINGERPRINT = ":".join(["AB"] * 32)
WRONG_TOKEN = "ecp_ha_" + "x" * 43


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client_session:
        yield client_session


def _pinned(
    session: aiohttp.ClientSession, gateway: FakeGateway, fingerprint: str | None = None
) -> EcoPulseApiClient:
    return EcoPulseApiClient(
        session,
        gateway.host,
        gateway.port,
        gateway.token,
        fingerprint=fingerprint or gateway.fingerprint,
    )


# -- the client ---------------------------------------------------------------------------------


async def test_a_pinned_client_reads_over_https_with_the_token(
    session: aiohttp.ClientSession, tls_gateway: FakeGateway
) -> None:
    client = _pinned(session, tls_gateway)

    info = await client.async_get_info()

    assert client.base_url.startswith("https://")
    assert info.gateway_id == "gw-7f3a9c"
    assert info.tls_fingerprint == tls_gateway.fingerprint
    assert tls_gateway.requests[-1].authorization == f"Bearer {tls_gateway.token}"


@pytest.mark.parametrize("call", ["info", "stream"])
async def test_another_certificate_than_the_pinned_one_sends_nothing(
    session: aiohttp.ClientSession, tls_gateway: FakeGateway, call: str
) -> None:
    client = _pinned(session, tls_gateway, OTHER_FINGERPRINT)

    async def consume() -> None:
        if call == "info":
            await client.async_get_info()
            return
        async for _ in client.async_stream():
            pass

    with pytest.raises(EcoPulseCertificateMismatchError) as raised:
        await consume()

    assert raised.value.seen == tls_gateway.fingerprint
    assert tls_gateway.requests == [], "the token must not reach a certificate that is not pinned"


async def test_a_pinned_client_never_falls_back_to_plain_http(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    # The same gateway on plain HTTP: a pinned client fails instead of asking it in the clear.
    client = _pinned(session, gateway, OTHER_FINGERPRINT)

    with pytest.raises(EcoPulseConnectionError):
        await client.async_get_info()

    assert gateway.requests == []
    # Positive control: the unpinned client gets an answer from this very server.
    assert (await client_for(session, _http_data(gateway)).async_get_info()).gateway_id


async def test_the_handshake_fingerprint_is_read_without_sending_anything(
    tls_gateway: FakeGateway,
) -> None:
    fingerprint = await async_fetch_certificate_fingerprint(
        tls_gateway.host, tls_gateway.port, get_default_no_verify_context()
    )

    assert fingerprint == tls_gateway.fingerprint
    assert tls_gateway.requests == []


async def test_a_closed_tls_port_is_a_connection_error(tls_gateway: FakeGateway) -> None:
    port = tls_gateway.port
    await tls_gateway.close()

    with pytest.raises(EcoPulseConnectionError):
        await async_fetch_certificate_fingerprint(
            tls_gateway.host, port, get_default_no_verify_context()
        )


async def test_entry_data_with_a_fingerprint_makes_a_pinned_client_on_the_tls_port(
    session: aiohttp.ClientSession,
) -> None:
    http = client_for(session, {CONF_HOST: "10.0.0.5", CONF_PORT: 8080, CONF_TOKEN: WRONG_TOKEN})
    pinned = client_for(
        session,
        {
            CONF_HOST: "10.0.0.5",
            CONF_PORT: 8080,
            CONF_TOKEN: WRONG_TOKEN,
            CONF_TLS_PORT: 8443,
            CONF_TLS_FINGERPRINT: OTHER_FINGERPRINT,
        },
    )

    assert (http.pinned, http.base_url) == (False, "http://10.0.0.5:8080/api/ha/v1")
    assert (pinned.pinned, pinned.base_url) == (True, "https://10.0.0.5:8443/api/ha/v1")


def test_a_malformed_fingerprint_is_refused() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        EcoPulseApiClient(None, "h", 1, WRONG_TOKEN, fingerprint="ab:cd")  # type: ignore[arg-type]


# -- the config flow ------------------------------------------------------------------------------


@pytest.fixture
def no_setup() -> Generator[None]:
    with patch("custom_components.ecopulse.async_setup_entry", return_value=True):
        yield


def _http_data(gateway: FakeGateway) -> dict:
    return {CONF_HOST: gateway.host, CONF_PORT: gateway.port, CONF_TOKEN: gateway.token}


def _tls_input(gateway: FakeGateway, token: str | None = None) -> dict:
    return {
        CONF_HOST: gateway.host,
        CONF_PORT: 8080,
        CONF_TOKEN: token or gateway.token,
        CONF_USE_TLS: True,
        CONF_TLS_PORT: gateway.port,
    }


async def _to_confirmation(hass: HomeAssistant, gateway: FakeGateway, token: str | None = None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], _tls_input(gateway, token)
    )


@pytest.mark.usefixtures("no_setup")
async def test_tls_is_the_default_of_the_user_form(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    schema = result["data_schema"]({CONF_HOST: "h", CONF_PORT: 8080, CONF_TOKEN: WRONG_TOKEN})
    assert schema[CONF_USE_TLS] is True
    assert schema[CONF_TLS_PORT] == 8443


@pytest.mark.usefixtures("no_setup")
async def test_the_user_confirms_the_handshake_fingerprint_before_the_token_is_sent(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    result = await _to_confirmation(hass, tls_gateway)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tls_confirm"
    assert result["description_placeholders"]["fingerprint"] == tls_gateway.fingerprint
    assert tls_gateway.requests == [], "nothing is sent before the user confirmed the pin"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FINGERPRINT_MATCHES: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_HOST: tls_gateway.host,
        CONF_PORT: 8080,
        CONF_TOKEN: tls_gateway.token,
        CONF_TLS_PORT: tls_gateway.port,
        CONF_TLS_FINGERPRINT: tls_gateway.fingerprint,
    }
    assert tls_gateway.requests[0].authorization == f"Bearer {tls_gateway.token}"


@pytest.mark.usefixtures("no_setup")
async def test_without_the_confirmation_nothing_is_sent(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    result = await _to_confirmation(hass, tls_gateway)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FINGERPRINT_MATCHES: False}
    )

    assert result["step_id"] == "tls_confirm"
    assert result["errors"] == {"base": "fingerprint_not_confirmed"}
    assert tls_gateway.requests == []


@pytest.mark.usefixtures("no_setup")
async def test_a_certificate_swapped_after_the_confirmation_gets_no_token(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    result = await _to_confirmation(hass, tls_gateway)
    await tls_gateway.restart_with_a_new_certificate()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FINGERPRINT_MATCHES: True}
    )

    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "certificate_mismatch"}
    assert tls_gateway.requests == []


@pytest.mark.usefixtures("no_setup")
async def test_a_gateway_that_names_another_certificate_than_it_presents_is_refused(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    result = await _to_confirmation(hass, tls_gateway)
    tls_gateway.info["transport"]["certificateSha256"] = OTHER_FINGERPRINT

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FINGERPRINT_MATCHES: True}
    )

    assert result["errors"] == {"base": "certificate_mismatch"}
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.usefixtures("no_setup")
async def test_a_wrong_token_after_the_pin_returns_to_the_form_with_the_error(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    result = await _to_confirmation(hass, tls_gateway, token=WRONG_TOKEN)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FINGERPRINT_MATCHES: True}
    )

    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.usefixtures("no_setup")
async def test_no_tls_port_is_cannot_connect_tls_before_anything_is_sent(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    # The plain-HTTP gateway's port does not speak TLS: the handshake fails, no token is sent.
    result = await _to_confirmation(hass, gateway)

    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect_tls"}
    assert gateway.requests == []


@pytest.mark.usefixtures("no_setup")
async def test_adding_a_pinned_gateway_again_over_http_keeps_the_pin(
    hass: HomeAssistant, gateway: FakeGateway
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gw-7f3a9c",
        data={
            **_http_data(gateway),
            CONF_TOKEN: WRONG_TOKEN,
            CONF_TLS_PORT: 8443,
            CONF_TLS_FINGERPRINT: OTHER_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_http_data(gateway), CONF_USE_TLS: False}
    )

    assert result["reason"] == "already_configured"
    assert entry.data[CONF_TLS_FINGERPRINT] == OTHER_FINGERPRINT
    assert entry.data[CONF_TLS_PORT] == 8443


def _pinned_entry(gateway: FakeGateway, fingerprint: str | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Haus Nord",
        unique_id="gw-7f3a9c",
        data={
            CONF_HOST: gateway.host,
            CONF_PORT: 8080,
            CONF_TOKEN: gateway.token,
            CONF_TLS_PORT: gateway.port,
            CONF_TLS_FINGERPRINT: fingerprint or gateway.fingerprint,
        },
    )


@pytest.mark.usefixtures("no_setup")
async def test_reauth_of_a_pinned_entry_keeps_the_pin_and_offers_no_plain_http(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = _pinned_entry(tls_gateway)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert list(result["data_schema"].schema) == [CONF_TOKEN]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: tls_gateway.token}
    )

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TLS_FINGERPRINT] == tls_gateway.fingerprint


@pytest.mark.usefixtures("no_setup")
async def test_reauth_after_a_new_certificate_asks_to_confirm_it_before_the_token_is_sent(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = _pinned_entry(tls_gateway, OTHER_FINGERPRINT)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: tls_gateway.token}
    )

    assert result["step_id"] == "tls_changed"
    assert result["description_placeholders"]["fingerprint"] == tls_gateway.fingerprint
    assert tls_gateway.requests == []

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FINGERPRINT_MATCHES: True}
    )

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TLS_FINGERPRINT] == tls_gateway.fingerprint


@pytest.mark.usefixtures("no_setup")
async def test_reauth_of_a_plain_http_entry_can_switch_it_to_tls(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gw-7f3a9c",
        data={CONF_HOST: tls_gateway.host, CONF_PORT: 8080, CONF_TOKEN: WRONG_TOKEN},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOKEN: tls_gateway.token, CONF_USE_TLS: True, CONF_TLS_PORT: tls_gateway.port},
    )
    assert result["step_id"] == "tls_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FINGERPRINT_MATCHES: True}
    )

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TLS_FINGERPRINT] == tls_gateway.fingerprint
    assert entry.data[CONF_TLS_PORT] == tls_gateway.port
    assert entry.data[CONF_PORT] == 8080


# -- setup and the stream ------------------------------------------------------------------------


@pytest.mark.usefixtures("fast_backoff")
async def test_a_pinned_entry_loads_and_streams_over_tls(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = _pinned_entry(tls_gateway)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await tls_gateway.wait_for_stream(1)

    assert entry.state is ConfigEntryState.LOADED
    assert {r.path for r in tls_gateway.requests} >= {"/api/ha/v1/catalog", "/api/ha/v1/stream"}
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("fast_backoff")
async def test_setup_against_another_certificate_starts_reauth_and_sends_nothing(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = _pinned_entry(tls_gateway, OTHER_FINGERPRINT)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(entry.async_get_active_flows(hass, {"reauth"}))
    assert tls_gateway.requests == []


@pytest.mark.usefixtures("fast_backoff")
async def test_a_certificate_change_while_streaming_starts_reauth_and_stops_reconnecting(
    hass: HomeAssistant, tls_gateway: FakeGateway
) -> None:
    entry = _pinned_entry(tls_gateway)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await tls_gateway.wait_for_stream(1)
    requests_before = len(tls_gateway.requests)

    await tls_gateway.restart_with_a_new_certificate()
    await wait_until(hass, lambda: any(entry.async_get_active_flows(hass, {"reauth"})))

    assert len(tls_gateway.requests) == requests_before, "no request to the new certificate"
    await hass.config_entries.async_unload(entry.entry_id)
