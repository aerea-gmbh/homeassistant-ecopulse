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

CONTRACT_SHA256 = "8f475d966df64cb465f044606471136ad7c79355d80483a7debfe1c1f8b83766"
MANIFEST_PATH = Path(__file__).parents[1] / "custom_components" / "ecopulse" / "manifest.json"


def test_fixture_is_the_pinned_contract() -> None:
    assert hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest() == CONTRACT_SHA256


def test_schema_and_api_version_are_the_ones_this_plugin_speaks(contract: dict[str, Any]) -> None:
    assert contract["schemaVersion"] == 1
    assert contract["apiVersion"] == const.API_VERSION
    assert contract["basePath"] == const.API_BASE_PATH


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
