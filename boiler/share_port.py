# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""Share the boiler's USB serial cable over the network, like ser2net does on the Pi.

Run it on the computer the cable is plugged into, such as a Windows PC:

    uv run share_port.py --port COM3

Heatmon then opens rfc2217://<this computer>:2217. The cable is opened when heatmon
connects and closed when it leaves, so the Lochinvar app or boiler_log.py can use it
in between. A new connection replaces the old one, so heatmon can reconnect after a
network drop. Bytes are passed through unchanged; this script sends nothing itself.
"""

import argparse
import logging
import socket
import threading
import time

import serial
import serial.rfc2217

log = logging.getLogger("share_port")


class Bridge:
    """Copies bytes between one network client and the serial port."""

    def __init__(self, ser: serial.Serial, sock: socket.socket):
        self.ser, self.sock = ser, sock
        self.alive = True
        self.lock = threading.Lock()
        self.rfc2217 = serial.rfc2217.PortManager(ser, self)
        self.threads = [threading.Thread(target=f, daemon=True)
                        for f in (self.to_network, self.to_boiler, self.poll_lines)]
        for t in self.threads:
            t.start()

    def write(self, data: bytes) -> None:  # PortManager sends its replies through this
        with self.lock:
            self.sock.sendall(data)

    def to_network(self) -> None:
        try:
            while self.alive:
                data = self.ser.read(self.ser.in_waiting or 1)
                if data:
                    self.write(b"".join(self.rfc2217.escape(data)))
        except OSError as e:
            if self.alive:
                log.warning("serial port: %s", e)
        self.close()

    def to_boiler(self) -> None:
        try:
            while self.alive and (data := self.sock.recv(1024)):
                self.ser.write(b"".join(self.rfc2217.filter(data)))
        except OSError as e:
            if self.alive:
                log.warning("network: %s", e)
        self.close()

    def poll_lines(self) -> None:
        try:
            while self.alive:
                time.sleep(1)
                self.rfc2217.check_modem_lines()
        except OSError:
            pass

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.ser.close()

    def join(self) -> None:
        for t in self.threads:
            if t is not threading.current_thread():
                t.join()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="serial port, e.g. COM3 or /dev/ttyUSB0")
    p.add_argument("--listen", type=int, default=2217, help="TCP port to listen on (default 2217)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    server = socket.create_server(("", args.listen))
    log.info("sharing %s on TCP port %d", args.port, args.listen)
    bridge = None
    while True:
        sock, addr = server.accept()
        if bridge:
            log.info("new connection, closing the old one")
            bridge.close()
            bridge.join()
        log.info("connected: %s", addr[0])
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            ser = serial.serial_for_url(args.port, baudrate=19200, timeout=1)
        except serial.SerialException as e:
            log.warning("can't open %s: %s", args.port, e)
            sock.close()
            bridge = None
            continue
        bridge = Bridge(ser, sock)


if __name__ == "__main__":
    main()
