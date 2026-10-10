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
TRANSLATED_GATEWAY_ENTITY_KEYS: Final = (
    PROBLEM_SENSOR_KEYS | FORECAST_ENTITY_KEYS | PLAN_ENTITY_KEYS
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
