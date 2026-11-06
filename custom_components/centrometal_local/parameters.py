"""Every parameter of a PelTec II Lambda: name, how it is shown, and its entity_id.

Names and values are in English and match the cloud integration Internetrevizor/hass-centrometal-boiler (Apache-2.0),
so automations and dashboards keep working. Translate them in Home Assistant, not here.
A parameter that is not listed still gets a diagnostic sensor under its raw name, so nothing the boiler sends is lost.
"""
from datetime import datetime, timezone
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
    "B_PTV/GRI": ("DHW / Heating Valve", "number", None, True, "dhw_heating_valve"),
    "B_PTV/GRI_SEL": ("DHW / Heating Valve Select", "number", None, True, "dhw_heating_valve_select"),
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
    # Known from the fork for PelTec, not sent by this boiler so far.
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
    # The same signal under another name, checked on a night of real data: one entity is enough.
    "B_P1", "B_Pk1_k2",  # K1B_P: 6 of 6 changes at the same moment; on scheme 43 this is pump P1
    "B_zahK1_K2",  # K1B_zahP
    "B_Tkm1",  # K1B_Tpol1 rounded to whole degrees: the same DHW sensor
    "B_CMD",  # the state of the boiler switch
    "K1B_onOff",  # the state of the K1 switch
    "K1B_Prec",  # recirculation is pump P2
    # On scheme 43 K1 loads the DHW tank, and there is no room thermostat.
    "K1B_Tsob", "K1B_Tsob1", "K1B_dayNight", "K1B_kor", "K1B_korN", "K1B_korType",
}
TABLE_RE = re.compile(r"^(PVAL|PDEF|PMIN|PMAX)_")
ALARM_RE = re.compile(r"^[EWI]\d+(\.\d+)?$")
PUMP_RE = re.compile(r"^B_P(\d+)$")

GATEWAY_ENTITY_ID = "sensor.peltec_ii_lambda_gateway_state"
HISTORY_ENTITY_ID = ROOM_PREFIX + "event_history"
SWITCH_ENTITY_ID = "switch.peltec_ii_lambda_boiler_switch"
K1_SWITCH_ENTITY_ID = "switch.k1_heating_circuit_off_on"
K1_SWITCH_SLOT = 272
CONNECTION_ENTITY_ID = "binary_sensor.centrometal_boiler_system_connection"
ALARM_ENTITY_ID = "binary_sensor.peltec_ii_lambda_alarm"
NUMBER_PREFIX = "number.technical_room_centrometal_peltec_ii_peltec_ii_lambda_"
# slot: (name, step, object id, default)
SETTINGS = {
    67: ("Buffer tank temperature", 1, "buffer_tank_temperature", 80),
    68: ("Buffer tank temperature differential", 1, "buffer_tank_temperature_differential", 10),
    70: ("Minimal buffer tank temperature", 1, "minimal_buffer_tank_temperature", 20),
    71: ("Stop buffer tank temperature", 1, "stop_buffer_tank_temperature", 5),
    291: ("DHW temperature", 0.1, "dhw_temperature_k1_circuit", 50),
    292: ("DHW difference", 0.1, "dhw_difference_k1_circuit", 5),
    576: ("Boiler difference", 1, "boiler_difference", 5),
}
LIMITS = {67: (40, 85), 68: (5, 40), 70: (5, 51), 71: (3, 30), 291: (40, 85), 292: (4, 40), 576: (5, 10)}
MAX_AGE_SECONDS = 480


def is_entity(key):
    return key not in NOT_ENTITIES and not TABLE_RE.match(key) and not ALARM_RE.match(key)


def parameter_info(key):
    """(name, kind, extra, diagnostic, entity_id) for any parameter, also one seen for the first time."""
    if key in PARAMETERS:
        name, kind, extra, diagnostic, object_id = PARAMETERS[key]
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
    if value is None or not -45.0 < value < (300.0 if key == "B_Tdpl1" else 145.0):
        return None
    if key in ("B_Tdpl1", "B_Tpov1") and value == 0:
        return None
    return float(value)


def display_value(key, value):
    """The value shown in Home Assistant for a raw parameter; None when it is not a valid reading."""
    if value is None:
        return None
    _, kind, extra, _, _ = parameter_info(key)
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
            return datetime.fromtimestamp(int(str(value), 16), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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


def extra_attributes(key, values):
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
    elif key == "B_KONF":
        code = number(raw)
        attributes.update(SCHEME_ATTRIBUTES.get(int(code) if code is not None else None, {}))
    elif key in ("B_PTV_PRI", "B_signal"):
        attributes["Raw value"] = raw
    return attributes


def enabled_by_default(key):
    """Meaning unknown or nothing connected to it: the entity exists, but stays off until someone wants it."""
    return key != "B_Ths1" and parameter_info(key)[1] != "raw"


def is_fresh(age):
    return isinstance(age, (int, float)) and age < MAX_AGE_SECONDS
