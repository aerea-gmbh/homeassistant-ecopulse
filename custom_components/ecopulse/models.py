"""Typed views of the /info and /catalog responses (ADR-230 §4).

Unknown properties are ignored (additive changes within v1, ADR-230 §10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GatewayInfo:
    """``GET /info``."""

    gateway_id: str
    gateway_name: str
    firmware_version: str | None
    catalog_revision: str
    supported_api_versions: tuple[int, ...]
    scopes: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayInfo:
        return cls(
            gateway_id=str(data["gatewayId"]),
            gateway_name=str(data.get("gatewayName") or data["gatewayId"]),
            firmware_version=data.get("firmwareVersion"),
            catalog_revision=str(data["catalogRevision"]),
            supported_api_versions=tuple(data.get("supportedApiVersions") or ()),
            scopes=tuple(data.get("scopes") or ()),
        )


@dataclass(frozen=True, slots=True)
class CatalogEntity:
    """One signal; ``unique_id`` is the signal Guid and survives renames."""

    unique_id: str
    name: str
    platform: str
    unit: str | None
    device_class: str | None
    state_class: str | None
    suggested_display_precision: int | None
    entity_category: str | None
    enabled_by_default: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CatalogEntity:
        return cls(
            unique_id=str(data["uniqueId"]),
            name=str(data["name"]),
            platform=str(data["platform"]),
            unit=data.get("unit"),
            device_class=data.get("deviceClass"),
            state_class=data.get("stateClass"),
            suggested_display_precision=data.get("suggestedDisplayPrecision"),
            entity_category=data.get("entityCategory"),
            enabled_by_default=bool(data.get("enabledByDefault", True)),
        )


@dataclass(frozen=True, slots=True)
class CatalogDevice:
    """One aggregate; ``id`` is the aggregate Guid."""

    id: str
    name: str
    manufacturer: str | None
    model: str | None
    entities: tuple[CatalogEntity, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CatalogDevice:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            manufacturer=data.get("manufacturer"),
            model=data.get("model"),
            entities=tuple(CatalogEntity.from_dict(e) for e in data.get("entities") or ()),
        )


@dataclass(frozen=True, slots=True)
class Catalog:
    """``GET /catalog``."""

    revision: str
    gateway_id: str
    gateway_name: str
    firmware_version: str | None
    devices: tuple[CatalogDevice, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        gateway = data.get("gateway") or {}
        return cls(
            revision=str(data["catalogRevision"]),
            gateway_id=str(gateway["id"]),
            gateway_name=str(gateway.get("name") or gateway["id"]),
            firmware_version=gateway.get("firmwareVersion"),
            devices=tuple(CatalogDevice.from_dict(d) for d in data.get("devices") or ()),
        )

    def entities_for(self, platform: str) -> list[tuple[CatalogDevice, CatalogEntity]]:
        return [
            (device, entity)
            for device in self.devices
            for entity in device.entities
            if entity.platform == platform
        ]

    def device_of(self) -> dict[str, str]:
        """Map each entity's unique id to its device id."""
        return {e.unique_id: d.id for d in self.devices for e in d.entities}
