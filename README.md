# Centrometal Local MQTT Protocol

Run a Centrometal PelTec II Lambda from Home Assistant **without the Centrometal cloud**. A small local server takes the place of the portal for the boiler's built-in WiFi module, so readings and on/off keep working with no internet.

Community project, not affiliated with Centrometal. No firmware change and no wiring: the boiler keeps its own controller and all its safety functions.

**Tested on one boiler:** PelTec II Lambda, firmware v3.03dL, scheme 43 (buffer + K1 DHW). Other models and schemes need their own check.

## Install

The app and the integration install separately, from these two buttons. The full steps are under [Installation](#installation), and the module's traffic still has to be redirected in your router.

[![Add the app repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FInternetrevizor%2Fcentrometal-local-mqtt)
[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Internetrevizor&repository=centrometal-local-mqtt&category=integration)

## How it works

The module connects to `portal.centrometal.hr` on TCP 1883 using plain MQTT. You redirect that connection, for this one device, to Home Assistant. The server here then answers it the way the portal does: it acknowledges the handshake and the heartbeat, asks for values, and passes on/off.

| Sent to the module | When |
|---|---|
| `_sync_ACK`, `PING_ACK`, `E*`/`W*` acknowledgements | as replies |
| `REFRESH` (read request) | every `read_interval_seconds`, default 60 s |
| `RSTAT` (counters) | every 60 s at most |
| `CMD` 0 or 1 | only when you use the switch |
| `PRD` (read a setting) | after connecting and every hour |
| `PWR` (write a setting) | only when you change one |

With the burner on, the module also sends the fast values by itself when they change (boiler, flue gas, return, lambda, stage). With the burner off it sends almost nothing unless asked, so the read interval sets how fresh the readings are. The cloud integration asks every 240 s, which is why it shows many values at that pace.

Nothing else can be sent. Settings can be written only for the known slots, within the limits of the controller.

The app watches its own session. A module that connects but never completes the handshake (30 s), or that stays silent for 3 minutes while its TCP connection looks alive, is disconnected, and it reconnects within seconds. Text the boiler's controller leaks into the stream (`AT+CWLIF`) is skipped, counted and never answered. `Gateway State` shows the names of those commands in `at_commands` and `last_malformed`, never their arguments or the skipped bytes. After leaking text the module may send older messages again, with the same `clMsgId`, up to a few minutes later. Their values would overwrite newer ones, so they are counted in `stale_messages` on `Gateway State` and ignored.

## Commands

A command is finished only when the boiler says so. The switch waits up to 15 s for the module to report the requested value; the app keeps trying for 90 s, across reconnects, and the result is one of:

| Result | Meaning |
|---|---|
| `confirmed` | the module reported the requested value |
| `unchanged` | a setting the controller reported holding that value in the last 2 minutes: nothing is written |
| `expired` | no confirmation in 90 s. The boiler may or may not have received it |
| `replaced` | a newer request for the same thing took its place |
| `rate_limited` | refused: more than 6 writes to one setting in 10 minutes |

Each result is fired as the event `centrometal_local_command_result` and shown in the `commands` attribute of `Gateway State`, so an automation can react to it. CMD is repeated every 5 s until confirmed. PWR is written at most 3 times per request.

## What you get

Every parameter the boiler sends becomes a sensor, with English names. Every entity_id starts with the device name, as Home Assistant builds it: `PelTec II Lambda` gives `sensor.peltec_ii_lambda_...`; rename the device and Home Assistant offers to rename the entities with it. An install made by 1.6.10 or older is aligned once on update: only entities of this integration whose id does not start with the device name are renamed (the settings, the mode and DHW priority, the DHW circuit, the event history and the system connection), and the log lists each one. Check automations and dashboards that use them. Translate names in Home Assistant. A parameter not seen before gets a diagnostic sensor under its raw name.

- `switch.peltec_ii_lambda_boiler_switch`: boiler on/off (tested).
- On scheme 43, K1 is the DHW circuit and pump P1. Signals that repeat another one have no entity of their own, and values with an unknown meaning start disabled.
- `switch.peltec_ii_lambda_dhw_circuit` and seven `number.peltec_ii_lambda_...` settings: written with `PWR`, in steps of 1, as on the boiler panel. A value typed or clicked into a box is written once it has stayed the same for 3 s, so clicking the arrows sends one write, not one per click. `select.peltec_ii_lambda_dhw_heating_mode_setting` (DHW + heating, DHW only, Heating only, Auto; slot 4) and `switch.peltec_ii_lambda_dhw_priority_setting` (slot 898) are written the same way. The format follows the portal's own request; **writing has not been tested on a real boiler yet.**
- `binary_sensor.peltec_ii_lambda_system_connection`: on while data keeps arriving.
- `binary_sensor.peltec_ii_lambda_alarm`: on while the boiler reports an error or a warning; the codes are in its attributes, and past events survive a restart in Event History. Alarms are read only: nothing here can end one. The alarm format comes from another Centrometal model and has not been seen yet on a PelTec II.
- `sensor.peltec_ii_lambda_sessions_closed`, `_malformed_frames`, `_commands_expired`: counters that survive restarts and stay in the long-term history. `Gateway State` also shows `close_reasons`, and the integration offers Download diagnostics, without the token.


## Choose a mode

| `connection_mode` | Portal and app | Readings in Home Assistant | On/off and settings from this project |
|---|---|---|---|
| `local` (default) | Stop seeing the boiler | Always, also with no internet | Yes |
| `cloud_proxy` (bypass) | Keep working | A copy of what passes through | No, use the portal or the cloud integration |

**Before you choose:**
- In every mode the module's traffic goes through Home Assistant. If Home Assistant is down, the module reaches neither this server nor the portal; only the boiler's own panel remains. If you want the cloud to be independent of Home Assistant, do not install this at all.
- `cloud_proxy` passes the bytes unchanged and sends nothing of its own. With the burner off, the boiler sends temperatures only when asked, so readings depend on something asking through the portal, such as the cloud integration.
- Next to the cloud integration, the sensors of this project get their own names with a `_2` suffix, because the entity_ids are taken.

Tested on one real boiler: `local` and `cloud_proxy`.

## Requirements

- Home Assistant OS or Supervised on a 64-bit system (for the app): aarch64 or amd64.
- TCP 1883 free on the Home Assistant host. Port 1883 is the only one the app publishes.
- A fixed IP for the boiler's module (DHCP reservation).
- A firewall or gateway able to redirect traffic for one device. **This redirection is required: without it the module keeps talking to the cloud and this project receives nothing.**

## Installation

1. **Add the repository.** Use the button, or go to Settings → Apps → App store → ⋮ → Repositories and add `https://github.com/Internetrevizor/centrometal-local-mqtt`.

   [![Add the app repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FInternetrevizor%2Fcentrometal-local-mqtt)

2. **Install the app** Centrometal Local MQTT Protocol. Set `module_ip` to the module's IP. Turn on Start on boot and Watchdog, then start it.
3. **Install the integration with HACS.** Use the button, or go to HACS → ⋮ → Custom repositories and add the same URL with the type Integration.

   [![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Internetrevizor&repository=centrometal-local-mqtt&category=integration)

   Then download Centrometal Local MQTT Protocol and restart Home Assistant. HACS installs integrations only, which is why the app comes from the app store.
   The app announces itself: the boiler appears under Discovered in Settings → Devices & services. Confirm it, nothing has to be typed.

   [![Open Devices and services](https://my.home-assistant.io/badges/integrations.svg)](https://my.home-assistant.io/redirect/integrations/)

4. **Redirect the module's traffic to Home Assistant, last.** Allow the module's network to reach Home Assistant on TCP 1883, then add a destination NAT rule:

   | Field | Value |
   |---|---|
   | Interface | the module's network, **not WAN** |
   | Protocol | TCP |
   | Source | the module's IP |
   | Destination | the portal's IP (`nslookup portal.centrometal.hr`), port 1883 |
   | Translated to | Home Assistant's IP, port 1883 |

   On a UniFi gateway: Settings → Policy Table → Create New Policy → NAT → Dest. NAT.

   The rule catches only new connections, so the module moves over the next time it reconnects. Restarting its WiFi connection speeds this up.
5. **Check.** `Gateway state` shows `local`, and temperatures update within seconds.

**Working without internet:** the module looks up the portal's name each time it reconnects. If your DNS server cannot answer with the internet down, add a local DNS record for `portal.centrometal.hr` pointing to the same portal IP.

## Updates

The app updates from the app store and the integration from HACS, separately. Update both to the same version. A pair that no longer understands each other raises a repair notice in Home Assistant.

## Options

| Option | Meaning |
|---|---|
| `module_ip` | Only this IP is accepted |
| `connection_mode` | `local` or `cloud_proxy`, see above |
| `upstream_host` | The portal's name, used only by `cloud_proxy` |
| `read_interval_seconds` | Time between read requests, 10 to 240, default 60. One answer is about 1.3 kB. What matters during a burn arrives by itself |
| `debug_journal` | Writes every message to `/share/centrometal_local_mqtt/journal.jsonl`, without MQTT password, token or signature. Skipped bytes are never written. Normally only new parameter names and session events are written |

## Going back

Disable the redirection rule, then restart the app. The module reconnects to the portal by itself.

## Security

The module talks to the server over unencrypted MQTT and does not authenticate the server beyond a signature whose key derivation is already public. That is what makes this project possible, and it also means anyone able to redirect this traffic could do the same. Keep the module on a separate network, and never expose TCP 1883 to the internet. The app runs under its own AppArmor profile, and its internal API needs a token generated at the first start. The password in the module's CONNECT is also its signing key, so a signature alone does not prove who is at the other end: the only gate is `module_ip`. The app remembers a salted fingerprint of the first module it sees, and counts in `identity_changes` any other that connects.

## Development

```
python -B -m unittest discover -s tests                  # the app and the integration's own logic, standard library only
pip install pytest-homeassistant-custom-component && python -m pytest   # the integration inside a real Home Assistant, Linux or macOS
```

`protocol.py`, `session.py`, `commands.py` and `store.py` touch no socket and take the time from outside, so `tests/test_session.py` runs a day of traffic in a moment. `tests/data/module_messages.jsonl` holds real messages of the boiler: numbers and stages only. `PELTEC_PCAP` points the capture tests to a private capture.

The image is built by GitHub Actions and has to exist before a new `version` reaches `main`, because the store offers it from there. A tag `v*` publishes the GitHub release that HACS reads. `custom_components/centrometal_local/parameters.py` is the only list of parameters and writable slots: the app takes it from there at build time.

## Disclaimer

Provided as is, under the Apache-2.0 license, with no warranty. You are responsible for your heating system; see `NOTICE` for credits.
