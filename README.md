# Centrometal Local MQTT Protocol

Run a Centrometal PelTec II Lambda from Home Assistant **without the Centrometal cloud**. A small local server takes the place of the portal for the boiler's built-in WiFi module, so readings and on/off keep working with no internet.

Community project, not affiliated with Centrometal. No firmware change and no wiring: the boiler keeps its own controller and all its safety functions.

**Tested on one boiler:** PelTec II Lambda, firmware v3.03dL, scheme 43 (buffer + K1 DHW). Other models and schemes need their own check.

## How it works

The module connects to `portal.centrometal.hr` on TCP 1883 using plain MQTT. You redirect that connection, for this one device, to Home Assistant. The server here then answers it the way the portal does: it acknowledges the handshake and the heartbeat, asks for values, and passes on/off.

| Sent to the module | When |
|---|---|
| `_sync_ACK`, `PING_ACK`, `E*`/`W*` acknowledgements | as replies |
| `REFRESH`, `RSTAT` (read requests) | every 240 s |
| `CMD` 0 or 1 | only when you use the switch |
| `PRD` (read a setting) | after connecting and every hour |
| `PWR` (write a setting) | only when you change one |

Nothing else can be sent. Settings can be written only for the seven known slots, within the limits of the controller.

## What you get

Every parameter the boiler sends becomes a sensor, with English names and the same entity_ids as the cloud integration [hass-centrometal-boiler](https://github.com/Internetrevizor/hass-centrometal-boiler), so existing automations and dashboards keep working after you remove it. Translate names in Home Assistant. A parameter not seen before gets a diagnostic sensor under its raw name.

- `switch.peltec_ii_lambda_boiler_switch`: boiler on/off (tested).
- On scheme 43, K1 is the DHW circuit and pump P1. Signals that repeat another one have no entity of their own, and values with an unknown meaning start disabled.
- `switch.k1_heating_circuit_off_on` and seven `number` settings: written with `PWR`. The format follows the portal's own request; **writing has not been tested on a real boiler yet.**
- `binary_sensor.centrometal_boiler_system_connection`: on while data keeps arriving.

To keep the same entity_ids in `local` mode, remove the cloud integration before adding this one.

## Choose a mode

| `connection_mode` | Portal and app | Readings in Home Assistant | On/off and settings from this project |
|---|---|---|---|
| `local` (default) | Stop seeing the boiler | Always, also with no internet | Yes |
| `cloud_proxy` (bypass) | Keep working | A copy of what passes through | No, use the portal or the cloud integration |
| `cloud_proxy_fallback` | Keep working while the portal answers | Always | Only while the portal is down; it returns by itself when the portal is back |

**Before you choose:**
- In every mode the module's traffic goes through Home Assistant. If Home Assistant is down, the module reaches neither this server nor the portal; only the boiler's own panel remains. If you want the cloud to be independent of Home Assistant, do not install this at all.
- `cloud_proxy` passes the bytes unchanged and sends nothing of its own. With the burner off, the boiler sends temperatures only when asked, so readings depend on something asking through the portal, such as the cloud integration.
- Next to the cloud integration, the sensors of this project get their own names with a `_2` suffix, because the entity_ids are taken.

Tested on one real boiler: `local` and `cloud_proxy`. `cloud_proxy_fallback` has only been tested in software.

## Requirements

- Home Assistant OS or Supervised (for the app).
- TCP 1883 free on the Home Assistant host.
- A fixed IP for the boiler's module (DHCP reservation).
- A firewall or gateway able to redirect traffic for one device. **This redirection is required: without it the module keeps talking to the cloud and this project receives nothing.**

## Installation

1. **Add the project.**
   - App: Settings → Apps → App store → ⋮ → Repositories, add `https://github.com/Internetrevizor/centrometal-local-mqtt`.
   - Integration: HACS → ⋮ → Custom repositories, same URL, type Integration. Without HACS, copy `custom_components/centrometal_local/` to your configuration folder.
2. **Install the app** Centrometal Local MQTT Protocol. Set `module_ip` to the module's IP. Turn on Start on boot and Watchdog, then start it.
3. **Restart Home Assistant** and add the integration Centrometal Local MQTT Protocol, with host `127.0.0.1` and port `8765`.
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
5. **Check.** `Gateway state` shows `local`, and temperatures update within a few minutes.

**Working without internet:** the module looks up the portal's name each time it reconnects. If your DNS server cannot answer with the internet down, add a local DNS record for `portal.centrometal.hr` pointing to the same portal IP.

## Options

| Option | Meaning |
|---|---|
| `module_ip` | Only this IP is accepted |
| `connection_mode` | `local`, `cloud_proxy` or `cloud_proxy_fallback`, see above |
| `upstream_host` | The portal's name, used only by the two proxy modes |
| `poll_interval_seconds` | Time between read requests, default 240 |
| `debug_journal` | Writes every message to `/share/centrometal_local_mqtt/journal.jsonl`, without password, token or signature. Normally only new parameter names and session events are written |

## Going back

Disable the redirection rule, then restart the app. The module reconnects to the portal by itself.

## Security

The module talks to the server over unencrypted MQTT and does not authenticate the server beyond a signature whose key derivation is already public. That is what makes this project possible, and it also means anyone able to redirect this traffic could do the same. Keep the module on a separate network, and never expose TCP 1883 or 8765 to the internet.

## Disclaimer

Provided as is, under the Apache-2.0 license, with no warranty. You are responsible for your heating system; see `NOTICE` for credits.
