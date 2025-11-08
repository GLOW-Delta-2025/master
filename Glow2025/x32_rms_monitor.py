#!/usr/bin/env python3
"""Behringer X32 channel RMS monitor using osc4py3.

This script connects to the X32 OSC interface, subscribes to the meter feed,
parses the returned OSC blob, and keeps a running RMS / dBFS estimate for each
input channel. It is intentionally lightweight so you can layer in custom peak
logic later on.

Requirements:
    pip install osc4py3

The X32 must have "X-Control (UDP)" enabled (a.k.a. X-Remote) and the Mac/PC
running this script must be on the same subnet. The script keeps the session
alive by sending /xremote every 9 seconds.
"""
from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from typing import List

from osc4py3.as_eventloop import *
from osc4py3 import oscbuildparse


@dataclass
class ChannelLevel:
    """Container for decoded channel metering information."""

    raw: float = 0.0  # raw float from X32 (0.0 .. 1.0)
    rms: float = 0.0  # RMS amplitude (0.0 .. 1.0)
    dbfs: float = -120.0  # Converted to dBFS (approximate)
    peak_hold: float = -120.0  # Highest dBFS seen since last reset
    last_update: float = 0.0  # time.time() of last sample

    def update(self, raw_value: float, timestamp: float) -> None:
        raw_clamped = max(0.0, min(1.0, raw_value))
        rms = math.sqrt(raw_clamped)
        if rms > 0.0:
            db = 20.0 * math.log10(rms)
        else:
            db = -120.0

        self.raw = raw_clamped
        self.rms = rms
        self.dbfs = db
        self.last_update = timestamp
        if db > self.peak_hold:
            self.peak_hold = db

    def reset_peak(self) -> None:
        self.peak_hold = self.dbfs


class X32RMSMonitor:
    """Subscribe to X32 /meters feed and expose per-channel RMS values."""

    def __init__(
        self,
        x32_ip: str,
        x32_port: int = 10023,
        listen_port: int = 10024,
        channel_count: int = 32,
        meters_path: str = "/meters/1",
        keepalive_interval: float = 9.0,
    ) -> None:
        self.x32_ip = x32_ip
        self.x32_port = x32_port
        self.listen_port = listen_port
        self.channel_count = channel_count
        self.meters_path = meters_path
        self.keepalive_interval = keepalive_interval

        self.levels: List[ChannelLevel] = [ChannelLevel() for _ in range(channel_count)]
        self._lock = threading.Lock()
        self._running = False
        self._keepalive_thread = None

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return

        print(f"[INIT] Starting X32 RMS Monitor (osc4py3)...")
        print(f"[INIT] X32 address: {self.x32_ip}:{self.x32_port}")
        print(f"[INIT] Listening on: 0.0.0.0:{self.listen_port}")
        print(f"[INIT] Monitoring path: {self.meters_path}")
        
        self._running = True
        
        # Initialize osc4py3
        osc_startup()
        
        # Create UDP client for sending to X32
        osc_udp_client(self.x32_ip, self.x32_port, "x32")
        
        # Create UDP server for receiving from X32
        osc_udp_server("0.0.0.0", self.listen_port, "monitor")
        
        # Register handler for meter data
        osc_method(self.meters_path, self._handle_meter_blob)
        
        time.sleep(0.5)  # Let OSC initialize

        # CRITICAL: Send /xremote FIRST to enable remote control
        print(f"[NET] Sending /xremote to enable remote control...")
        self._send_xremote()
        osc_process()
        time.sleep(0.3)  # Wait for X32 to enable remote mode
        
        # Subscribe to meters - X32 requires explicit subscription
        print(f"[NET] Subscribing to {self.meters_path}...")
        msg = oscbuildparse.OSCMessage("/meters", None, [self.meters_path])
        osc_send(msg, "x32")
        osc_process()
        time.sleep(0.2)
        
        # Send subscription again to ensure it's received
        print(f"[NET] Re-sending meter subscription...")
        msg = oscbuildparse.OSCMessage("/meters", None, [self.meters_path])
        osc_send(msg, "x32")
        osc_process()
        
        print(f"[INIT] ✓ Initialization complete. Waiting for meter data...")
        print(f"[INIT] (If no data appears within 5 seconds, check X32 Setup -> Remote)")

        # Start OSC processing thread FIRST
        self._osc_thread = threading.Thread(target=self._osc_loop, daemon=True)
        self._osc_thread.start()
        
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

    def stop(self) -> None:
        self._running = False
        osc_terminate()
        print("\n[STOP] OSC connection closed.")

    # ------------------------------------------------------------------
    # Data access helpers
    # ------------------------------------------------------------------
    def snapshot(self) -> List[ChannelLevel]:
        """Return a shallow copy of the current channel levels."""
        with self._lock:
            return [ChannelLevel(**vars(level)) for level in self.levels]

    def reset_peaks(self) -> None:
        with self._lock:
            for level in self.levels:
                level.reset_peak()

    # ------------------------------------------------------------------
    # OSC handlers & background loops
    # ------------------------------------------------------------------
    def _osc_loop(self) -> None:
        """Process OSC messages continuously"""
        while self._running:
            osc_process()
            time.sleep(0.01)
    
    def _handle_meter_blob(self, address, tags, data, client_address):
        """Parse incoming meter blob and update channel levels.
        
        osc4py3 handler signature: (address, tags, data, client_address)
        data is a list/tuple where data[0] contains the OSC blob as bytes.
        """
        if not data or len(data) == 0:
            return
        
        # Extract blob from data[0]
        blob = data[0]
        
        # Debug: print first reception
        if not hasattr(self, '_first_meter_received'):
            self._first_meter_received = True
            print(f"[DEBUG] ✓ First meter data received! Address: {address}, Size: {len(blob)} bytes")

        now = time.time()
        # Each channel is a big-endian float (4 bytes)
        with self._lock:
            for ch in range(self.channel_count):
                offset = ch * 4
                if offset + 4 > len(blob):
                    break
                raw_value = struct.unpack_from(">f", blob, offset)[0]
                self.levels[ch].update(raw_value, now)

    def _keepalive_loop(self) -> None:
        """Send periodic /xremote keepalive and meter subscription renewal."""
        meter_renew_counter = 0
        while self._running:
            self._send_xremote()
            osc_process()
            
            # Renew meter subscription every 3 keepalive cycles (27 seconds)
            # Some X32 firmware versions require periodic re-subscription
            meter_renew_counter += 1
            if meter_renew_counter >= 3:
                try:
                    msg = oscbuildparse.OSCMessage("/meters", None, [self.meters_path])
                    osc_send(msg, "x32")
                    osc_process()
                except Exception:
                    pass
                meter_renew_counter = 0
            
            time.sleep(self.keepalive_interval)

    def _send_xremote(self) -> None:
        try:
            msg = oscbuildparse.OSCMessage("/xremote", None, [])
            osc_send(msg, "x32")
        except Exception:
            pass


def print_loop(monitor: X32RMSMonitor, interval: float = 0.5, channels: int = 8) -> None:
    """Simple console visualiser for quick testing."""
    try:
        while True:
            # OSC processing happens in dedicated thread now
            levels = monitor.snapshot()
            print("\033[2J\033[H", end="")  # clear terminal
            print("Behringer X32 RMS Monitor (osc4py3)\n")
            print("Ch |   Raw   |   RMS   |   dBFS  | Peak dB  | Updated")
            print("---+---------+---------+---------+----------+----------------")
            for idx, level in enumerate(levels[:channels]):
                age = time.time() - level.last_update
                print(
                    f"{idx+1:2d} | {level.raw:7.4f} | {level.rms:7.4f} | "
                    f"{level.dbfs:7.1f} | {level.peak_hold:8.1f} | {age:5.2f}s ago"
                )
            print("\nPress Ctrl+C to stop. Use monitor.reset_peaks() to clear peak holds.")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopping monitor...")
        monitor.stop()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="X32 channel RMS monitor (python-osc)")
    parser.add_argument("--ip", default="192.168.0.3", help="X32 mixer IP address")
    parser.add_argument("--port", type=int, default=10023, help="X32 OSC port")
    parser.add_argument(
        "--listen", type=int, default=10024, help="Local UDP port to receive meter data"
    )
    parser.add_argument(
        "--channels", type=int, default=32, help="Number of input channels to decode"
    )
    parser.add_argument(
        "--meters-path",
        default="/meters/1",
        help="Meter tap to subscribe to (e.g. /meters/0 pre-fader, /meters/1 post-fader)",
    )
    parser.add_argument(
        "--display",
        type=int,
        default=8,
        help="How many channels to display in the console visualiser",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Refresh interval for the console output (seconds)",
    )

    args = parser.parse_args()

    monitor = X32RMSMonitor(
        x32_ip=args.ip,
        x32_port=args.port,
        listen_port=args.listen,
        channel_count=args.channels,
        meters_path=args.meters_path,
    )
    monitor.start()
    print_loop(monitor, interval=args.interval, channels=args.display)


if __name__ == "__main__":
    main()
