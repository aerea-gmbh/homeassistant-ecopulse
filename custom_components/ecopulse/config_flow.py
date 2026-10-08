"""Config flow: host + port + integration token, zeroconf discovery, reauth (ADR-230 §4, §8).

``GET /info`` is the credential check; the gateway id it returns is the entry's unique id.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import voluptuous as vol

from .api import (
    EcoPulseApiClient,
    EcoPulseAuthError,
    EcoPulseConnectionError,
    EcoPulseNotEnabledError,
    EcoPulseRateLimitedError,
)
from .const import (
    API_VERSION,
    DEFAULT_PORT,
    DOMAIN,
    TOKEN_PREFIX,
    ZEROCONF_TXT_API,
    ZEROCONF_TXT_ID,
)
from .models import GatewayInfo

_LOGGER = logging.getLogger(__name__)

_TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class _FlowError(Exception):
    def __init__(self, error: str, field: str = "base") -> None:
        super().__init__(error)
        self.error = error
        self.field = field


class EcoPulseConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            token = user_input[CONF_TOKEN].strip()
            try:
                info = await self._async_validate(host, port, token)
            except _FlowError as err:
                errors[err.field] = err.error
            else:
                await self.async_set_unique_id(info.gateway_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
                return self._create(info, host, port, token)

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_TOKEN): _TOKEN_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

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
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
        # TXT records carry no name (ADR-230 §8), so the id is what the discovery card shows.
        self.context["title_placeholders"] = {"name": gateway_id}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            try:
                info = await self._async_validate(self._host, self._port, token)
            except _FlowError as err:
                errors[err.field] = err.error
            else:
                if info.gateway_id != self.unique_id:
                    return self.async_abort(reason="wrong_gateway")
                return self._create(info, self._host, self._port, token)

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): _TOKEN_SELECTOR}),
            description_placeholders={"host": self._host, "id": self.unique_id or ""},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            try:
                info = await self._async_validate(
                    entry.data[CONF_HOST], entry.data[CONF_PORT], token
                )
            except _FlowError as err:
                errors[err.field] = err.error
            else:
                await self.async_set_unique_id(info.gateway_id)
                self._abort_if_unique_id_mismatch(reason="wrong_gateway")
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): _TOKEN_SELECTOR}),
            description_placeholders={"name": entry.title},
            errors=errors,
        )

    def _create(self, info: GatewayInfo, host: str, port: int, token: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=info.gateway_name,
            data={CONF_HOST: host, CONF_PORT: port, CONF_TOKEN: token},
        )

    async def _async_validate(self, host: str, port: int, token: str) -> GatewayInfo:
        # Every failed token check counts in the gateway's lockout (ADR-230 §3); something that
        # cannot be an integration token (e.g. the web password) is refused before the request.
        if not token.startswith(TOKEN_PREFIX):
            raise _FlowError("invalid_token_format", CONF_TOKEN)
        client = EcoPulseApiClient(async_get_clientsession(self.hass), host, port, token)
        try:
            return await client.async_get_info()
        except EcoPulseAuthError as err:
            raise _FlowError("invalid_auth") from err
        except EcoPulseNotEnabledError as err:
            raise _FlowError("not_enabled") from err
        except EcoPulseRateLimitedError as err:
            raise _FlowError("rate_limited") from err
        except EcoPulseConnectionError as err:
            raise _FlowError("cannot_connect") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error validating the gateway")
            raise _FlowError("unknown") from err
