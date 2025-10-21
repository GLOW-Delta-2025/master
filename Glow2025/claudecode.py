#!/usr/bin/env python3
"""
GLOW 25' - Mac Mini Control System
Audio reactive lighting control with 5 arms, X32 Rack integration
Handles star creation, updates, and climax sequence
"""

import time
import threading
import serial
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ---------------- CONFIG ----------------
SERIAL_PORT = "/dev/tty.usbmodem14301"
SERIAL_BAUD = 115200
MAX_STARS_FOR_CLIMAX = 10
PEAK_TIMEOUT = 2.0
STAR_SEND_TIME = 10.0  # seconds before auto-send
MAX_BRIGHTNESS = 255
UPDATE_STEP = 50  # brightness increase per peak
# ----------------------------------------

class DeviceType(Enum):
    MASTER = "MASTER"
    ARM1 = "ARM1"
    ARM2 = "ARM2"
    ARM3 = "ARM3"
    ARM4 = "ARM4"
    ARM5 = "ARM5"
    CENTER = "CENTER"
    TOP = "TOP"

class RequestType(Enum):
    MAKE_STAR = "MAKE_STAR"
    UPDATE_STAR = "UPDATE_STAR"
    SEND_STAR = "SEND_STAR"
    STAR_ARRIVED = "STAR_ARRIVED"
    CLIMAX_READY = "CLIMAX_READY"
    ADD_STAR_TOP = "ADD_STAR_TOP"
    ADD_STAR_CENTER = "ADD_STAR_CENTER"

@dataclass
class Message:
    request_type: RequestType
    target_device: DeviceType
    brightness: Optional[int] = None

    def to_command(self) -> str:
        cmd = f"!![{self.target_device.value}]:REQUEST:{self.request_type.value}"
        if self.brightness is not None:
            cmd += f"{{[{self.brightness}]}}"
        cmd += "##"
        return cmd

# ---------------- STAR MANAGER ----------------
class Star:
    def __init__(self, arm: DeviceType):
        self.arm = arm
        self.active = False
        self.brightness = 0
        self.start_time = None
        self.last_peak_time = None
        self.sent = False

class MacMiniController:
    def __init__(self):
        self.serial = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.1)
        time.sleep(2)
        print(f"[SERIAL] Connected to Teensy on {SERIAL_PORT}")

        # 5 arms
        self.arms = {DeviceType[f"ARM{i}"]: Star(DeviceType[f"ARM{i}"]) for i in range(1, 6)}
        self.lock = threading.Lock()

        # Global counter
        self.stars_collected = 0

        # Receive thread
        threading.Thread(target=self.receive_loop, daemon=True).start()

        print("[MAC MINI] Initialized - Manual 5 ARM control ready")
        print("[TEST] Press keys 1–5 to trigger peaks for respective arms. Ctrl+C to exit.")

    # ---------------- SEND COMMANDS ----------------
    def send_command(self, msg: Message):
        cmd = msg.to_command()
        self.serial.write(cmd.encode('utf-8'))
        self.serial.flush()
        print(f"[SEND] {cmd}")

    # ---------------- RECEIVE LOOP ----------------
    def receive_loop(self):
        buffer = ""
        while True:
            try:
                if self.serial.in_waiting:
                    data = self.serial.read(self.serial.in_waiting).decode('utf-8', errors='ignore')
                    buffer += data
                    print(data)
                    while '!!' in buffer and '##' in buffer:
                        start = buffer.find('!!')
                        end = buffer.find('##', start) + 2
                        msg = buffer[start:end]
                        buffer = buffer[end:]
                        self.handle_received(msg)
            except Exception as e:
                print(f"[ERROR] Receive loop: {e}")

    def handle_received(self, msg: str):
        msg = msg.strip()
        print(f"[RECEIVED] {msg}")
        # Can parse confirmations or STAR_ARRIVED if needed
        # Currently just logs

    # ---------------- PEAK TRIGGER ----------------
    def trigger_peak(self, arm_num: int):
        arm = DeviceType[f"ARM{arm_num}"]
        with self.lock:
            star = self.arms[arm]
            now = time.time()
            if not star.active:
                # Start new star
                star.active = True
                star.start_time = now
                star.last_peak_time = now
                star.brightness = UPDATE_STEP
                self.send_command(Message(RequestType.MAKE_STAR, arm, star.brightness))
                print(f"[PEAK] Star creation initiated for {arm.value}")
            else:
                # Update existing star
                star.brightness = min(star.brightness + UPDATE_STEP, MAX_BRIGHTNESS)
                star.last_peak_time = now
                self.send_command(Message(RequestType.UPDATE_STAR, arm, star.brightness))
                print(f"[PEAK] Star brightness updated for {arm.value}")

    # ---------------- MAIN LOOP ----------------
    def run(self):
        try:
            while True:
                with self.lock:
                    now = time.time()
                    for star in self.arms.values():
                        if star.active and not star.sent:
                            elapsed = now - star.start_time
                            idle = now - star.last_peak_time
                            if elapsed >= STAR_SEND_TIME or idle >= PEAK_TIMEOUT or star.brightness >= MAX_BRIGHTNESS:
                                self.send_command(Message(RequestType.SEND_STAR, star.arm, star.brightness))
                                star.sent = True
                                star.active = False
                                self.stars_collected += 1
                                print(f"[COMPLETE] Star sent at brightness {star.brightness} (Total stars: {self.stars_collected})")
                                # Send to TOP and CENTER
                                self.send_command(Message(RequestType.ADD_STAR_TOP, DeviceType.TOP))
                                print(f"[STAR] Star added to TOP ({self.stars_collected}/{MAX_STARS_FOR_CLIMAX})")
                                self.send_command(Message(RequestType.ADD_STAR_CENTER, DeviceType.CENTER))
                                print(f"[STAR] Star added to CENTER ({self.stars_collected}/{MAX_STARS_FOR_CLIMAX})")
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[MAC MINI] Shutting down...")
            self.serial.close()

    # ---------------- MANUAL INPUT LOOP ----------------
    def input_loop(self):
        import sys, termios, tty, select
        def get_key():
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                [i, o, e] = select.select([sys.stdin], [], [], 0.1)
                if i:
                    return sys.stdin.read(1)
                else:
                    return None
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        while True:
            key = get_key()
            if key and key in "12345":
                self.trigger_peak(int(key))
                print(f"[KEY] Triggered peak for ARM{key}")

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    controller = MacMiniController()
    threading.Thread(target=controller.run, daemon=True).start()
    controller.input_loop()
1