"""Every parameter the boiler sends: name, how it is shown, and its entity_id. The PelTec II Lambda is the tested boiler.

Names and values are in English and match the cloud integration Internetrevizor/hass-centrometal-boiler (Apache-2.0),
so automations and dashboards keep working. Translate them in Home Assistant, not here.
A parameter that is not listed still gets a diagnostic sensor under its raw name, so nothing the boiler sends is lost.
"""
from datetime import datetime, UTC
import re

OFF_ON = {0: "Off", 1: "On"}
STAGES = {
    "OFF": "Switched off", "S0": "Initial fan blowing, waiting for grate initial position", "S1": "Not in use",
    "S2": "Initial pellet filling", "S3": "Waiting for flame", "S3-1": "Extended ignition / flame verification",
    "S3-2": "Additional ignition fuel charge", "S4": "Electric heater working after flame appears", "S5": "Flame developing",
    "SP1": "Stabilising 1", "SP2": "Stabilising 2", "SP3": "Stabilising 3", "SP4": "Stabilising 4", "SP5": "Stabilising 5",
    "S6": "Additional flame development", "D0": "Power D0 (min)", "D1": "Power D1", "D2": "Power D2", "D3": "Power D3",
    "D4": "Power D4", "D5": "Power D5", "D6": "Power D6 (max)", "S7": "Shutdown stage",
    "S7-1": "Shutdown 1, waiting for flame to disappear", "S7-2": "Shutdown 2, fan at maximum speed",
    "S7-3": "Standby, waiting for start request", "PF0": "Power failure, electric heater on, waiting for flame",
    "PF1": "Power failure, flame appeared, electric heater off", "PF2": "Power failure, flame developing",
    "PF3": "Power failure, waiting for flame to disappear", "PF4": "Power failure, final fan blowing", "C0": "Grate cleaning",
}
BURNER = {0: "Stopped", 1: "Running", 2: "Stopping", 3: "Cleaning"}
FUEL = {0: "Empty", 1: "Reserve", 2: "Full"}
MARK = {0: "None", 1: "R", 2: "B", 3: "T", 4: "G", 5: "F"}
MARK_DETAILS = {0: ('No special shutdown activity', 'None', False, 'PelTec II Lambda controller manual'), 1: ('Shutdown after loss of flame during operation', 'Temporary shutdown reason', True, 'PelTec II Lambda controller manual'), 2: ('Shutdown after excessive pellet feed tube bimetal temperature', 'Temporary shutdown reason', True, 'PelTec II Lambda controller manual'), 3: ('Shutdown so the turbulator / flue passage cleaner can operate', 'Temporary shutdown reason', True, 'PelTec II Lambda controller manual'), 4: ('Shutdown because burner grate cleaning is required', 'Temporary shutdown reason', True, 'PelTec II Lambda controller manual'), 5: ('Shutdown associated with pellet tank filling or refilling', 'Temporary shutdown reason', True, 'Related official Centrometal controller manual')}
SUPERVISION = {0: "Off", 1: "Supervision", 2: "Supervision + control"}
# Controller manual 5.1, in the order of the panel; the owner went through all four on 07.11.2026 and saw 0, 1, 2, 3.
DHW_HEATING_MODES = {0: "DHW + heating", 1: "DHW only", 2: "Heating only", 3: "Auto"}
SCHEMES = {42: "43. BUF + K1 DHW"}
SCHEME_ATTRIBUTES = {42: {"Buffer tank": "CAS", "DHW tank": "pump", "DHW recirculation": True, "P1": "DHW Pump (K1)",
                          "P2": "DHW Recirculation Pump (K1)", "P3": "P3 Pump", "P4": "P4 Pump"}}
SENSOR_PREFIX = "sensor.peltec_ii_lambda_"
ROOM_PREFIX = "sensor.technical_room_centrometal_peltec_ii_peltec_ii_lambda_"

# key: (name, kind, extra, diagnostic, object id). Object ids of the entities that existed in the cloud integration are kept.
PARAMETERS = {
    "B_Tak1_1": ("Puffer Upper", "temperature", None, False, "puffer_upper"),
    "B_Tak2_1": ("Puffer Lower", "temperature", None, False, "puffer_lower"),
    "B_Tk1": ("Core Temperature", "temperature", None, False, "core_temperature"),
    "B_Tdpl1": ("Flue Gas Temperature", "temperature", None, False, "flue_gas_temperature"),
    "B_Tpov1": ("Return Flow Temperature", "temperature", None, False, "return_flow_temperature"),
    "B_Tva1": ("Outside Temperature", "temperature", None, False, "outside_temperature"),
    "B_Ths1": ("Hydraulic Crossover Temperature", "temperature", None, True, "hydraulic_crossover_temperature"),
    "K1B_Tpol1": ("DHW Temperature", "temperature", None, False, "circuit_1k_flow_measured_temperature"),
    "K1B_Tpol": ("DHW Target Temperature", "setpoint", None, False, "circuit_1k_flow_target_temperature"),
    "B_STATE": ("Boiler State", "stage", STAGES, False, "boiler_state"),
    "B_start": ("Start / Stop Transition", "map", BURNER, False, "b_start"),
    "B_fireS": ("Flame State", "map", OFF_ON, False, "firing_state"),
    "B_fan01": ("Fan", "map", OFF_ON, False, "fan_speed"),
    "B_doz": ("Pellet Feed", "map", OFF_ON, False, "b_doz"),
    "B_gri": ("Electric Heater", "map", OFF_ON, False, "electric_heater"),
    "B_Oxy1": ("Lambda Probe Reading", "measure", "%", False, "lambda_probe_reading"),
    "B_razP": ("Pelet Level", "measure", "%", False, "pelet_level"),
    "B_razina": ("Fuel Level State", "map", FUEL, False, "b_razina"),
    "B_specG": ("Status Mark", "map", MARK, False, "b_specg"),
    "B_Ppwm": ("PWM Pump", "map", OFF_ON, False, "pwm_pump"),
    "B_zahPpwm": ("PWM Pump Demand", "map", OFF_ON, False, "pwm_pump_demand"),
    "B_P2": ("P2 DHW Recirculation Pump", "map", OFF_ON, False, "p2_pump"),
    "B_P3": ("P3 Pump", "map", OFF_ON, False, "p3_pump"),
    "B_P4": ("P4 Pump", "map", OFF_ON, False, "p4_pump"),
    "K1B_P": ("P1 DHW Pump", "map", OFF_ON, False, "circuit_1k_pump"),
    "K1B_zahP": ("DHW Pump Demand", "map", OFF_ON, False, "k1b_zahp"),
    "K1B_CircType": ("DHW Circuit Type", "raw", None, True, "circuit_1k_heating_type"),
    "B_Paku": ("Accumulator Pump", "map", OFF_ON, False, "accumulator_pump"),
    "B_zahPa": ("Accumulator Pump Demand", "map", OFF_ON, False, "accumulator_pump_demand"),
    "B_Valve": ("Valve State", "map", OFF_ON, True, "valve_state"),
    "B_zahValve": ("Valve Demand", "map", OFF_ON, True, "valve_demand"),
    "B_PTV/GRI": ("DHW / Heating Mode", "map", DHW_HEATING_MODES, False, "dhw_heating_valve"),  # the mode in use; Auto is one of the first three
    "B_PTV/GRI_SEL": ("DHW / Heating Mode Selected", "map", DHW_HEATING_MODES, False, "dhw_heating_valve_select"),
    "B_PTV_PRI": ("DHW Priority", "map", OFF_ON, True, "dhw_priority"),
    "B_VAC_TUR": ("Vacuum Turbine", "map", OFF_ON, True, "vacuum_turbine"),
    "B_VAC_STS": ("Vacuum Status", "number", None, True, "vacuum_status"),
    "B_ODRTMP": ("Boiler Temperature Maintenance", "number", None, True, "boiler_temperature_maintenance"),
    "B_SUP_TYPE": ("Internet Access", "map", SUPERVISION, True, "b_sup_type"),
    "B_KONF": ("Configuration", "map", SCHEMES, True, "configuration"),
    "B_FILE": ("Active File", "text", None, True, "b_file"),
    "B_VER": ("Software Version", "text", None, True, "firmware_version"),
    "B_PRODNAME": ("Product Name", "text", None, True, "product_name"),
    "B_BRAND": ("Brand", "text", None, True, "brand"),
    "B_INST": ("Installation", "text", None, True, "installation"),
    "B_sng": ("Rated Boiler Power", "number", "kW", True, "nominal_power"),
    "B_addConf": ("Accessory Bitmask", "text", None, True, "accessory_bitmask"),
    "B_cm2k": ("CM2K Modules", "number", None, True, "cm2k_modules"),
    "B_signal": ("WiFi Signal", "number", "dB", True, "wifi_signal"),
    "B_Time": ("Controller Clock", "clock", None, True, "controller_clock"),
    "CNT_0": ("Boiler Work + Standby Time", "counter", "min", False, "burner_work"),
    "CNT_15": ("Boiler Work Time", "counter", "min", False, "reserve_counter"),
    "CNT_1": ("Number of Burner Starts", "counter", None, False, "number_of_burner_start"),
    "CNT_2": ("Feeder Screw Work", "counter", "min", False, "feeder_screw_work"),
    "CNT_3": ("Flame Duration", "counter", "min", False, "flame_duration"),
    "CNT_4": ("Fan Working Time", "counter", "min", False, "fan_working_time"),
    "CNT_5": ("Electric Heater Working Time", "counter", "min", False, "electric_heater_working_time"),
    "CNT_6": ("Vacuum Turbine Working Time", "counter", "min", True, "vacuum_turbine_working_time"),
    "CNT_7": ("Vacuum Turbine Cycles", "counter", None, True, "vacuum_turbine_cycles"),
    "CNT_8": ("Time on D6", "counter", "min", False, "time_on_d6"),
    "CNT_9": ("Time on D5", "counter", "min", False, "time_on_d5"),
    "CNT_10": ("Time on D4", "counter", "min", False, "time_on_d4"),
    "CNT_11": ("Time on D3", "counter", "min", False, "time_on_d3"),
    "CNT_12": ("Time on D2", "counter", "min", False, "time_on_d2"),
    "CNT_13": ("Time on D1", "counter", "min", False, "time_on_d1"),
    "CNT_14": ("Time on D0", "counter", "min", False, "time_on_d0"),
    # Known from the cloud integration for the PelTec, not sent by this boiler so far.
    "B_FotV": ("Fire Sensor", "number", "kΩ", True, "b_fotv"),
    "B_HS_AKU": ("Hydraulic Separator / Buffer Config", "raw", None, True, "b_hs_aku"),
    "B_Inp1": ("Input Bitmask", "raw", None, True, "b_inp1"),
    "B_MPC": ("Configured Maximum Power", "raw", None, True, "b_mpc"),
    "B_MPO": ("Output Maximum Power", "raw", None, True, "b_mpo"),
    "B_Out1": ("Auxiliary Output 1", "raw", None, True, "b_out1"),
    "B_REC": ("DHW Recirculation Active", "map", OFF_ON, True, "b_rec"),
    "B_REO": ("DHW Recirculation Pump", "map", OFF_ON, True, "b_reo"),
    "B_Tptv1": ("Secondary DHW Temperature", "temperature", None, True, "b_tptv1"),
    "B_ashC": ("Ash Screw Active", "raw", None, True, "b_ashc"),
    "B_ashSc": ("Ash Cleaner", "raw", None, True, "b_ashsc"),
    "B_bim": ("Bimetal Overheat Sensor", "map", OFF_ON, True, "b_bim"),
    "B_bup": ("Buffer Tank Heat Request", "map", OFF_ON, True, "b_bup"),
    "B_dop": ("Additional Heating", "raw", None, True, "b_dop"),
    "B_fan": ("Fan Speed", "number", "rpm", True, "b_fan"),
    "B_fanB": ("Fan B Speed", "number", "rpm", True, "b_fanb"),
    "B_korNum": ("Working Phase", "raw", None, True, "b_kornum"),
    "B_misP": ("Mixing Valve", "number", "%", True, "b_misp"),
    "B_pres": ("Pressure Sensor", "map", OFF_ON, True, "b_pres"),
    "B_puz": ("Pellet Transporter", "map", OFF_ON, True, "b_puz"),
    "B_rpm": ("Motor Speed", "number", "rpm", True, "b_rpm"),
    "B_tur": ("Turbulator", "map", OFF_ON, True, "b_tur"),
    "B_vanjS": ("Outdoor Sensor Connected", "raw", None, True, "b_vanjs"),
    "B_zlj": ("Log", "raw", None, True, "b_zlj"),
}
NOT_ENTITIES = {
    "PING", "CMD", "wf_req",
    "K1B_onOff",  # the state of the K1 switch
    "K1B_Prec",  # recirculation is pump P2
    # On scheme 43 K1 loads the DHW tank, and there is no room thermostat.
    "K1B_Tsob", "K1B_Tsob1", "K1B_dayNight", "K1B_kor", "K1B_korN", "K1B_korType",
}
# On the PelTec II the same signal under another name, checked on a night of real data: one entity is enough. Any other
# boiler has no switch and was not checked, so it shows them.
PELTEC2_DUPLICATES = frozenset((
    "B_P1", "B_Pk1_k2",  # K1B_P: 6 of 6 changes at the same moment; on scheme 43 this is pump P1
    "B_zahK1_K2",  # K1B_zahP
    "B_Tkm1",  # K1B_Tpol1 rounded to whole degrees: the same DHW sensor
    "B_CMD",  # the state of the boiler switch
))
# Parameters the tested PelTec II has not sent: other boilers, and heating circuits scheme 43 does not use. Names from the
# cloud integration. Kept out of PARAMETERS, whose values the app checks before keeping them: from a boiler nobody has
# tested here, no value is thrown away; one that is not a number is shown as unknown.
OTHER_PARAMETERS = {
    "B_CMD": ("Command Active", "map", OFF_ON, False, "command_active"),
    "B_Pk1_k2": ("K1/K2 Pump", "map", OFF_ON, False, "k1_k2_pump"),
    "B_zahK1_K2": ("K1/K2 Pump Demand", "map", OFF_ON, False, "k1_k2_pump_demand"),
    "B_Tkm1": ("DHW Tank Temperature", "temperature", None, False, "dhw_tank_temperature"),
    "B_Tk1b": ("Boiler Temperature Wood", "temperature", None, False, "boiler_temperature_wood"),
    "B_Tk1p": ("Boiler Temperature Pellet", "temperature", None, False, "boiler_temperature_pellet"),
    "B_Tlo1": ("Firebox Temperature", "temperature", None, False, "firebox_temperature"),
    "B_Tak1": ("Buffer Tank Temperature (Upper)", "temperature", None, False, "buffer_tank_temperature_upper"),
    "B_Tak2": ("Buffer Tank Temperature (Lower)", "temperature", None, False, "buffer_tank_temperature_lower"),
    "B_zahP1": ("Boiler Pump Demand", "map", OFF_ON, False, "boiler_pump_demand"),
    "B_zahP2": ("Second Pump Demand", "map", OFF_ON, False, "second_pump_demand"),
    "B_zahP3": ("Third Pump Demand", "map", OFF_ON, False, "third_pump_demand"),
    "B_priS": ("Air Flow Engine Primary", "number", "%", False, "air_flow_engine_primary"),
    "B_secS": ("Air Flow Engine Secondary", "number", "%", False, "air_flow_engine_secondary"),
    "B_zar": ("Glow", "text", None, False, "glow"),
    "B_pbs": ("Wood Pellet Mode", "text", None, True, "wood_pellet_mode"),
    "B_scs": ("Control Mode", "text", None, True, "control_mode"),
    "B_preuz": ("Take Over", "text", None, True, "take_over"),
    "B_cmsr100": ("Pellet Tank Level", "text", None, False, "pellet_tank_level"),
    "B_freezEn": ("Freeze Guard", "text", None, True, "freeze_guard"),
    "B_freezMon": ("Freeze Monitor", "text", None, True, "freeze_monitor"),
    "B_netMon": ("Remote Start Enabled", "text", None, True, "remote_start_enabled"),
    "B_uklKot": ("Boiler Operational", "text", None, True, "boiler_operational"),
    "B_Add": ("Additional Features", "text", None, True, "additional_features"),
    "B_CP": ("CentroPlus", "text", None, True, "centroplus"),
    "B_KONF_STR": ("Setup", "text", None, True, "setup"),
    "B_WifiVER": ("Wifi Box Version", "text", None, True, "wifi_box_version"),
}
# What a parameter means on a boiler other than the PelTec II, where it differs. Counters are numbered per family: on a
# family with its own list, a counter not in it keeps its raw name rather than the PelTec II's.
_MINUTES = "min"
OTHER_PRODUCTS = {
    "cmpelet": {
        "B_Pk": ("Boiler Pump", "map", OFF_ON, False, "boiler_pump"),
        "B_fan": ("Heater Fan State", "map", OFF_ON, False, "heater_fan_state"),
        "B_zlj": ("Operation Mode", "text", None, False, "operation_mode"),
        "CNT_0": ("Burner Work", "counter", _MINUTES, False, "burner_work"),
        "CNT_1": ("Working DHW Only", "counter", _MINUTES, False, "working_dhw_only"),
        "CNT_2": ("Freeze Protection", "counter", _MINUTES, False, "freeze_protection"),
        "CNT_3": ("Number of Burner Starts", "counter", None, False, "number_of_burner_starts"),
        "CNT_4": ("Fan Working Time", "counter", _MINUTES, False, "fan_working_time"),
        "CNT_5": ("Electric Heater Working Time", "counter", _MINUTES, False, "electric_heater_working_time"),
        "CNT_6": ("Number of Electric Heater Starts", "counter", None, False, "number_of_electric_heater_starts"),
        "CNT_7": ("Vacuum Turbine Working Time", "counter", _MINUTES, False, "vacuum_turbine_working_time"),
        "CNT_8": ("Boiler Pump Working Time", "counter", _MINUTES, False, "boiler_pump_working_time"),
    },
    "biotec": {
        "B_Tpov1": ("Mixer Temperature", "temperature", None, False, "mixer_temperature"),
        "B_Tptv1": ("Domestic Hot Water", "temperature", None, False, "domestic_hot_water"),
        "B_P1": ("Boiler Pump", "map", OFF_ON, False, "boiler_pump"),
        "B_P2": ("Second Pump", "map", OFF_ON, False, "second_pump"),
        "B_P3": ("Third Pump", "map", OFF_ON, False, "third_pump"),
        "B_zlj": ("Operation Mode", "text", None, False, "operation_mode"),
        "CNT_0": ("Burner Work", "counter", _MINUTES, False, "burner_work"),
        "CNT_4": ("Fan Working Time", "counter", _MINUTES, False, "fan_working_time"),
    },
    "biopl": {
        "B_Tpov1": ("Mixer Temperature", "temperature", None, False, "mixer_temperature"),
        "B_Tptv1": ("Domestic Hot Water", "temperature", None, False, "domestic_hot_water"),
        "B_P1": ("Boiler Pump", "map", OFF_ON, False, "boiler_pump"),
        "B_P2": ("Second Pump", "map", OFF_ON, False, "second_pump"),
        "B_P3": ("Third Pump", "map", OFF_ON, False, "third_pump"),
        "B_doz": ("Pellet Dispenser", "map", OFF_ON, False, "pellet_dispenser"),
        "B_razP": ("Fuel Level", "measure", "%", False, "fuel_level"),
        "B_zlj": ("Operation Mode", "text", None, False, "operation_mode"),
        "CNT_0": ("Operation Wood", "counter", _MINUTES, False, "operation_wood"),
        "CNT_1": ("Operation Pellets", "counter", _MINUTES, False, "operation_pellets"),
        "CNT_2": ("Pellets D6", "counter", _MINUTES, False, "pellets_d6"),
        "CNT_3": ("Pellets D5", "counter", _MINUTES, False, "pellets_d5"),
        "CNT_4": ("Pellets D4", "counter", _MINUTES, False, "pellets_d4"),
        "CNT_5": ("Pellets D3", "counter", _MINUTES, False, "pellets_d3"),
        "CNT_6": ("Pellets D2", "counter", _MINUTES, False, "pellets_d2"),
        "CNT_7": ("Startup Wood", "counter", None, False, "startup_wood"),
        "CNT_8": ("Startup Pellets", "counter", None, False, "startup_pellets"),
        "CNT_9": ("DHW And Heating Time", "counter", _MINUTES, False, "dhw_and_heating_time"),
        "CNT_10": ("DHW Only Time", "counter", _MINUTES, False, "dhw_only_time"),
        "CNT_11": ("Fan Time", "counter", _MINUTES, False, "fan_time"),
        "CNT_12": ("Heater Time", "counter", _MINUTES, False, "heater_time"),
        "CNT_13": ("Heater Starts", "counter", None, False, "heater_starts"),
        "CNT_14": ("Screw Feeder Time", "counter", _MINUTES, False, "screw_feeder_time"),
        "CNT_15": ("Grate Cleaning", "counter", None, False, "grate_cleaning"),
    },
}
# Scheme 43 is the PelTec II's: on any other boiler P2 is just the second pump, and the configuration index has no name here.
OTHER_PRODUCT_DEFAULTS = {
    "B_P2": ("P2 Pump", "map", OFF_ON, False, "p2_pump"),
    "B_KONF": ("Configuration", "text", None, True, "configuration"),
}
# Heating circuits: C1-C4, and K2-K4 (K1 is the DHW circuit of scheme 43, named above). Names as in the cloud integration.
CIRCUIT_RE = re.compile(r"^(C[1-4]|K[2-4])B(_[A-Za-z0-9]+)$")
DAY_NIGHT = {0: "Day", 1: "Night", 2: "Program"}
CIRCUIT_PARTS = {
    "_Tpol1": ("Flow Measured Temperature", "temperature", None, False),
    "_Tpol": ("Flow Target Temperature", "setpoint", None, False),
    "_Tsob1": ("Room Measured Temperature", "temperature", None, False),
    "_Tsob": ("Room Target Temperature", "setpoint", None, False),
    "_onOff": ("Heating Circuit Enabled", "map", OFF_ON, False),
    "_P": ("Pump", "map", OFF_ON, False),
    "_zahP": ("Pump Demand", "map", OFF_ON, False),
    "_dayNight": ("Day Night Mode", "map", DAY_NIGHT, False),
    "_Prec": ("Recirculation", "map", OFF_ON, True),
    "_misC": ("Valve Closing", "map", OFF_ON, True),
    "_misO": ("Valve Opening", "map", OFF_ON, True),
    "_kor": ("Room Target Correction", "number", "°C", True),
    "_korN": ("Night Correction", "number", "°C", True),
    "_korType": ("Correction Type", "raw", None, True),
    "_CircType": ("Heating Type", "raw", None, True),
}
# Above the bounds of the portal templates: the flue gas, and the firebox of a wood boiler.
TEMPERATURE_MAX = {"B_Tdpl1": 300.0, "B_Tlo1": 1500.0}
TABLE_RE = re.compile(r"^(PVAL|PDEF|PMIN|PMAX)_")
ALARM_RE = re.compile(r"^[EWI]\d+(\.\d+)?$")
PUMP_RE = re.compile(r"^B_P(\d+)$")

GATEWAY_ENTITY_ID = "sensor.peltec_ii_lambda_gateway_state"
HISTORY_ENTITY_ID = ROOM_PREFIX + "event_history"
SWITCH_ENTITY_ID = "switch.peltec_ii_lambda_boiler_switch"
K1_SWITCH_ENTITY_ID = "switch.k1_heating_circuit_off_on"
K1_SWITCH_SLOT = 272
# Seen on 07.11.2026 when the owner changed them on the panel: PVAL_4_0 together with B_PTV/GRI_SEL, and PVAL_898_0
# with B_PTV_PRI (0 when the priority was switched off, 1 with it on). Written like any setting, read back from PVAL.
DHW_MODE_SLOT = 4
DHW_PRIORITY_SLOT = 898
# A setting goes to the boiler only once it has stayed the same this long: the arrows of a number box send a value at
# every click, and each one would be a write to the controller's memory.
WRITE_SETTLE_SECONDS = 3
CONNECTION_ENTITY_ID = "binary_sensor.centrometal_boiler_system_connection"
ALARM_ENTITY_ID = "binary_sensor.peltec_ii_lambda_alarm"
NUMBER_PREFIX = "number.technical_room_centrometal_peltec_ii_peltec_ii_lambda_"
DHW_MODE_ENTITY_ID = "select.technical_room_centrometal_peltec_ii_peltec_ii_lambda_dhw_heating_mode"
DHW_PRIORITY_ENTITY_ID = "switch.technical_room_centrometal_peltec_ii_peltec_ii_lambda_dhw_priority"
# slot: (name, step, object id, default)
SETTINGS = {
    67: ("Buffer tank temperature", 1, "buffer_tank_temperature", 80),
    68: ("Buffer tank temperature differential", 1, "buffer_tank_temperature_differential", 10),
    70: ("Minimal buffer tank temperature", 1, "minimal_buffer_tank_temperature", 20),
    71: ("Stop buffer tank temperature", 1, "stop_buffer_tank_temperature", 5),
    291: ("DHW temperature", 1, "dhw_temperature_k1_circuit", 50),
    292: ("DHW difference", 1, "dhw_difference_k1_circuit", 5),
    576: ("Boiler difference", 1, "boiler_difference", 5),
}
# The only list of writable slots, with the controller's own limits (min, max), read from the portal 06.11.2026.
# The app takes it from this file as well: it refuses what is not here.
LIMITS = {67: (40, 85), 68: (5, 40), 70: (5, 51), 71: (3, 30), K1_SWITCH_SLOT: (0, 1), 291: (40, 85), 292: (4, 40),
          576: (5, 10), DHW_MODE_SLOT: (0, 3), DHW_PRIORITY_SLOT: (0, 1)}
# Read once an hour and after a write. On scheme 43 only the maximum boiler temperature is computed by the controller
# (buffer tank temperature + 5); 71, the stop buffer tank difference, is set on the panel like the others (owner, 07.11.2026).
READ_SLOTS = tuple(LIMITS)
MAX_AGE_SECONDS = 480
# Settings (PVAL_*) are read once an hour, or when they change on the panel: an hour old is still the current value.
SETTING_MAX_AGE_SECONDS = 3600 + 900
# What the module says about the boiler itself. None of these is in the answer to a read request (see
# tests/data/module_messages.jsonl), and they stay true for as long as the boiler is the same: they never count as old.
DESCRIPTION_KEYS = frozenset(("B_FILE", "B_VER", "B_PRODNAME", "B_BRAND", "B_INST", "B_sng", "B_KONF", "B_addConf",
                              "B_WifiVER"))
# The product in the module's MQTT topics (cm.inst.<product>.<serial>), the same code as the portal's device type. Only
# the PelTec II has been tested; any other boiler is read only: no on/off, no setting written, no settings table read.
TESTED_PRODUCT = "peltec2"
PRODUCT_NAMES = {"peltec2": "PelTec II Lambda", "peltec": "PelTec", "compact": "PelTec Compact",
                 "cmpelet": "CM Pelet-set", "biotec": "BioTec", "biopl": "BioTec-Plus"}
API_VERSION = 1  # the app and the integration update separately: this goes up when one stops understanding the other
COUNTER_SENSORS = {
    "sessions_closed": ("Sessions Closed", SENSOR_PREFIX + "sessions_closed"),
    "malformed_frames": ("Malformed Frames", SENSOR_PREFIX + "malformed_frames"),
    "commands_expired": ("Commands Expired", SENSOR_PREFIX + "commands_expired"),
}
NUMERIC_KINDS = ("temperature", "setpoint", "measure", "counter", "number", "map")


def setting_key(slot):
    """The parameter in which the controller reports a setting."""
    return "K1B_onOff" if slot == K1_SWITCH_SLOT else f"PVAL_{slot}_0"


def valid_raw(key, value):
    """False for a value of the wrong type for a known parameter. Unknown parameters pass: nothing the boiler sends is lost."""
    if key in PARAMETERS and PARAMETERS[key][1] in NUMERIC_KINDS:
        return number(value) is not None
    return True


def is_entity(key, product=None):
    """Whether a parameter gets a sensor. `product` is the boiler's code; None for the tested PelTec II."""
    if key in PARAMETERS:
        return True
    if (product or TESTED_PRODUCT) == TESTED_PRODUCT and key in PELTEC2_DUPLICATES:
        return False
    return key not in NOT_ENTITIES and not TABLE_RE.match(key) and not ALARM_RE.match(key)


def setting_value(values, slot, fallback=None):
    """What the controller holds for a setting: its PVAL, or the regular message that carries the same value."""
    values = values or {}
    value = number(values.get(f"PVAL_{slot}_0"))
    return number(values.get(fallback)) if value is None and fallback else value


def setting_step(slot):
    """The step of the boiler panel: 1 for every setting (checked by the owner, 07.11.2026)."""
    return SETTINGS[slot][1] if slot in SETTINGS else 1


def _product_info(key, product):
    """What a parameter means on a boiler other than the PelTec II, where that differs; None where it does not."""
    own = OTHER_PRODUCTS.get(product, {})
    if key in own:
        return own[key]
    if key.startswith("CNT_") and any(k.startswith("CNT_") for k in own):
        return key, "raw", None, True, key.lower()
    return OTHER_PRODUCT_DEFAULTS.get(key)


def parameter_info(key, product=None):
    """(name, kind, extra, diagnostic, entity_id) for any parameter, also one seen for the first time. `product` is the
    boiler's code; None for the tested PelTec II."""
    product = product or TESTED_PRODUCT
    circuit = CIRCUIT_RE.match(key)
    other = _product_info(key, product) if product != TESTED_PRODUCT else None
    if other is not None:
        name, kind, extra, diagnostic, object_id = other
    elif key in PARAMETERS:
        name, kind, extra, diagnostic, object_id = PARAMETERS[key]
    elif key in OTHER_PARAMETERS:
        name, kind, extra, diagnostic, object_id = OTHER_PARAMETERS[key]
    elif circuit and circuit.group(2) in CIRCUIT_PARTS:
        circuit_name = f"Circuit {circuit.group(1)[1]}" if circuit.group(1)[0] == "C" else f"{circuit.group(1)} Circuit"
        part, kind, extra, diagnostic = CIRCUIT_PARTS[circuit.group(2)]
        name = f"{circuit_name} {part}"
        object_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    elif PUMP_RE.match(key):
        number_ = PUMP_RE.match(key).group(1)
        name, kind, extra, diagnostic, object_id = f"P{number_} Pump", "map", OFF_ON, False, f"p{number_}_pump"
    else:
        name, kind, extra, diagnostic = key, "raw", None, True
        object_id = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    entity_id = object_id if object_id.startswith("sensor.") else SENSOR_PREFIX + object_id
    return name, kind, extra, diagnostic, entity_id


def number(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value) if isinstance(value, str) else value if isinstance(value, (int, float)) else None
    except ValueError:
        return None


def valid_temperature(key, value):
    """Same bounds as the portal templates; -55 and 0 on flue gas or return mean no reading."""
    value = number(value)
    if value is None or not -45.0 < value < TEMPERATURE_MAX.get(key, 145.0):
        return None
    if key in ("B_Tdpl1", "B_Tpov1") and value == 0:
        return None
    return float(value)


def display_value(key, value, product=None):
    """The value shown in Home Assistant for a raw parameter; None when it is not a valid reading."""
    if value is None:
        return None
    _, kind, extra, _, _ = parameter_info(key, product)
    if kind in ("temperature", "setpoint"):
        return valid_temperature(key, value)
    if kind == "stage":
        return f"{value}: {extra[value]}" if value in extra else f"Unknown ({value})"
    if kind == "map":
        code = number(value)
        code = int(code) if code is not None and code == int(code) else value
        return extra.get(code, str(value))
    if kind in ("number", "counter", "measure"):
        return number(value)
    if kind == "clock":
        try:
            return datetime.fromtimestamp(int(str(value), 16), UTC).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            return None
    return str(value)


def stage_attributes(code):
    """The attributes of the cloud entity that automations read."""
    code = "" if code is None else str(code).strip()
    level = int(code[1:]) if len(code) == 2 and code.startswith("D") and code[1:].isdigit() else None
    if code == "OFF":
        group = "off"
    elif code == "S7-3":
        group = "standby"
    elif code.startswith("S7"):
        group = "shutdown"
    elif code == "C0":
        group = "cleaning"
    elif code.startswith("PF"):
        group = "power_failure_recovery"
    elif level is not None:
        group = "modulation"
    elif code.startswith(("S", "SP")):
        group = "ignition_startup"
    else:
        group = "unknown"
    return {"Raw stage code": code, "Stage group": group, "Description": STAGES.get(code, code), "Power level": level}


def extra_attributes(key, values, product=None):
    """Attributes per parameter, the same ones the cloud integration exposes."""
    attributes = {"Original name": key}
    raw = values.get(key)
    if key == "B_STATE":
        attributes.update(stage_attributes(raw))
    elif key == "B_start":
        attributes["Boiler state"] = values.get("B_STATE")
    elif key in ("B_Ppwm", "B_fan01"):
        attributes["raw_value"] = raw
    elif key == "B_Oxy1":
        flame = number(values.get("B_fireS"))
        flame = None if flame is None else flame == 1
        attributes.update({"Raw value": raw, "Measurement active": number(raw) is not None and flame is True,
                           "Boiler state": values.get("B_STATE"), "Flame detected": flame})
    elif key == "B_specG":
        code = number(raw)
        meaning = MARK_DETAILS.get(int(code)) if code is not None else None
        attributes["Raw value"] = raw
        if meaning:
            attributes.update({"Meaning": meaning[0], "Category": meaning[1], "Temporary shutdown": meaning[2],
                               "Documentation": meaning[3]})
    elif key == "B_KONF" and (product or TESTED_PRODUCT) == TESTED_PRODUCT:
        code = number(raw)
        attributes.update(SCHEME_ATTRIBUTES.get(int(code) if code is not None else None, {}))
    elif key in ("B_PTV_PRI", "B_signal"):
        attributes["Raw value"] = raw
    return attributes


def enabled_by_default(key, product=None):
    """Meaning unknown or nothing connected to it: the entity exists, but stays off until someone wants it."""
    return key != "B_Ths1" and parameter_info(key, product)[1] != "raw"


def product_of(status):
    """The boiler's product code. An app older than 1.7.4 does not say it, and it served only the PelTec II."""
    return (status or {}).get("product") or TESTED_PRODUCT


def product_name(product):
    return PRODUCT_NAMES.get(product, f"Centrometal {product}")


def writable(status):
    """Only the tested boiler gets the entities that write: on/off, settings, mode and priority."""
    return product_of(status) == TESTED_PRODUCT


def commands_available(status):
    """Commands wait for the module during a short reconnect in local mode, so a session gap does not take them away."""
    status = status or {}
    return status.get("connection_mode") == "local" and bool(status.get("data_fresh")) and writable(status)


def is_fresh(age, key=None):
    if key in DESCRIPTION_KEYS:
        return isinstance(age, (int, float))
    limit = SETTING_MAX_AGE_SECONDS if key and TABLE_RE.match(key) else MAX_AGE_SECONDS
    return isinstance(age, (int, float)) and age < limit
