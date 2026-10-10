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

Beyond reading, Home Assistant can feed values into EcoPulse and EcoPulse can switch Home Assistant loads (both over
the encrypted connection, see below). Controlling EcoPulse from Home Assistant is planned.

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
*Settings > Devices & services > EcoPulse > Configure > Surplus available*. Changes apply immediately, without a
restart.

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
  as is, with no grace period, so a short reconnect (for example a certificate renewal) shows as a short off. A
  gateway that is not set up for EcoPulse's cloud yet shows it off for good.
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

## Forecasts and electricity price

When EcoPulse has a forecast or a price for your plant, the gateway device also gets these sensors. EcoPulse
computes them in its cloud and sends them to the gateway, which keeps them on its disk and works out the current
values itself, so they keep moving through a cloud outage:

- **PV forecast today / tomorrow / remaining today** (kWh), with the attribute `hourly` (`start`, `wh`) on today and
  tomorrow, and **PV forecast power now / next hour** (W).
- **Consumption forecast today / tomorrow / remaining today** (kWh), `hourly` on today and tomorrow.
- **Electricity price** (for example `EUR/kWh`, the currency of your tariff): the import price of the running slot,
  with `export_now`, `slot_start`, `slot_end` and `prices` (the coming slots with `start`, `import`, `export`). A
  flat tariff shows its one price with no slot and an empty `prices`; a tiered tariff shows no price at all.

The energy forecasts have no state class on purpose: they are no meters, so they do not belong in the Energy
dashboard. A sensor only exists while EcoPulse delivers its part: a plant without a price has no price sensor.

Each of them also carries `cloud_synced` (whether EcoPulse currently delivers this data), `synced_at` (when it
last arrived) and `computed_at` (when EcoPulse computed it). While the cloud is unreachable the values stay
available and `cloud_synced` turns false. Once the delivered forecast runs out (after about a day without the cloud,
"tomorrow" has no data left) the value becomes unknown rather than showing an old day. The lists and `synced_at` are kept out of the
recorder. The names are translated (English, German).

## Battery and heat pump plans

When EcoPulse's optimizer plans your battery or your heat pump, the gateway that runs the optimizer also gets:

- **Battery plan**: what the battery is planned to do in the running slot (`zero_export` self-consumption,
  `zero_export_with_no_excess_to_grid`, `charge_from_grid`, `discharge_to_grid`), and **Battery plan state of
  charge** (%), the state of charge planned for the end of the running slot.
- **Heat pump plan**: `blocked`, `normal`, `recommended` or `forced` for the running slot.

The two plan sensors carry `slot_start`, `slot_end`, `next_state`, `next_change_at` and `plan`: the coming runs of
one state, each with its `start`, `state` and (battery) the `soc` planned at its end. Like the forecasts they carry
`cloud_synced`, `synced_at` and `computed_at`, keep moving through a cloud outage and become unknown once the plan
runs out. A gateway shows one plan per kind, that of its enabled optimizer with the lowest id; the states and names
are translated (English, German).

## Values from Home Assistant into EcoPulse

Home Assistant can send values EcoPulse has no device for, for example an outdoor temperature from your weather
station or a meter Home Assistant reads. EcoPulse stores and uses them like its own measurements: in the history,
the live view and as input of its assistants.

1. In the EcoPulse web app, add a **Home Assistant** driver to the gateway and the devices to feed on it (each
   numeric or on/off value of such a device becomes an *input*), and turn on **Values from Home Assistant** on the
   gateway's Home Assistant card.
2. Reload the EcoPulse entry in Home Assistant, then *Configure > Values from Home Assistant*: pick the entity that
   feeds each input (sensors, numbers and input numbers for a number; binary sensors, input booleans and switches for
   on/off). EcoPulse's own entities are not offered.

The integration sends every mapped value on each change and every 10 seconds (EcoPulse stamps its own receive time,
so a value that is not re-sent goes stale). Values are converted into the input's unit (°F into °C, kW into W);
an entity whose unit cannot be converted is not sent and a **repair issue** names it. `unavailable` and `unknown`
are sent as "no value". An input without a value for a minute counts as missing in EcoPulse: it shows a driver
problem there, and its assistants stop using the value; there is no substitute value.

Values are sent **only over the encrypted connection**. An entry without it sends nothing and shows a repair issue.
When the gateway refuses the values (the toggle in the web app was turned off), the integration stops sending them,
shows a repair issue and keeps reading; reload the entry after turning the toggle on again.

## Loads switched by EcoPulse

EcoPulse can switch devices through Home Assistant, for example a heating rod or a pool pump on solar surplus. Its
PV heater assistant switches them like relays: with its priority, minimum on and off times and delays.

1. In the EcoPulse web app, create the device on the gateway's **Home Assistant** driver, add it as a **load** with its
   nominal power on the gateway's Home Assistant card, and turn on **Loads switched by EcoPulse** there. Build the
   heater assistant's levels from the loads.
2. Reload the EcoPulse entry in Home Assistant, then *Configure > Loads switched by EcoPulse*: pick the switch, light,
   fan or input boolean EcoPulse switches for each load. EcoPulse's own entities are not offered.

When EcoPulse switches a load, the integration calls the entity's `turn_on` or `turn_off` and reports the result (and
the entity's state) back to the gateway. A load nobody mapped, a service that fails or takes longer than 8 s, and an
entity Home Assistant does not have or reports unavailable are reported as failed, and EcoPulse's assistant shows why.
Commands for one entity run one after the other; when several wait, only the latest runs, so a slow `turn_on` never
ends after a later `turn_off`. Without a power sensor, EcoPulse counts the load's nominal power as its consumption
while it is on; map a power sensor as a value from Home Assistant to the load's device and bind it to the heater to
measure it instead.

Commands are taken **only over the encrypted connection**; an entry without it never switches anything and shows a
repair issue. There is no timeout on the Home Assistant side: when the gateway or the connection goes away, the
entity keeps its state, and EcoPulse sends its command again after the reconnect. When the gateway refuses the answer
to a command (the toggle in the web app was turned off), the integration stops switching, shows a repair issue and
keeps reading; reload the entry after turning the toggle on again.

## Alarms

Your EcoPulse alarm rules show up on the gateway device as problem sensors, so an automation can react to an alarm
without the EcoPulse app:

- **Alarm**: on while any rule of the plant fires, with `active_count`, `critical_count` and `alarms` (up to 10
  firing rules with `rule`, `severity`, `since`, critical ones first, then the oldest).
- **One sensor per alarm rule**, named after the rule: on while the rule fires, with `rule_type`, `severity`
  (`warning` or `critical`), `since`, `message`, `acknowledged` and `in_maintenance`. An acknowledged alarm, or one
  during a maintenance window, stays on: the condition is still there, the attributes say it is being handled.

Every rule of the plant is shown, also on a second gateway of the same plant; a gateway's own "gateway offline" rule
is left out on that gateway, since it could never show it firing. Adding, deleting or renaming a rule in EcoPulse
adds, removes or renames its sensor within seconds (the integration reloads). While the cloud is unreachable the
alarms keep their last state and `cloud_synced` turns false; a rule never disappears just because the cloud is gone.
The alarm list and `synced_at` are kept out of the recorder.

## Requirements

- Home Assistant **2025.10** or newer.
- An EcoPulse EdgeGateway with firmware that offers the Home Assistant API (`/api/ha/v1`), its **local web
  server enabled**, and Home Assistant reachable on the same network as the gateway (port 8080 by default, and
  8443 for the encrypted connection; 18443 on a NAS).

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
4. **Encrypted connection (on by default).** Home Assistant connects to the gateway's TLS port (8443 unless changed)
   and shows the **SHA-256 fingerprint** of the certificate the gateway presents. Compare it with the fingerprint in
   the gateway's Home Assistant settings in the EcoPulse web app and confirm only if both match exactly. The token is
   sent only after that, and from then on Home Assistant trusts only this certificate. Without the encrypted
   connection the integration can only read values; anything it writes to EcoPulse later needs it.

One config entry per gateway; a plant with several gateways is added once per gateway.

**Rotating the token**: create a new token in the EcoPulse web app. The old one stops working immediately and Home
Assistant asks you to re-authenticate the EcoPulse entry; paste the new token there. An entry without the encrypted
connection is offered to switch to it there.

**A new gateway certificate**: the gateway keeps its certificate across restarts and updates. Should it ever present
another one, Home Assistant sends nothing to it and asks you to re-authenticate; it then shows the new fingerprint to
compare with the EcoPulse web app again. Another fingerprint than the web app's means another device is answering:
do not confirm it.

## Security notes

- The token can only **read** the values of this one gateway, plus, if you turn on *Values from Home Assistant* in
  the EcoPulse web app, send values into the inputs EcoPulse defined, and, with *Loads switched by EcoPulse*, answer
  the gateway's switch commands. The entities EcoPulse switches are only the ones you map, with Home Assistant's own
  services. It cannot change the gateway's configuration and
  does not open its web interface.
- With the encrypted connection the token and the values travel over TLS, and the entry is pinned to the gateway's
  certificate: it never falls back to plain HTTP. Without it the connection is plain HTTP on your LAN, as the
  gateway's web interface, and someone who can sniff your LAN can read the token and with it the values; that is
  why the gateway accepts anything that writes only over the encrypted connection.
- A discovery announcement never moves a configured gateway on its own (anyone on the LAN can announce a gateway's
  id): an entry with the encrypted connection follows a new address only if that address presents the trusted
  certificate; an entry without it keeps its address until you change it.
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
