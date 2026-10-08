"""EcoPulse: every signal of an EcoPulse EdgeGateway, live, over the gateway's local API."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .api import EcoPulseApiClient, EcoPulseAuthError, EcoPulseError
from .const import DOMAIN, GATEWAY_MODEL, MANUFACTURER
from .coordinator import EcoPulseStreamCoordinator
from .models import Catalog

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


@dataclass(slots=True)
class EcoPulseRuntimeData:
    client: EcoPulseApiClient
    coordinator: EcoPulseStreamCoordinator


type EcoPulseConfigEntry = ConfigEntry[EcoPulseRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: EcoPulseConfigEntry) -> bool:
    client = EcoPulseApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_TOKEN],
    )
    try:
        catalog = await client.async_get_catalog()
    except EcoPulseAuthError as err:
        raise ConfigEntryAuthFailed("The gateway rejected the integration token") from err
    except EcoPulseError as err:
        raise ConfigEntryNotReady(f"Gateway not ready: {err}") from err
    if catalog.gateway_id != entry.unique_id:
        # DHCP handed the address to another gateway; zeroconf will bring the right address.
        raise ConfigEntryNotReady(f"Found gateway {catalog.gateway_id} at this address")

    _register_gateway_device(hass, entry, catalog)
    _remove_stale_registry_entries(hass, entry, catalog)

    coordinator = EcoPulseStreamCoordinator(hass, entry, client, catalog)
    entry.runtime_data = EcoPulseRuntimeData(client, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EcoPulseConfigEntry) -> bool:
    # The stream task was created with entry.async_create_background_task and is cancelled
    # by Home Assistant when the entry unloads.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


@callback
def _register_gateway_device(hass: HomeAssistant, entry: ConfigEntry, catalog: Catalog) -> None:
    """The gateway is the parent (``via_device``) of every aggregate device."""
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, catalog.gateway_id)},
        name=catalog.gateway_name,
        manufacturer=MANUFACTURER,
        model=GATEWAY_MODEL,
        sw_version=catalog.firmware_version,
        configuration_url=str(
            URL.build(scheme="http", host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])
        ),
    )


@callback
def _remove_stale_registry_entries(
    hass: HomeAssistant, entry: ConfigEntry, catalog: Catalog
) -> None:
    """Drop entities and devices the catalog no longer has (signal or aggregate deleted)."""
    entity_registry = er.async_get(hass)
    current_ids = set(catalog.device_of())
    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if registry_entry.unique_id not in current_ids:
            entity_registry.async_remove(registry_entry.entity_id)

    device_registry = dr.async_get(hass)
    current_devices = {catalog.gateway_id} | {device.id for device in catalog.devices}
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
        if not identifiers & current_devices:
            device_registry.async_remove_device(device.id)
