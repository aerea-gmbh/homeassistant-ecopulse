"""The plugin's copy of the shared contract and every constant the plugin derives from it.

The fixture is a byte-identical copy of HomeAssistant/ha-api-contract.json from the cloud
(EcoPulse.Domain.Shared) and the edge (EdgeGateway.Contracts). The cross-repo check (#545) does not
cover this repository yet, so the sha256 pin below is what makes a drift visible: when the contract
changes, this test turns red until the fixture and the plugin are updated together.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.ecopulse import api, const

from .fake_gateway import CONTRACT_PATH

CONTRACT_SHA256 = "b68946f709feba29e2ead3c8326798a5fea64b8390f93472d354d31fff1c1fcb"
COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "ecopulse"
MANIFEST_PATH = COMPONENT_PATH / "manifest.json"
STRINGS_PATH = COMPONENT_PATH / "strings.json"
TRANSLATIONS_PATH = COMPONENT_PATH / "translations"


def test_fixture_is_the_pinned_contract() -> None:
    assert hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest() == CONTRACT_SHA256


def test_schema_and_api_version_are_the_ones_this_plugin_speaks(contract: dict[str, Any]) -> None:
    assert contract["schemaVersion"] == 3
    assert contract["apiVersion"] == const.API_VERSION
    assert contract["basePath"] == const.API_BASE_PATH


def test_gateway_entities_are_keyed_by_the_contract_template(contract: dict[str, Any]) -> None:
    # Schema 2 (#623): the gateway's own entities, unique id "{gatewayId}:{key}".
    section = contract["gatewayEntities"]
    gateway = contract["examples"]["catalog"]["gateway"]
    keys = [e["key"] for e in section["entities"]]
    assert section["uniqueIdTemplate"] == const.GATEWAY_ENTITY_UNIQUE_ID_TEMPLATE
    assert keys[0] == const.SOLAR_EXCESS_POWER_KEY
    example_ids = [e["uniqueId"] for e in gateway["entities"]]
    assert example_ids[: len(keys)] == [
        const.GATEWAY_ENTITY_UNIQUE_ID_TEMPLATE.format(gatewayId=gateway["id"], key=key)
        for key in keys
    ]


def test_the_translated_gateway_entities_are_the_contract_s_problem_sensors(
    contract: dict[str, Any],
) -> None:
    # Schema 3 (#626, ADR-231 §10): the gateway's own diagnostic binary sensors, named by the
    # integration's translations; every one has a name in every language.
    by_key = {e["key"]: e for e in contract["gatewayEntities"]["entities"]}
    for key in const.PROBLEM_SENSOR_KEYS:
        assert by_key[key]["platform"] == "binary_sensor"
        assert by_key[key]["entityCategory"] == "diagnostic"
        assert by_key[key]["source"] == "edge"
    assert {
        const.CLOUD_CONNECTED_KEY,
        const.DRIVER_PROBLEM_KEY,
        const.DIRECTIVE_FALLBACK_KEY,
    } == const.PROBLEM_SENSOR_KEYS
    for language in ("en", "de"):
        names = json.loads((TRANSLATIONS_PATH / f"{language}.json").read_text(encoding="utf-8"))
        for key in const.PROBLEM_SENSOR_KEYS:
            assert names["entity"]["binary_sensor"][key]["name"], (language, key)
    strings = json.loads(STRINGS_PATH.read_text(encoding="utf-8"))
    english = json.loads((TRANSLATIONS_PATH / "en.json").read_text(encoding="utf-8"))
    assert strings == english


def test_the_forecast_sensors_are_the_contract_s_forecast_entities(
    contract: dict[str, Any],
) -> None:
    # Schema 3 (#626 S7.2, ADR-231 §12): every sensor fed by "forecast.summary" is named by the
    # integration's translations, with a name for each of its attributes in every language.
    section = contract["gatewayEntities"]
    forecast = {e["key"]: e for e in section["entities"] if e["cloudFact"] == "forecast"}
    assert frozenset(forecast) == const.FORECAST_ENTITY_KEYS
    assert const.FORECAST_ENTITY_KEYS <= const.TRANSLATED_GATEWAY_ENTITY_KEYS
    cloud_attributes = [a["key"] for a in section["cloudAttributes"]]
    for language in ("en", "de"):
        names = json.loads((TRANSLATIONS_PATH / f"{language}.json").read_text(encoding="utf-8"))
        for key, definition in forecast.items():
            assert definition["platform"] == "sensor"
            entry = names["entity"]["sensor"][key]
            assert entry["name"], (language, key)
            expected = cloud_attributes + [a["key"] for a in definition["attributes"]]
            assert sorted(entry["state_attributes"]) == sorted(expected), (language, key)
            assert all(v["name"] for v in entry["state_attributes"].values()), (language, key)


def test_the_plan_sensors_are_the_contract_s_plan_entities(contract: dict[str, Any]) -> None:
    # Schema 3 (#626 S7.3, ADR-231 §12): every sensor fed by "battery.plan" or "heatpump.plan" is
    # named by the integration's translations, with a name for each of its attributes in every
    # language; the translated keys are exactly the problem, forecast and plan sensors.
    section = contract["gatewayEntities"]
    plans = {
        e["key"]: e
        for e in section["entities"]
        if e["cloudFact"] in ("batteryPlan", "heatpumpPlan")
    }
    assert frozenset(plans) == const.PLAN_ENTITY_KEYS
    assert const.TRANSLATED_GATEWAY_ENTITY_KEYS == (
        const.PROBLEM_SENSOR_KEYS | const.FORECAST_ENTITY_KEYS | const.PLAN_ENTITY_KEYS
    )
    cloud_attributes = [a["key"] for a in section["cloudAttributes"]]
    for language in ("en", "de"):
        names = json.loads((TRANSLATIONS_PATH / f"{language}.json").read_text(encoding="utf-8"))
        for key, definition in plans.items():
            assert definition["platform"] == "sensor"
            entry = names["entity"]["sensor"][key]
            assert entry["name"], (language, key)
            expected = cloud_attributes + [a["key"] for a in definition["attributes"]]
            assert sorted(entry["state_attributes"]) == sorted(expected), (language, key)
            assert all(v["name"] for v in entry["state_attributes"].values()), (language, key)


def test_value_attributes_and_their_unrecorded_keys_match(contract: dict[str, Any]) -> None:
    # Schema 3 (#626, ADR-231 §12): "attributes" is the one optional value property; the
    # unrecorded keys are every array attribute plus synced_at.
    stream = contract["stream"]
    assert stream["optionalValueProperties"] == ["attributes"]
    assert stream["attributes"]["semantics"] == "replace"
    assert frozenset(stream["attributes"]["unrecordedKeys"]) == const.UNRECORDED_ATTRIBUTE_KEYS
    assert len(stream["attributes"]["unrecordedKeys"]) == len(const.UNRECORDED_ATTRIBUTE_KEYS)


def test_auth_constants_match(contract: dict[str, Any]) -> None:
    auth = contract["auth"]
    assert auth["tokenPrefix"] == const.TOKEN_PREFIX
    assert auth["realm"] == const.AUTH_REALM
    assert (auth["header"], auth["scheme"]) == ("Authorization", "Bearer")


def test_example_token_hashes_by_the_contract_rule(contract: dict[str, Any]) -> None:
    # The plugin never hashes, but the rule is shared by all three repos; keeps the copy honest.
    token = contract["examples"]["token"]
    assert hashlib.sha256(token["token"].encode()).hexdigest() == token["hash"]
    assert token["token"].startswith(const.TOKEN_PREFIX)


def test_client_timings_match(contract: dict[str, Any]) -> None:
    client, stream = contract["client"], contract["stream"]
    assert client["backoffInitialSeconds"] == const.BACKOFF_INITIAL_SECONDS
    assert client["backoffMaxSeconds"] == const.BACKOFF_MAX_SECONDS
    assert client["retryLaterSeconds"] == const.RETRY_LATER_SECONDS
    assert stream["clientDeadAfterSeconds"] == const.STREAM_DEAD_AFTER_SECONDS
    assert client["onSnapshotRevisionMismatch"] == "refetch"
    assert client["onUnknownValueId"] == "ignore"


def test_stream_event_names_match(contract: dict[str, Any]) -> None:
    assert contract["stream"]["events"] == [
        const.EVENT_SNAPSHOT,
        const.EVENT_VALUES,
        const.EVENT_AVAILABILITY,
        const.EVENT_CATALOG,
    ]


def test_manifest_announces_the_contract_zeroconf_type(contract: dict[str, Any]) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert contract["zeroconf"]["serviceType"] == const.ZEROCONF_SERVICE_TYPE
    assert manifest["zeroconf"] == [const.ZEROCONF_SERVICE_TYPE]
    assert contract["zeroconf"]["txtKeys"] == [
        const.ZEROCONF_TXT_ID,
        const.ZEROCONF_TXT_API,
        const.ZEROCONF_TXT_PATH,
    ]
    assert manifest["domain"] == const.DOMAIN
    assert manifest["iot_class"] == "local_push"


_ERROR_FOR_ACTION = {
    "reauth": api.EcoPulseAuthError,
    "retry-later": api.EcoPulseNotEnabledError,
    "honour-retry-after": api.EcoPulseRateLimitedError,
}


@pytest.mark.parametrize(
    "error", json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["errors"], ids=str
)
def test_every_contract_error_maps_to_its_client_action(error: dict[str, Any]) -> None:
    response = SimpleNamespace(status=error["status"], headers={"Retry-After": "3"})

    with pytest.raises(_ERROR_FOR_ACTION[error["client"]]):
        api._raise_for_status(response)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("17", 17.0),
        (None, None),
        ("", None),
        ("soon", None),
        ("Thu, 01 Jan 1970 00:00:00 GMT", 0.0),
    ],
)
def test_retry_after_is_parsed(header: str | None, expected: float | None) -> None:
    assert api.parse_retry_after(header) == expected
