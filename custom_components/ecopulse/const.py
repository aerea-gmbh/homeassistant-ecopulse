"""Constants of the EcoPulse integration.

Every number here comes from the shared contract ``ha-api-contract.json`` (ADR-230); the
contract test (tests/test_contract.py) pins them against the fixture so a drift turns red.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ecopulse"
MANUFACTURER: Final = "EcoPulse"
GATEWAY_MODEL: Final = "EdgeGateway"

API_VERSION: Final = 1
API_BASE_PATH: Final = "/api/ha/v1"
DEFAULT_PORT: Final = 8080
TOKEN_PREFIX: Final = "ecp_ha_"
AUTH_REALM: Final = "ecopulse-ha"

# Contract "tls" (schema 4, #610 S8-TLS, ADR-232): the gateway serves the same API over HTTPS on
# its own port with a self-signed certificate; the integration pins the certificate's SHA-256.
DEFAULT_TLS_PORT: Final = 8443
CONF_TLS_FINGERPRINT: Final = "tls_fingerprint"
CONF_USE_TLS: Final = "use_tls"
CONF_TLS_PORT: Final = "tls_port"
CONF_FINGERPRINT_MATCHES: Final = "fingerprint_matches"
SCOPES_REQUIRING_TLS: Final = frozenset({"inputs", "loads", "control"})
FINGERPRINT_PATTERN: Final = r"^[0-9A-F]{2}(:[0-9A-F]{2}){31}$"
ERROR_TLS_REQUIRED: Final = "tls_required"
ERROR_INVALID_REQUEST: Final = "invalid_request"

# Contract "inputs" (schema 5, #624, ADR-232 S5): Home Assistant's values for the gateway's input
# slots. The options map each slot's unique id (the signal Guid) to a Home Assistant entity id.
CONF_INPUTS: Final = "inputs"
INPUTS_PATH: Final = "inputs"
INPUT_DATA_TYPE_NUMBER: Final = "number"
INPUT_DATA_TYPE_BOOLEAN: Final = "boolean"
INPUTS_MAX_PER_REQUEST: Final = 100
INPUTS_REPOST_SECONDS: Final = 10
# Contract "client.routeErrors": a 403 on POST /inputs disables that route only, never the entry.
CLIENT_ACTION_DISABLE_ROUTE: Final = "disable-route"
CLIENT_ACTION_DROP_REQUEST: Final = "drop-request"
# What Home Assistant entities may feed a slot of each data type (the options flow's filter).
INPUT_DOMAINS: Final = {
    INPUT_DATA_TYPE_NUMBER: ["sensor", "number", "input_number"],
    INPUT_DATA_TYPE_BOOLEAN: ["binary_sensor", "input_boolean", "switch"],
}
ISSUE_INPUTS_REFUSED: Final = "inputs_refused"
ISSUE_INPUTS_NEED_TLS: Final = "inputs_need_tls"
ISSUE_INPUT_UNIT: Final = "input_unit"
ISSUE_INPUT_LOOP: Final = "input_loop"

# Contract "loads" (schema 6, #625, ADR-232 S6): EcoPulse switches Home Assistant loads. The gateway
# sends a "command" event on the pinned TLS stream; the options map each load's unique id (the
# device Guid) to a Home Assistant entity, which is switched and the command acknowledged.
CONF_LOADS: Final = "loads"
EVENT_COMMAND: Final = "command"
COMMAND_ACK_PATH: Final = "/commands/{commandId}/ack"
COMMAND_ID_PATTERN: Final = "^[0-9a-f]{32}$"
LOAD_ACTION_ON: Final = "on"
LOAD_ACTION_OFF: Final = "off"
LOAD_ENTITY_DOMAINS: Final = ("switch", "input_boolean", "light", "fan")
ACK_MAX_ERROR_LENGTH: Final = 256
ACK_BODY_MAX_BYTES: Final = 1024
ACK_TIMEOUT_SECONDS: Final = 10
# Review r1 MED-1: the gateway sends commands only to a stream that asks for them this way.
COMMAND_OPT_IN_PARAMETER: Final = "commands"
COMMAND_OPT_IN_VALUE: Final = "1"
ERROR_UNKNOWN_COMMAND: Final = "unknown_command"
ISSUE_LOADS_REFUSED: Final = "loads_refused"
ISSUE_LOADS_NEED_TLS: Final = "loads_need_tls"
ISSUE_LOAD_LOOP: Final = "load_loop"

# Contract "control" (schema 7, #627, ADR-232 S8): Home Assistant changes an assistant's mode,
# enabled state and battery-support thresholds. Entities only with the "control" scope over the
# pinned TLS connection; their state is the stream value named by the control's stateValueId.
SCOPE_CONTROL: Final = "control"
CONTROL_PATH: Final = "control"
CONTROL_PLATFORMS: Final = ("select", "switch", "number")
CONTROL_REFUSAL_CODES: Final = frozenset({"out_of_bounds", "unknown_target", "not_allowed"})
ERROR_UNAVAILABLE: Final = "unavailable"
ISSUE_CONTROL_REFUSED: Final = "control_refused"

ZEROCONF_SERVICE_TYPE: Final = "_ecopulse._tcp.local."
ZEROCONF_TXT_ID: Final = "id"
ZEROCONF_TXT_API: Final = "api"
ZEROCONF_TXT_PATH: Final = "path"

# Client behaviour, contract "client" + "stream".
BACKOFF_INITIAL_SECONDS: Final = 1
BACKOFF_MAX_SECONDS: Final = 60
RETRY_LATER_SECONDS: Final = 300
STREAM_DEAD_AFTER_SECONDS: Final = 45
REQUEST_TIMEOUT_SECONDS: Final = 10

ENTITY_CATEGORY_DIAGNOSTIC: Final = "diagnostic"

EVENT_SNAPSHOT: Final = "snapshot"
EVENT_VALUES: Final = "values"
EVENT_AVAILABILITY: Final = "availability"
EVENT_CATALOG: Final = "catalog"

# The gateway's own entities, contract "gatewayEntities" (schema 2, #623).
GATEWAY_ENTITY_UNIQUE_ID_TEMPLATE: Final = "{gatewayId}:{key}"
SOLAR_EXCESS_POWER_KEY: Final = "solar_excess_power"

# The gateway's own problem sensors (schema 3, #626, ADR-231 §10). Named by this integration's
# translations (en, de) rather than the catalog's English name.
CLOUD_CONNECTED_KEY: Final = "cloud_connected"
DRIVER_PROBLEM_KEY: Final = "driver_problem"
DIRECTIVE_FALLBACK_KEY: Final = "directive_fallback"
PROBLEM_SENSOR_KEYS: Final = frozenset(
    {CLOUD_CONNECTED_KEY, DRIVER_PROBLEM_KEY, DIRECTIVE_FALLBACK_KEY}
)
# The forecast and price sensors (schema 3, #626 S7.2, ADR-231): fed by the cloud's
# "forecast.summary" push, listed by the gateway while the push carries their section.
FORECAST_ENTITY_KEYS: Final = frozenset(
    {
        "pv_forecast_today",
        "pv_forecast_remaining_today",
        "pv_forecast_tomorrow",
        "pv_forecast_power_now",
        "pv_forecast_power_next_hour",
        "consumption_forecast_today",
        "consumption_forecast_remaining_today",
        "consumption_forecast_tomorrow",
        "price_now",
    }
)
# The optimizers' plan sensors (schema 3, #626 S7.3, ADR-231): fed by the cloud's "battery.plan"
# and "heatpump.plan" pushes, listed by the gateway while it holds a plan of its optimizer.
PLAN_ENTITY_KEYS: Final = frozenset(
    {"battery_plan_state", "battery_plan_soc", "heatpump_plan_state"}
)
# The alarm sensors (schema 3, #626 S7.4, ADR-231 §9): fed by the cloud's "alerts.state" push.
# "alarm" is named by the translations; a rule's sensor ("alarm_{ruleId}", contract family) is
# named after the rule, and takes only its attribute names from the translations.
ALARM_KEY: Final = "alarm"
ALARM_ENTITY_KEYS: Final = frozenset({ALARM_KEY})
ALARM_RULE_KEY_PREFIX: Final = "alarm_"
ALARM_RULE_TRANSLATION_KEY: Final = "alarm_rule"
TRANSLATED_GATEWAY_ENTITY_KEYS: Final = (
    PROBLEM_SENSOR_KEYS | FORECAST_ENTITY_KEYS | PLAN_ENTITY_KEYS | ALARM_ENTITY_KEYS
)

# Contract "stream.attributes" (schema 3, #626): a value's attributes; these keys stay out of
# the recorder (the arrays, and synced_at, which moves with every cloud resync).
UNRECORDED_ATTRIBUTE_KEYS: Final = frozenset(
    {"synced_at", "drivers", "assistants", "hourly", "prices", "plan", "alarms"}
)

# "Surplus available" is the plugin's, not the contract's: its settings are entry options
# (PO 09.10.), derived from solar_excess_power. Its unique id extends the source's, so it can
# never collide with a "{gatewayId}:{key}" the edge adds later.
SURPLUS_AVAILABLE_SUFFIX: Final = "surplus_available"
CONF_SURPLUS_THRESHOLD: Final = "surplus_threshold_w"
CONF_SURPLUS_HYSTERESIS: Final = "surplus_hysteresis_w"
CONF_SURPLUS_ON_DELAY: Final = "surplus_on_delay_s"
CONF_SURPLUS_OFF_DELAY: Final = "surplus_off_delay_s"
DEFAULT_SURPLUS_THRESHOLD_W: Final = 500
DEFAULT_SURPLUS_HYSTERESIS_W: Final = 100
DEFAULT_SURPLUS_ON_DELAY_SECONDS: Final = 120
DEFAULT_SURPLUS_OFF_DELAY_SECONDS: Final = 120
MAX_SURPLUS_POWER_W: Final = 100_000
MAX_SURPLUS_DELAY_SECONDS: Final = 3600
