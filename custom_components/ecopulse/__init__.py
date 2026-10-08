"""EcoPulse: every signal of an EcoPulse EdgeGateway, live, over the gateway's local API."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .api import (
    EcoPulseApiClient,
    EcoPulseAuthError,
    EcoPulseError,
    EcoPulseNotEnabledError,
    EcoPulseRateLimitedError,
)
from .const import BACKOFF_INITIAL_SECONDS, DOMAIN, GATEWAY_MODEL, MANUFACTURER
from .coordinator import EcoPulseStreamCoordinator, wait_after_failure
from .models import Catalog

_LOGGER = logging.getLogger(__name__)

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
    initial_wait = 0.0
    try:
        catalog = await client.async_get_catalog()
    except EcoPulseAuthError as err:
        raise ConfigEntryAuthFailed("The gateway rejected the integration token") from err
    except (EcoPulseNotEnabledError, EcoPulseRateLimitedError) as err:
        # The contract's client actions (retry-later 300 s, honour Retry-After) instead of HA's
        # setup-retry schedule: load without entities and let the stream loop wait. Its first
        # snapshot carries a revision the pending catalog cannot match, so the entry reloads
        # with the real catalog once the gateway answers.
        _LOGGER.info("Gateway not serving Home Assistant yet (%s), waiting", type(err).__name__)
        catalog = Catalog.pending(entry.unique_id or "", entry.title)
        initial_wait = wait_after_failure(err, BACKOFF_INITIAL_SECONDS)
    except EcoPulseError as err:
        raise ConfigEntryNotReady(f"Gateway not ready: {err}") from err
    if catalog.gateway_id != entry.unique_id:
        # DHCP handed the address to another gateway; zeroconf will bring the right address.
        raise ConfigEntryNotReady(f"Found gateway {catalog.gateway_id} at this address")

    if catalog.devices:
        _register_gateway_device(hass, entry, catalog)
        _remove_stale_registry_entries(hass, entry, catalog)
    else:
        # Never prune on an empty catalog (a push that rendered nothing, a projection bug, the
        # pending catalog above): it would wipe every customisation and hand-enabled entity.
        _LOGGER.warning("The gateway serves no devices; keeping all existing entities")

    coordinator = EcoPulseStreamCoordinator(hass, entry, client, catalog)
    entry.runtime_data = EcoPulseRuntimeData(client, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start(initial_wait)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: EcoPulseConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device by hand only when the gateway no longer serves it."""
    catalog = entry.runtime_data.coordinator.catalog
    current = {catalog.gateway_id} | {d.id for d in catalog.devices}
    return not any(domain == DOMAIN and value in current for domain, value in device.identifiers)


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
    # (platform, unique id): a signal that moved sensor <-> binary_sensor keeps its unique id,
    # and its entry on the old platform would otherwise stay behind as an orphan.
    current_ids = {(e.platform, e.unique_id) for d in catalog.devices for e in d.entities}
    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if (registry_entry.domain, registry_entry.unique_id) not in current_ids:
            entity_registry.async_remove(registry_entry.entity_id)

    device_registry = dr.async_get(hass)
    current_devices = {catalog.gateway_id} | {device.id for device in catalog.devices}
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
        if not identifiers & current_devices:
            device_registry.async_remove_device(device.id)
