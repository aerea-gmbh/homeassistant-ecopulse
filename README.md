# EcoPulse for Home Assistant

Custom integration (installable via HACS) that brings every value of an EcoPulse EdgeGateway into Home
Assistant, live and local-first: it talks to the gateway's local Home Assistant API on your LAN. No cloud in the
data path, no MQTT broker, no polling.

## What you get

- **One device per EcoPulse device** (battery, inverter, meter, wallbox, heat pump, smart assistant, ...), each
  linked to a parent device for the gateway itself.
- **Every signal the gateway reads**, as `sensor` or `binary_sensor`, with unit, device class, state class and
  display precision from the EcoPulse catalog. Energy counters work in Home Assistant's Energy dashboard.
- **Live updates** pushed by the gateway (Server-Sent Events, about every 2 seconds), also when the gateway has no
  internet connection.
- **Stable entities**: every entity is identified by the EcoPulse signal id, so renaming a device or a datapoint in
  EcoPulse renames nothing in Home Assistant and keeps the history.
- Cell and BMS internals (cell voltages and temperatures, FET states, alarm flags, ...) are created as
  **diagnostic entities, disabled by default**, so a large battery does not flood your dashboards. Enable the ones
  you want under *Settings > Devices & services > EcoPulse*.
- New or removed signals after a configuration change in EcoPulse appear and disappear automatically.
- A device whose source stops answering turns *unavailable*; everything turns unavailable while the gateway is
  unreachable, and the integration reconnects by itself.

Read-only in this version. Using Home Assistant entities as inputs for EcoPulse and controlling EcoPulse from Home
Assistant are planned.

## Solar excess and "Surplus available"

On a gateway with an EcoPulse PV assistant (PV-excess wallbox or heater), the gateway device gets two more
entities for your own automations:

- **Solar excess power** (`sensor`, W): the PV surplus a load switched by Home Assistant may use. It is the
  surplus left over *after* the EcoPulse assistants, so Home Assistant never starts a load on power the wallbox is
  about to take. **Positive** = surplus available; **negative** = deficit (the house draws from the grid or from
  the battery). *Unknown* while the gateway has no usable grid reading. Updated about every 2 seconds.
- **Surplus available** (`binary_sensor`): turns **on** once the solar excess has been at or above a threshold for
  a whole on-delay, and **off** once it has been below *threshold minus hysteresis* for a whole off-delay. A dip
  restarts the delay, so a passing cloud does not switch anything. A missing value (gateway unreachable, no grid
  reading) counts as *no surplus*: the sensor turns off after the off-delay, it never turns unavailable, so an
  automation waiting for "off" still fires. Its state survives a Home Assistant restart.

Set threshold (default 500 W), hysteresis (default 100 W), on-delay and off-delay (default 120 s each) under
*Settings > Devices & services > EcoPulse > Configure*. Changes apply immediately, without a restart.

Example: run the pool pump on surplus.

```yaml
automation:
  - alias: Pool pump on solar surplus
    triggers:
      - trigger: state
        entity_id: binary_sensor.haus_nord_surplus_available  # named after your gateway
        to: ["on", "off"]
    actions:
      - action: >-
          {{ 'switch.turn_on' if trigger.to_state.state == 'on' else 'switch.turn_off' }}
        target:
          entity_id: switch.pool_pump
```

The excess is measured at the grid, so once the load runs, its own draw is no longer excess. Set the threshold to
the load's power and the hysteresis about as large: a 1.2 kW heater with threshold 1200 W and hysteresis 1200 W
starts when 1.2 kW are spare and keeps running as long as the excess stays at 0 W or above.

## Gateway problem sensors

Every gateway device gets diagnostic binary sensors for what the gateway sees itself. They are computed on the
gateway, so they keep telling the truth while the internet or EcoPulse is down:

- **Cloud connection** (`connectivity`): on while the gateway is connected to EcoPulse. It follows the connection
  as is, with no grace period, so a short reconnect (for example a certificate renewal) shows as a short off.
- **Driver problem** (`problem`): on while one of the gateway's device drivers has failed, or has been trying to
  reach its device for more than 120 seconds. The attribute `drivers` lists those drivers (up to 8) with `id`,
  `state` (`failed` or `connecting`), `since` and `error` (a plain-language reason, or null). Which *device* is
  affected also shows as that device's entities turning unavailable.
- **Optimizer fallback** (`problem`, only on a gateway with an EcoPulse battery or heat pump optimizer): on while
  the optimizer has stopped receiving its plan from EcoPulse and the gateway runs its safe fallback (zero export
  for the battery, normal operation for the heat pump). The attribute `assistants` lists them with `id`, `kind`
  (`battery` or `heatpump`) and `since`.

The list attributes are empty (`[]`) while nothing is wrong, so a template can read them without a check. They are
kept out of the recorder (Home Assistant's history database stores the state, not the lists), and so is every other
list attribute the gateway sends. The names are translated (English, German).

Example: get notified when a driver stays down.

```yaml
automation:
  - alias: EcoPulse driver problem
    triggers:
      - trigger: state
        entity_id: binary_sensor.haus_nord_driver_problem  # named after your gateway
        to: "on"
        for: "00:05:00"
    actions:
      - action: notify.notify
        data:
          message: >-
            {% for d in state_attr('binary_sensor.haus_nord_driver_problem', 'drivers') %}
            {{ d.id }}: {{ d.state }} since {{ d.since }} ({{ d.error or 'no reason given' }})
            {% endfor %}
```

## Requirements

- Home Assistant **2025.10** or newer.
- An EcoPulse EdgeGateway with firmware that offers the Home Assistant API (`/api/ha/v1`), its **local web
  server enabled**, and Home Assistant reachable on the same network as the gateway (port 8080 by default).

## Installation via HACS

1. In HACS, open the menu (three dots) > *Custom repositories*.
2. Add `https://github.com/aerea/homeassistant-ecopulse` with category *Integration*.
3. Install *EcoPulse* and restart Home Assistant.

Manual installation: copy `custom_components/ecopulse` into the `custom_components` folder of your Home Assistant
configuration and restart.

## Setup

1. **Create the integration token** in the EcoPulse web app: open the plant, then the gateway's **Home Assistant**
   settings, enable Home Assistant and create a token. Copy it right away: it is shown **only once**. It starts with
   `ecp_ha_` and is *not* the gateway's web password.
2. Wait until the configuration has reached the gateway (usually a few seconds).
3. In Home Assistant: *Settings > Devices & services*. If the gateway was discovered on your network, click
   *Configure* on it and paste the token. Otherwise *Add integration > EcoPulse* and enter the gateway's IP address
   (or host name), the port (8080 unless changed) and the token.

One config entry per gateway; a plant with several gateways is added once per gateway.

**Rotating the token**: create a new token in the EcoPulse web app. The old one stops working immediately and Home
Assistant asks you to re-authenticate the EcoPulse entry; paste the new token there.

## Security notes

- The token can only **read** the values of this one gateway. It cannot change the gateway's configuration and
  does not open its web interface.
- The connection is plain HTTP on your LAN, as the gateway's web interface. Someone who can sniff your LAN can
  read the token and with it the values.
- Repeated wrong tokens lock the client out for a while (HTTP 429). The setup form refuses anything that does not
  look like an integration token before it contacts the gateway.

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -c ci/constraints-2026.10.0.txt -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`requirements_test_min.txt` (Python 3.13) tests against the minimum supported Home Assistant. The tests run a
fake gateway that serves the examples of the shared API contract (`tests/fixtures/ha-api-contract.json`, a
byte-identical copy of the contract in the EcoPulse repositories, pinned by sha256 in `tests/test_contract.py`).

## License

MIT, see [LICENSE](LICENSE).
