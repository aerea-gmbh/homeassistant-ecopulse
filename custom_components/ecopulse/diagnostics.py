"""Diagnostics: entry, catalog summary and stream state; never the token.

The gateway name is redacted too: it can be personal ("Familie Müller", ADR-230 §4) and
diagnostics end up in public issue trackers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from . import EcoPulseConfigEntry

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EcoPulseConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    catalog = coordinator.catalog
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "unique_id": entry.unique_id,
            "title": REDACTED,
        },
        "catalog": {
            "revision": catalog.revision,
            "gateway_id": catalog.gateway_id,
            "gateway_name": REDACTED,
            "firmware_version": catalog.firmware_version,
            "device_count": len(catalog.devices),
            "entity_count": sum(len(d.entities) for d in catalog.devices),
            "devices": [
                {
                    "id": device.id,
                    "manufacturer": device.manufacturer,
                    "model": device.model,
                    "entities": [
                        {
                            "unique_id": e.unique_id,
                            "platform": e.platform,
                            "unit": e.unit,
                            "device_class": e.device_class,
                            "state_class": e.state_class,
                            "entity_category": e.entity_category,
                            "enabled_by_default": e.enabled_by_default,
                        }
                        for e in device.entities
                    ],
                }
                for device in catalog.devices
            ],
        },
        "stream": {
            "connected": coordinator.connected,
            "values_known": len(coordinator.values),
            "unavailable_devices": [
                d.id for d in catalog.devices if not coordinator.is_device_available(d.id)
            ],
        },
    }
