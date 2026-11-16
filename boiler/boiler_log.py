# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""Read-only logger for the Lochinvar boiler's front service port.

Polls Modbus holding registers and appends raw values to CSV files so we
can work out what each register means. It only ever sends function 3
(read holding registers). Close the Lochinvar app first: the port can
have only one user.

    uv run boiler_log.py --port COM3
    uv run boiler_log.py --port rfc2217://boilerpi:2217
"""

import argparse
import csv
import time
from datetime import datetime
from pathlib import Path

import serial

SLAVE = 1
STATUS = [(62, 121)]
# The chunks the Lochinvar app reads, in case the boiler is picky about them.
# (The app reads 125 at 1152, overlapping 1276 by one; 124 avoids that.)
PARAMS = [(1000, 16), (1016, 42), (1058, 10), (1068, 84), (1152, 124),
          (1276, 9), (1285, 12), (1297, 123)]


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_request(start: int, count: int) -> bytes:
    body = bytes([SLAVE, 3]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
    return body + crc16(body).to_bytes(2, "little")


def parse_response(frame: bytes, count: int) -> list[int]:
    if len(frame) < 5 or crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise IOError(f"bad or short reply: {frame.hex()}")
    if frame[1] != 3:
        raise IOError(f"boiler refused the read: {frame.hex()}")
    if frame[2] != 2 * count or len(frame) != 5 + 2 * count:
        raise IOError(f"wrong length reply: {frame.hex()}")
    return [int.from_bytes(frame[3 + 2 * i:5 + 2 * i], "big") for i in range(count)]


def read_registers(port: serial.SerialBase, start: int, count: int) -> list[int]:
    port.reset_input_buffer()
    port.write(read_request(start, count))
    head = port.read(3)
    rest = port.read(2 if len(head) == 3 and head[1] & 0x80 else 2 + 2 * count)
    return parse_response(head + rest, count)


def read_blocks(port, blocks) -> list[int]:
    values = []
    for start, count in blocks:
        values += read_registers(port, start, count)
        time.sleep(0.05)
    return values


def temp_f(raw: int) -> str:
    """Boiler temperatures are signed 16-bit: °F = raw / 25 + 932; 0x8000 means no sensor."""
    if raw == 0x8000:
        return "none"
    signed = raw - 0x10000 if raw & 0x8000 else raw
    return f"{signed / 25 + 932:.1f}°F"


def append_row(path: Path, blocks, values: list[int]) -> None:
    new = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        if new:
            names = [f"r{s + i}" for s, n in blocks for i in range(n)]
            writer.writerow(["time"] + names)
        writer.writerow([datetime.now().astimezone().isoformat(timespec="seconds")] + values)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="COM3", help="COM3, /dev/ttyUSB0 or rfc2217://host:port")
    ap.add_argument("--every", type=float, default=2, help="seconds between status reads")
    ap.add_argument("--params-every", type=float, default=300, help="seconds between parameter reads")
    ap.add_argument("--out", type=Path, default=Path("."), help="folder for the CSV files")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    port = None
    next_params = 0.0
    while True:
        started = time.monotonic()
        try:
            if port is None:
                port = serial.serial_for_url(args.port, baudrate=19200, bytesize=8,
                                             parity="N", stopbits=1, timeout=1)
            status = read_blocks(port, STATUS)
            append_row(args.out / "boiler_status.csv", STATUS, status)
            line = f"{datetime.now():%H:%M:%S} status ok: outlet {temp_f(status[0])}, inlet {temp_f(status[11])}"
            if started >= next_params:
                append_row(args.out / "boiler_params.csv", PARAMS, read_blocks(port, PARAMS))
                next_params = started + args.params_every
                line += ", parameters saved"
            print(line, flush=True)
        except (IOError, serial.SerialException) as e:
            print(f"{datetime.now():%H:%M:%S} {e}", flush=True)
            if port is not None:
                port.close()
                port = None
            time.sleep(5)
            continue
        time.sleep(max(0.0, args.every - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
