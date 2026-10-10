"""Typed views of the /info and /catalog responses (ADR-230 §4).

Unknown properties are ignored (additive changes within v1, ADR-230 §10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import GATEWAY_ENTITY_UNIQUE_ID_TEMPLATE


@dataclass(frozen=True, slots=True)
class GatewayInfo:
    """``GET /info``."""

    gateway_id: str
    gateway_name: str
    firmware_version: str | None
    catalog_revision: str
    supported_api_versions: tuple[int, ...]
    scopes: tuple[str, ...]
    # Schema 4 (#610 S8-TLS): where the gateway serves the API over TLS; None on an older
    # gateway or while it serves no TLS port.
    tls_port: int | None = None
    tls_fingerprint: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayInfo:
        transport = data.get("transport") or {}
        return cls(
            gateway_id=str(data["gatewayId"]),
            gateway_name=str(data.get("gatewayName") or data["gatewayId"]),
            firmware_version=data.get("firmwareVersion"),
            catalog_revision=str(data["catalogRevision"]),
            supported_api_versions=tuple(data.get("supportedApiVersions") or ()),
            scopes=tuple(data.get("scopes") or ()),
            tls_port=transport.get("tlsPort"),
            tls_fingerprint=transport.get("certificateSha256"),
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
class CatalogInput:
    """One input slot (schema 5, #624): ``key`` is what a post names, ``unique_id`` (the signal
    Guid) what the options map to a Home Assistant entity; it survives renames."""

    key: str
    unique_id: str
    name: str
    device_name: str
    unit: str | None
    data_type: str
    stale_after_seconds: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CatalogInput:
        return cls(
            key=str(data["key"]),
            unique_id=str(data["uniqueId"]),
            name=str(data["name"]),
            device_name=str(data.get("deviceName") or ""),
            unit=data.get("unit"),
            data_type=str(data["dataType"]),
            stale_after_seconds=int(data["staleAfterSeconds"]),
        )


@dataclass(frozen=True, slots=True)
class CatalogLoad:
    """One load EcoPulse may switch (schema 6, #625): ``unique_id`` (the device Guid) is what a
    command names and what the options map to a Home Assistant entity; it survives renames."""

    key: str
    unique_id: str
    name: str
    channel: int
    nominal_power_w: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CatalogLoad:
        return cls(
            key=str(data["key"]),
            unique_id=str(data["uniqueId"]),
            name=str(data["name"]),
            channel=int(data["channel"]),
            nominal_power_w=float(data["nominalPowerW"]),
        )


@dataclass(frozen=True, slots=True)
class Catalog:
    """``GET /catalog``."""

    revision: str
    gateway_id: str
    gateway_name: str
    firmware_version: str | None
    devices: tuple[CatalogDevice, ...]
    # The gateway's own entities (schema 2, #623): on the gateway device, in no device of the
    # availability event. Absent before schema 2, so a schema 1 gateway simply has none.
    gateway_entities: tuple[CatalogEntity, ...]
    # The input slots (schema 5, #624): absent before schema 5 and while the token lacks "inputs".
    inputs: tuple[CatalogInput, ...] = ()
    # The loads (schema 6, #625): absent before schema 6 and while the token lacks "loads".
    loads: tuple[CatalogLoad, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        gateway = data.get("gateway") or {}
        return cls(
            revision=str(data["catalogRevision"]),
            gateway_id=str(gateway["id"]),
            gateway_name=str(gateway.get("name") or gateway["id"]),
            firmware_version=gateway.get("firmwareVersion"),
            devices=tuple(CatalogDevice.from_dict(d) for d in data.get("devices") or ()),
            gateway_entities=tuple(
                CatalogEntity.from_dict(e) for e in gateway.get("entities") or ()
            ),
            inputs=tuple(CatalogInput.from_dict(i) for i in data.get("inputs") or ()),
            loads=tuple(CatalogLoad.from_dict(lo) for lo in data.get("loads") or ()),
        )

    @classmethod
    def pending(cls, gateway_id: str, gateway_name: str) -> Catalog:
        """Stand-in while the gateway answers 404/429; its empty revision matches no snapshot."""
        return cls(
            revision="",
            gateway_id=gateway_id,
            gateway_name=gateway_name,
            firmware_version=None,
            devices=(),
            gateway_entities=(),
        )

    def entities_for(self, platform: str) -> list[tuple[CatalogDevice, CatalogEntity]]:
        return [
            (device, entity)
            for device in self.devices
            for entity in device.entities
            if entity.platform == platform
        ]

    def gateway_entities_for(self, platform: str) -> list[CatalogEntity]:
        return [entity for entity in self.gateway_entities if entity.platform == platform]

    def gateway_entity(self, key: str) -> CatalogEntity | None:
        unique_id = GATEWAY_ENTITY_UNIQUE_ID_TEMPLATE.format(gatewayId=self.gateway_id, key=key)
        return next((e for e in self.gateway_entities if e.unique_id == unique_id), None)

    def device_of(self) -> dict[str, str]:
        """Map each entity's unique id to its device id."""
        return {e.unique_id: d.id for d in self.devices for e in d.entities}
