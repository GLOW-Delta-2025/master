#!/usr/bin/env python3
"""
GLOW 25' - Mac Mini Control System (Manual Test Version)
Controls 5 ARMs with real-time key-triggered audio peaks.
"""

import sys
import select
import termios
import tty
import time
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict

# -------------------- Configuration --------------------
SERIAL_PORT = "/dev/tty.usbmodem14301"
SERIAL_BAUD_RATE = 115200
MAX_RETRIES = 3
PEAK_TIMEOUT = 2.0
STAR_TIME_LIMIT = 10.0
MAX_BRIGHTNESS = 255
UPDATE_INCREMENT = 20

# -------------------- Serial --------------------
import serial

class SerialConnection:
    def __init__(self, port=SERIAL_PORT, baud=SERIAL_BAUD_RATE):
        self.port = port
        self.baud = baud
        self.conn = None
        self.lock = threading.Lock()

    def connect(self):
        try:
            self.conn = serial.Serial(self.port, self.baud, timeout=0.1)
            time.sleep(2)
            print(f"[SERIAL] Connected to Teensy on {self.port}")
            return True
        except Exception as e:
            print(f"[SERIAL] Failed: {e}")
            return False

    def send(self, command: str):
        with self.lock:
            if self.conn:
                self.conn.write(command.encode('utf-8'))
                self.conn.flush()
                print(f"[SEND] {command.strip()}")

# -------------------- Device Enums --------------------
class DeviceType(Enum):
    ARM1 = "ARM1"
    ARM2 = "ARM2"
    ARM3 = "ARM3"
    ARM4 = "ARM4"
    ARM5 = "ARM5"
    CENTER = "CENTER"
    TOP = "TOP"

# -------------------- Message --------------------
@dataclass
class Message:
    request_type: str
    target: DeviceType
    brightness: Optional[int] = None

    def to_command(self):
        cmd = f"!![{self.target.value}]:REQUEST:{self.request_type}"
        if self.brightness is not None:
            cmd += f"{{[{self.brightness}]}}"
        cmd += "##"
        return cmd

# -------------------- Star Manager --------------------
class Star:
    def __init__(self, arm: DeviceType, serial_conn: SerialConnection):
        self.arm = arm
        self.serial = serial_conn
        self.active = False
        self.brightness = 0
        self.start_time = None
        self.last_peak = None

    def start(self):
        if self.active:
            return
        self.active = True
        self.brightness = 50
        self.start_time = time.time()
        self.last_peak = time.time()
        self.serial.send(Message("MAKE_STAR", self.arm, self.brightness).to_command())
        print(f"[PEAK] Star creation initiated for {self.arm.value}")

    def update(self):
        if not self.active:
            return
        self.brightness = min(self.brightness + UPDATE_INCREMENT, MAX_BRIGHTNESS)
        self.last_peak = time.time()
        self.serial.send(Message("UPDATE_STAR", self.arm, self.brightness).to_command())
        print(f"[UPDATE] {self.arm.value} brightness updated to {self.brightness}")

    def check_send(self):
        if not self.active:
            return False
        now = time.time()
        # Send star if max brightness, time limit, or idle
        if (self.brightness >= MAX_BRIGHTNESS) or \
           (now - self.start_time >= STAR_TIME_LIMIT) or \
           (now - self.last_peak >= PEAK_TIMEOUT):
            self.serial.send(Message("SEND_STAR", self.arm, self.brightness).to_command())
            print(f"[COMPLETE] Star sent from {self.arm.value}")
            self.active = False
            return True
        return False

# -------------------- Controller --------------------
class MacMiniController:
    def __init__(self):
        self.serial = SerialConnection()
        if not self.serial.connect():
            raise RuntimeError("Failed to connect to Teensy")
        # Create 5 ARMs
        self.arms = {DeviceType[f"ARM{i}"]: Star(DeviceType[f"ARM{i}"], self.serial) for i in range(1,6)}
        self.running = True

    def input_loop(self):
        print("[TEST] Press keys 1–5 to trigger peaks for respective arms. Ctrl+C to exit.")
        while self.running:
            key = self.get_key()
            if key and key in "12345":
                arm_num = int(key)
                arm = DeviceType[f"ARM{arm_num}"]
                star = self.arms[arm]
                if not star.active:
                    star.start()
                else:
                    star.update()
            # Check all stars for send conditions
            for star in self.arms.values():
                if star.check_send():
                    # Send to CENTER and TOP
                    self.serial.send(Message("ADD_STAR_TOP", DeviceType.TOP).to_command())
                    print(f"[STAR] Star added to TOP")
                    self.serial.send(Message("ADD_STAR_CENTER", DeviceType.CENTER).to_command())
                    print(f"[STAR] Star added to CENTER")
            time.sleep(0.05)

    @staticmethod
    def get_key():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            dr, dw, de = select.select([sys.stdin], [], [], 0.1)
            if dr:
                return sys.stdin.read(1)
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

# -------------------- Main --------------------
if __name__ == "__main__":
    try:
        controller = MacMiniController()
        controller.input_loop()
    except KeyboardInterrupt:
        print("\n[MAC MINI] Shutting down...")
    finally:
        if controller.serial.conn:
            controller.serial.conn.close()
            print("[SERIAL] Disconnected")
