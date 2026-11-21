# Changelog

The app and the integration always have the same version: update both.

## 1.7.7 — the other boilers get their own names

- **CM Pelet-set, BioTec, BioTec-Plus:** the counters are named after the boiler's own family, with its units ("Operation Pellets" in minutes, "Startup Wood" as a count). They carried the PelTec II's names, which mean something else on these boilers. A BioTec counter of unknown meaning keeps its raw name and stays disabled. On the BioTec and BioTec-Plus `B_Tpov1` is the mixer temperature and `B_Tptv1` the domestic hot water; on the CM Pelet-set `B_fan` is the heater fan on/off.
- **Every boiler other than the PelTec II:** pump P1, the boiler command state, the K1/K2 pump and the DHW tank temperature are shown; on the PelTec II they repeat another sensor and stay hidden. P2 is "P2 Pump" (the DHW recirculation pump is the PelTec II's scheme 43), and the configuration shows its number without the scheme 43 name.
- **Heating circuits C1-C4 and K2-K4** are named ("Circuit 1 Room Measured Temperature", "Circuit 1 Day Night Mode"), as are the wood and pellet boiler temperatures, the firebox temperature (up to 1500 °C), the pump demands, the air flow and the WiFi box version.
- Entity IDs that exist keep working: an existing sensor changes only its name, and a counter its unit, which Home Assistant may report under Statistics. The PelTec II is unchanged.

## 1.7.6 — up to date

- The app runs on Python 3.14 (was 3.12), on the official image pinned by digest.
- The code uses the current forms of the standard library (`datetime.UTC`, `TimeoutError`, `collections.abc`, `asyncio.timeout`). Nothing changes in what the app or the integration does.
- Ruff checks the code in CI. The unit tests run on the Python of the app image.
- This changelog: shown on the app's page in Home Assistant, and the release notes on GitHub come from it.

## 1.7.5 — the integration waits for the app

- The integration sets up only once the app has answered, because the boiler it serves decides the device name and whether the entities that write exist. Until then Home Assistant retries the setup on its own. Before, a boiler other than the PelTec II got switches, numbers and a device named "PelTec II Lambda" when the app did not answer at setup.

## 1.7.4 — any Centrometal boiler is read

- The app serves the WiFi module of any Centrometal boiler (`peltec2`, `peltec`, `compact`, `cmpelet`, `biotec`, `biopl`), built in or CM WiFi-Box: the protocol is the same, only the product code in the topics differs. It served only the PelTec II before.
- Any boiler other than the PelTec II is read only: no on/off, no setting written, no settings table read. The integration creates no switch, number or select for it.
- The device is named after the boiler ("CM Pelet-set"); the PelTec II keeps "PelTec II Lambda" and its entity_ids.
- README: a table of supported boilers.

## 1.7.3 — last_reported follows the boiler

- A sensor is written only when the boiler reports its parameter again, or when what it shows changes, so `last_reported` is the moment of the boiler's last report, not of the app's heartbeat. 10 heartbeats on 58 sensors wrote 355 states before, 90 now.
- What describes the boiler (software version, product, rated power, scheme) never becomes unavailable.
- The same information message (`I...`) within 10 minutes is one event in the history.
- A module that starts its numbering over just below the last number loses at most 64 messages, not up to 200.

## 1.7.2 — settings writes tested

- No change in behaviour: writing settings is tested on a PelTec II Lambda 36, firmware v3.03dL, scheme 43.

## 1.7.1 — no deprecated device registry call

- The one-time entity_id migration of 1.7.0 uses the device registry call that Home Assistant 2026.8 did not deprecate.

## 1.7.0 — entity_ids follow the device name

- No entity_id is forced: Home Assistant builds them from the device name and the entity name, so renaming the device renames them. An older install is aligned once, and each rename is logged.

## 1.6.10 — the stop buffer tank difference is a setting

- Slot 71 is a setting again (3 to 30), as on the panel. The DHW / heating mode and the DHW priority are controls.

## 1.6.9 — settings stay available

- A setting counts as current for 75 minutes (read once an hour); everything else keeps the 8-minute rule.

## 1.6.8 — one write per change

- A setting is written once its box has stayed the same for 3 s, not at every click. The DHW / heating mode (slot 4) and the DHW priority (slot 898) can be set.

## 1.6.7 — steps of 1

- Every setting moves in steps of 1, as on the panel. The DHW / heating mode shows its name.

## 1.6.6 — settings as the panel has them

- Setting limits and steps follow the boiler panel.

## 1.6.5 — repeated messages are ignored

- A message the module sends again after leaking AT text no longer overwrites newer values; it is counted in `stale_messages`.

## 1.6.2 – 1.6.4

- The names of the AT commands the controller leaks are shown, never their arguments.
- A version with no release yet is published from `main` after the tests.

## 1.6.1 and earlier

- 1.6.1: the integration category in the manifest.
- First releases: the local server and the integration; `local` and `cloud_proxy` modes; confirmed commands (`confirmed`, `unchanged`, `expired`, `replaced`, `rate_limited`); watched sessions; AT text skipped; values, counters and alarms kept across restarts; discovery through the Supervisor; AppArmor profile, API token, prebuilt images.
