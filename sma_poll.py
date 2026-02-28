#!/usr/bin/env python3

from pymodbus.client import ModbusTcpClient
from influxdb import InfluxDBClient
from datetime import datetime
import logging
import sys

# ---------------- CONFIG ---------------- #

INVERTER_IP = "192.168.#.#"
MODBUS_UNIT = 3

# ---------- InfluxDB 1.7 ----------
INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB   = "solar"

LOG_FILE = "/var/log/sma_poll.log"

# -------------- LOGGING ----------------- #

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

# ------------- REGISTERS --------------- #
REGISTERS = {
    "ac_power":         {"addr": 30775, "len": 2, "type": "s32"},
    "total_yield":      {"addr": 30513, "len": 4, "type": "u64"},
    "daily_yield":      {"addr": 30517, "len": 4, "type": "u64"},
    "status_text":      {"addr": 30201, "len": 2, "type": "u32", "status": 1},
    "grid_frequency":   {"addr": 30803, "len": 2, "type": "u32", "scale": 0.01},
    "inverter_temp":    {"addr": 30953, "len": 2, "type": "s32", "scale": 0.1},
}

# ------------- STATUS MAP --------------- #

STATUS_MAP = {
    35: "Fault",
    303: "Off",
    307: "Ok",
    455: "Warning",
    887: "Standby",
}

# ----------- REGISTER HELPERS ----------- #

def decode_u32(regs):
    val = (regs[0] << 16) | regs[1]
    if val == 0xFFFFFFFF: # SMA invalid marker
        return None
    return val

def decode_s32(regs):
    val = decode_u32(regs)
    if val >= 0x80000000:
        val -= 0x100000000
    if val == -2147483648:   # SMA invalid marker
        return None
    return val

def decode_u64(regs):
    return (
        (regs[0] << 48) |
        (regs[1] << 32) |
        (regs[2] << 16) |
        regs[3]
    )


# ------------- MODBUS READ -------------- #

def read_sma_registers(client, register_map):
    results = {}

    for name, spec in register_map.items():
        rr = client.read_holding_registers(address=spec["addr"], count=spec["len"], device_id=MODBUS_UNIT)

        if rr.isError():
            results[name] = None
            continue

        regs = rr.registers

        if spec["type"] == "u32":
            value = decode_u32(regs)

        elif spec["type"] == "s32":
            value = decode_s32(regs)

        elif spec["type"] == "u64":
            value = decode_u64(regs)

        else:
            value = regs

        # Apply scaling if defined
        if value is not None and "scale" in spec:
            value = value * spec["scale"]

        if value is not None and "status" in spec:
            value = STATUS_MAP.get(value, "Unknown")

        results[name] = value

    return results

# ------------- INVERTER READ -------------- #

def read_inverter():

    client = ModbusTcpClient(INVERTER_IP, timeout=5)

    if not client.connect():
        logging.error("Cannot connect to inverter")
        sys.exit(1)

    data = read_sma_registers(client, REGISTERS)

    client.close()

    return data

# ------------- INFLUX WRITE ------------- #

def write_influx(data):

    # ---- InfluxDB ----
    influxclient = InfluxDBClient(
        host=INFLUX_HOST,
        port=INFLUX_PORT
    )

    try:
        # Create database if needed
        databases = [db["name"] for db in influxclient.get_list_database()]
        if INFLUX_DB not in databases:
            influxclient.create_database(INFLUX_DB)
            logging.info(f"Created database: {INFLUX_DB}")

        influxclient.switch_database(INFLUX_DB)
        points = []

        if data["ac_power"] is not None:
            point = {
                "measurement": "sma_tl21",
                "fields": {
                    "ac_power": data["ac_power"],
                    "daily_yield": data["daily_yield"],
                    "total_yield": data["total_yield"],
                    "grid_frequency": data["grid_frequency"],
                    "inverter_temp": data["inverter_temp"],
                    "status": data["status_text"],
                }
            }
            points.append(point)
            influxclient.write_points(points)
            #logging.info(points)
        else:
            laststatus = ""
            query = """
                    SELECT last("status")
                    FROM sma_tl21
                    fill(null)
                    """

            result = influxclient.query(query)

            for point in result.get_points():
                laststatus=point["last"]

            if laststatus!=data["status_text"]:
                logging.info("status changed to: " + data["status_text"])

                point = {
                    "measurement": "sma_tl21",
                    "fields": {
                        "status": data["status_text"],
                    }
                }
                points.append(point)
                influxclient.write_points(points)
                logging.info(points)


    except Exception as e:
        logging.error(f"Influx write failed: {e}")
        sys.exit(1)


# ---------------- MAIN ------------------ #

if __name__ == "__main__":

    data = read_inverter()
    #logging.info(data)
    write_influx(data)

