"""Config flow: host + port + integration token, zeroconf discovery, reauth (ADR-230 §4, §8).

``GET /info`` is the credential check; the gateway id it returns is the entry's unique id.
With TLS (the default, #610 S8-TLS, ADR-232) the entry pins the gateway's certificate: its
SHA-256 is read from the handshake and confirmed by the user against the EcoPulse web app
before the token is sent, and a pinned entry never goes back to plain HTTP.
The options flow holds the settings of "Surplus available" (#623); they apply live, the
binary sensor listens for entry updates, so saving them does not reconnect the stream.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_ZEROCONF,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.util.ssl import get_default_no_verify_context
import voluptuous as vol

from .api import (
    EcoPulseApiClient,
    EcoPulseAuthError,
    EcoPulseCertificateMismatchError,
    EcoPulseConnectionError,
    EcoPulseNotEnabledError,
    EcoPulseRateLimitedError,
    async_fetch_certificate_fingerprint,
)
from .const import (
    API_VERSION,
    CONF_FINGERPRINT_MATCHES,
    CONF_SURPLUS_HYSTERESIS,
    CONF_SURPLUS_OFF_DELAY,
    CONF_SURPLUS_ON_DELAY,
    CONF_SURPLUS_THRESHOLD,
    CONF_TLS_FINGERPRINT,
    CONF_TLS_PORT,
    CONF_USE_TLS,
    DEFAULT_PORT,
    DEFAULT_SURPLUS_HYSTERESIS_W,
    DEFAULT_SURPLUS_OFF_DELAY_SECONDS,
    DEFAULT_SURPLUS_ON_DELAY_SECONDS,
    DEFAULT_SURPLUS_THRESHOLD_W,
    DEFAULT_TLS_PORT,
    DOMAIN,
    MAX_SURPLUS_DELAY_SECONDS,
    MAX_SURPLUS_POWER_W,
    TOKEN_PREFIX,
    ZEROCONF_TXT_API,
    ZEROCONF_TXT_ID,
)
from .models import GatewayInfo

_LOGGER = logging.getLogger(__name__)

_TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _whole_number(maximum: int, unit: str) -> vol.All:
    """A box with a unit; the selector checks the range, the coercion stores an int."""
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=0, max=maximum, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement=unit
            )
        ),
        vol.Coerce(int),
    )


_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SURPLUS_THRESHOLD): _whole_number(MAX_SURPLUS_POWER_W, "W"),
        vol.Required(CONF_SURPLUS_HYSTERESIS): _whole_number(MAX_SURPLUS_POWER_W, "W"),
        vol.Required(CONF_SURPLUS_ON_DELAY): _whole_number(MAX_SURPLUS_DELAY_SECONDS, "s"),
        vol.Required(CONF_SURPLUS_OFF_DELAY): _whole_number(MAX_SURPLUS_DELAY_SECONDS, "s"),
    }
)
_OPTION_DEFAULTS = {
    CONF_SURPLUS_THRESHOLD: DEFAULT_SURPLUS_THRESHOLD_W,
    CONF_SURPLUS_HYSTERESIS: DEFAULT_SURPLUS_HYSTERESIS_W,
    CONF_SURPLUS_ON_DELAY: DEFAULT_SURPLUS_ON_DELAY_SECONDS,
    CONF_SURPLUS_OFF_DELAY: DEFAULT_SURPLUS_OFF_DELAY_SECONDS,
}


class _FlowError(Exception):
    def __init__(self, error: str, field: str = "base") -> None:
        super().__init__(error)
        self.error = error
        self.field = field


def _port() -> vol.All:
    return vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


def _tls_fields(defaults: Mapping[str, Any]) -> dict[Any, Any]:
    """TLS on by default (#610 S8-TLS): plain HTTP stays possible, but only for reading."""
    return {
        vol.Required(CONF_USE_TLS, default=defaults.get(CONF_USE_TLS, True)): bool,
        vol.Required(CONF_TLS_PORT, default=defaults.get(CONF_TLS_PORT, DEFAULT_TLS_PORT)): _port(),
    }


class EcoPulseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Host, port and token; with TLS (the default, #610 S8-TLS, ADR-232) the certificate's
    fingerprint is read from the handshake and pinned only after the user confirmed it against
    the one the EcoPulse web app shows. The token is sent only after that confirmation."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EcoPulseOptionsFlow:
        return EcoPulseOptionsFlow()

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._token: str | None = None
        self._tls_port: int = DEFAULT_TLS_PORT
        self._fingerprint: str | None = None
        # The step that asked for the token; a failure after the pin was confirmed shows it again.
        self._origin: str = "user"
        self._origin_input: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host, self._port = user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            self._origin, self._origin_input = "user", user_input
            try:
                return await self._async_connect(user_input)
            except _FlowError as err:
                errors[err.field] = err.error
        return self._show_user(user_input or {}, errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        properties = discovery_info.properties
        gateway_id = (properties.get(ZEROCONF_TXT_ID) or "").strip()
        if not gateway_id:
            return self.async_abort(reason="invalid_discovery_info")
        majors = {part.strip() for part in (properties.get(ZEROCONF_TXT_API) or "").split(",")}
        if str(API_VERSION) not in majors:
            return self.async_abort(reason="unsupported_api_version")

        self._host = str(discovery_info.ip_address)
        self._port = discovery_info.port or DEFAULT_PORT
        await self.async_set_unique_id(gateway_id)
        if entry := self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, gateway_id):
            return await self._async_follow_announcement(entry)
        # TXT records carry no name (ADR-230 §8), so the id is what the discovery card shows.
        self.context["title_placeholders"] = {"name": gateway_id}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            self._origin, self._origin_input = "zeroconf_confirm", user_input
            try:
                return await self._async_connect(user_input)
            except _FlowError as err:
                errors[err.field] = err.error
        return self._show_zeroconf_confirm(user_input or {}, errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        self._host, self._port = entry.data[CONF_HOST], entry.data[CONF_PORT]
        errors: dict[str, str] = {}
        if user_input is not None:
            self._origin, self._origin_input = "reauth_confirm", user_input
            try:
                return await self._async_reauth(entry, user_input)
            except _FlowError as err:
                errors[err.field] = err.error
        return self._show_reauth_confirm(user_input or {}, errors)

    async def async_step_tls_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Shows the fingerprint read from the handshake; nothing has been sent yet."""
        return await self._async_confirm_pin("tls_confirm", user_input)

    async def async_step_tls_changed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth of a pinned entry whose gateway now presents another certificate."""
        return await self._async_confirm_pin("tls_changed", user_input)

    async def _async_follow_announcement(self, entry: ConfigEntry) -> ConfigFlowResult:
        """Review r1 MED-1: anyone on the LAN can announce a gateway's id, so an announcement moves
        a configured entry only when the new address proves to be the gateway: it presents the
        pinned certificate on the entry's TLS port (read from the handshake, nothing sent). A
        plain-HTTP entry never follows: the reload would send its token in the clear to the
        announcer."""
        assert self._host is not None
        pinned = entry.data.get(CONF_TLS_FINGERPRINT)
        if pinned and entry.data[CONF_HOST] != self._host:
            try:
                seen = await async_fetch_certificate_fingerprint(
                    self._host, entry.data[CONF_TLS_PORT], get_default_no_verify_context()
                )
            except EcoPulseConnectionError:
                seen = None
            if seen == pinned:
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: self._host, CONF_PORT: self._port}
                )
            _LOGGER.warning(
                "Ignoring an announcement of gateway %s at %s: it does not present the pinned "
                "certificate",
                entry.unique_id,
                self._host,
            )
        return self.async_abort(reason="already_configured")

    # -- the shared paths -------------------------------------------------------------------

    async def _async_connect(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        self._token = _checked_token(user_input[CONF_TOKEN])
        if not user_input.get(CONF_USE_TLS, True):
            info = await self._async_validate(self._client(fingerprint=None))
            return await self._async_finish(info, fingerprint=None)
        self._tls_port = user_input[CONF_TLS_PORT]
        self._fingerprint = await self._async_read_fingerprint()
        return await self.async_step_tls_confirm()

    async def _async_reauth(
        self, entry: ConfigEntry, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        self._token = _checked_token(user_input[CONF_TOKEN])
        pinned = entry.data.get(CONF_TLS_FINGERPRINT)
        if pinned:
            # A pinned entry stays pinned: no way back to plain HTTP from here.
            self._tls_port = entry.data[CONF_TLS_PORT]
            self._fingerprint = await self._async_read_fingerprint()
            if self._fingerprint != pinned:
                return await self.async_step_tls_changed()
            info = await self._async_validate_pinned(pinned)
            return await self._async_finish(info, fingerprint=pinned)
        return await self._async_connect(user_input)

    async def _async_confirm_pin(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        assert self._fingerprint is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_FINGERPRINT_MATCHES):
                errors["base"] = "fingerprint_not_confirmed"
            else:
                try:
                    info = await self._async_validate_pinned(self._fingerprint)
                except _FlowError as err:
                    return self._show_origin({err.field: err.error})
                return await self._async_finish(info, fingerprint=self._fingerprint)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(CONF_FINGERPRINT_MATCHES, default=False): bool}),
            description_placeholders={
                "fingerprint": self._fingerprint,
                "host": self._host or "",
                "port": str(self._tls_port),
            },
            errors=errors,
        )

    async def _async_finish(self, info: GatewayInfo, fingerprint: str | None) -> ConfigFlowResult:
        data: dict[str, Any] = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_TOKEN: self._token,
        }
        if fingerprint:
            data |= {CONF_TLS_PORT: self._tls_port, CONF_TLS_FINGERPRINT: fingerprint}

        if self.source == SOURCE_REAUTH:
            await self.async_set_unique_id(info.gateway_id)
            self._abort_if_unique_id_mismatch(reason="wrong_gateway")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        if self.source == SOURCE_ZEROCONF:
            if info.gateway_id != self.unique_id:
                return self.async_abort(reason="wrong_gateway")
            return self.async_create_entry(title=info.gateway_name, data=data)
        await self.async_set_unique_id(info.gateway_id)
        # An entry that is already pinned keeps its pin when the gateway is added again over
        # plain HTTP: the updates only add or replace, they never drop the fingerprint.
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title=info.gateway_name, data=data)

    def _client(self, fingerprint: str | None) -> EcoPulseApiClient:
        assert self._host is not None
        assert self._token is not None
        port = self._tls_port if fingerprint else self._port
        return EcoPulseApiClient(
            async_get_clientsession(self.hass),
            self._host,
            port,
            self._token,
            fingerprint=fingerprint,
        )

    async def _async_validate_pinned(self, fingerprint: str) -> GatewayInfo:
        info = await self._async_validate(self._client(fingerprint=fingerprint))
        if info.tls_fingerprint and info.tls_fingerprint != fingerprint:
            # The gateway names another certificate than the one it presented.
            raise _FlowError("certificate_mismatch")
        return info

    async def _async_read_fingerprint(self) -> str:
        assert self._host is not None
        try:
            return await async_fetch_certificate_fingerprint(
                self._host, self._tls_port, get_default_no_verify_context()
            )
        except EcoPulseConnectionError as err:
            raise _FlowError("cannot_connect_tls") from err

    async def _async_validate(self, client: EcoPulseApiClient) -> GatewayInfo:
        try:
            return await client.async_get_info()
        except EcoPulseCertificateMismatchError as err:
            # The certificate changed between the handshake the user confirmed and this request.
            raise _FlowError("certificate_mismatch") from err
        except EcoPulseAuthError as err:
            raise _FlowError("invalid_auth") from err
        except EcoPulseNotEnabledError as err:
            raise _FlowError("not_enabled") from err
        except EcoPulseRateLimitedError as err:
            raise _FlowError("rate_limited") from err
        except EcoPulseConnectionError as err:
            raise _FlowError("cannot_connect_tls" if client.pinned else "cannot_connect") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error validating the gateway")
            raise _FlowError("unknown") from err

    # -- forms ------------------------------------------------------------------------------

    def _show_origin(self, errors: dict[str, str]) -> ConfigFlowResult:
        if self._origin == "zeroconf_confirm":
            return self._show_zeroconf_confirm(self._origin_input, errors)
        if self._origin == "reauth_confirm":
            return self._show_reauth_confirm(self._origin_input, errors)
        return self._show_user(self._origin_input, errors)

    def _show_user(self, defaults: Mapping[str, Any], errors: dict[str, str]) -> ConfigFlowResult:
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): _port(),
                vol.Required(CONF_TOKEN): _TOKEN_SELECTOR,
                **_tls_fields(defaults),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    def _show_zeroconf_confirm(
        self, defaults: Mapping[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_TOKEN): _TOKEN_SELECTOR, **_tls_fields(defaults)}
            ),
            description_placeholders={"host": self._host or "", "id": self.unique_id or ""},
            errors=errors,
        )

    def _show_reauth_confirm(
        self, defaults: Mapping[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        fields: dict[Any, Any] = {vol.Required(CONF_TOKEN): _TOKEN_SELECTOR}
        if not entry.data.get(CONF_TLS_FINGERPRINT):
            # A plain-HTTP entry is offered the switch to TLS (the way out of tls_required).
            fields |= _tls_fields(defaults)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(fields),
            description_placeholders={"name": entry.title},
            errors=errors,
        )


def _checked_token(raw: str) -> str:
    # Every failed token check counts in the gateway's lockout (ADR-230 §3); something that
    # cannot be an integration token (e.g. the web password) is refused before the request.
    token = raw.strip()
    if not token.startswith(TOKEN_PREFIX):
        raise _FlowError("invalid_token_format", CONF_TOKEN)
    return token


class EcoPulseOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _OPTIONS_SCHEMA, {**_OPTION_DEFAULTS, **self.config_entry.options}
            ),
        )
