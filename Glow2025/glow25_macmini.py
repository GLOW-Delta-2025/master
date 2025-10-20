#!/usr/bin/env python3
"""
GLOW 25' - Mac Mini Control System (Arduino-ready)
Full logic including star creation, buildup, climax, and async confirms.
Supports 5 arms in parallel.
"""

import time
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, List
import numpy as np
import serial
import serial.tools.list_ports

# === Configuration ===
PEAK_TIMEOUT = 2.0
MAX_RETRIES = 3
CLIMAX_WAIT_TIME = 0.5
MAX_STARS_FOR_CLIMAX = 10
SERIAL_BAUD_RATE = 115200
SERIAL_TIMEOUT = 0.1
SERIAL_PORT = "/dev/tty.usbmodem14301"  # replace with your Teensy port

# === Device Enums ===
class DeviceType(Enum):
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
    ADD_STAR_TOP = "ADD_STAR_TOP"
    ADD_STAR_CENTER = "ADD_STAR_CENTER"
    BUILDUP_CLIMAX_CENTER = "BUILDUP_CLIMAX_CENTER"
    CLIMAX_READY = "CLIMAX_READY"

@dataclass
class Message:
    request_type: RequestType
    target_device: DeviceType

    def to_command(self):
        return f"!![{self.target_device.value}]:REQUEST:{self.request_type.value}##"

# === Serial Connection ===
class SerialConnection:
    def __init__(self, port: str = SERIAL_PORT, baud_rate: int = SERIAL_BAUD_RATE):
        self.port = port
        self.baud_rate = baud_rate
        self.serial_conn: Optional[serial.Serial] = None
        self.connected = False
        self.read_buffer = ""

    def connect(self) -> bool:
        try:
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baud_rate,
                timeout=SERIAL_TIMEOUT,
                write_timeout=1.0
            )
            self.connected = True
            time.sleep(2)  # Wait for Teensy reset
            print(f"[SERIAL] Connected to Teensy on {self.port}")
            return True
        except serial.SerialException as e:
            print(f"[SERIAL] Connection failed: {e}")
            return False

    def send_command(self, command: str) -> bool:
        if not self.connected or not self.serial_conn:
            print("[SERIAL] Not connected")
            return False
        try:
            self.serial_conn.write(command.encode('utf-8'))
            self.serial_conn.flush()
            print(f"[SEND] {command.strip()}")
            return True
        except serial.SerialException as e:
            print(f"[SERIAL] Send error: {e}")
            self.connected = False
            return False

    def read_messages(self) -> List[str]:
        messages = []
        if not self.connected or not self.serial_conn:
            return messages
        try:
            while self.serial_conn.in_waiting > 0:
                chunk = self.serial_conn.read(self.serial_conn.in_waiting).decode('utf-8', errors='ignore')
                self.read_buffer += chunk
            while '!!' in self.read_buffer and '##' in self.read_buffer:
                start_idx = self.read_buffer.find('!!')
                end_idx = self.read_buffer.find('##', start_idx)
                if end_idx > start_idx:
                    message = self.read_buffer[start_idx:end_idx+2]
                    messages.append(message)
                    self.read_buffer = self.read_buffer[end_idx+2:]
                else:
                    break
            if len(self.read_buffer) > 1000:
                self.read_buffer = self.read_buffer[-500:]
        except serial.SerialException as e:
            print(f"[SERIAL] Read error: {e}")
            self.connected = False
        return messages

# === Communication Manager ===
class CommunicationManager:
    def __init__(self, serial_conn: SerialConnection):
        self.serial = serial_conn
        self.confirm_events: Dict[str, threading.Event] = {}

    def send_with_retry(self, message: Message, max_retries=MAX_RETRIES, timeout=2.0):
        key = f"{message.target_device.value}:{message.request_type.value}"
        if key not in self.confirm_events:
            self.confirm_events[key] = threading.Event()
        for attempt in range(max_retries):
            self.confirm_events[key].clear()
            self.serial.send_command(message.to_command())
            if self.confirm_events[key].wait(timeout):
                print(f"[SUCCESS] {message.request_type.value} to {message.target_device.value} confirmed")
                return True
            else:
                if attempt < max_retries - 1:
                    print(f"[RETRY] {message.request_type.value} attempt {attempt+2}/{max_retries}")
        print(f"[FAILED] {message.request_type.value} to {message.target_device.value} max retries reached")
        return False

    def handle_incoming_confirm(self, source_device: str, request_type: str):
        key = f"{source_device}:{request_type}"
        if key in self.confirm_events:
            self.confirm_events[key].set()

# === Star Manager ===
class StarManager:
    def __init__(self, comm: CommunicationManager, arm: DeviceType):
        self.comm = comm
        self.arm = arm
        self.star_being_made = False
        self.current_brightness = 0
        self.max_brightness = 255

    def make_star(self):
        if not self.star_being_made:
            self.star_being_made = True
            self.current_brightness = 50
            self.comm.send_with_retry(Message(RequestType.MAKE_STAR, self.arm))
            print(f"[PEAK] Star creation initiated for {self.arm.value}")
        else:
            self.update_star()

    def update_star(self):
        self.current_brightness = min(self.current_brightness + 50, self.max_brightness)
        self.comm.send_with_retry(Message(RequestType.UPDATE_STAR, self.arm))
        print(f"[PEAK] Star brightness updated for {self.arm.value}")

    def send_star(self):
        if self.star_being_made:
            self.comm.send_with_retry(Message(RequestType.SEND_STAR, self.arm))
            self.star_being_made = False
            print(f"[COMPLETE] Star sent from {self.arm.value}")

# === Mac Mini Controller ===
class MacMiniController:
    def __init__(self):
        self.serial = SerialConnection()
        if not self.serial.connect():
            raise ConnectionError("Failed to connect to Teensy")
        self.comm = CommunicationManager(self.serial)
        self.star_managers = [StarManager(self.comm, arm) for arm in [
            DeviceType.ARM1, DeviceType.ARM2, DeviceType.ARM3, DeviceType.ARM4, DeviceType.ARM5
        ]]
        self.running = True
        self.last_peak_time = {mgr.arm.value: None for mgr in self.star_managers}

    def start(self):
        print("[MAC MINI] Starting GLOW 25' Control System (Arduino)")
        # Start a thread to read incoming messages
        threading.Thread(target=self.read_loop, daemon=True).start()
        while self.running:
            spike_input = input("Enter spike arm number(s) 1-5 or 'a' for all: ").strip()
            if spike_input.lower() == 'a':
                spike_arms = self.star_managers
            else:
                spike_arms = []
                for c in spike_input:
                    if c in "12345":
                        idx = int(c)-1
                        spike_arms.append(self.star_managers[idx])
            now = time.time()
            for mgr in spike_arms:
                last_time = self.last_peak_time[mgr.arm.value]
                if last_time is None or (now - last_time) > PEAK_TIMEOUT:
                    mgr.make_star()
                    self.last_peak_time[mgr.arm.value] = now
            # Check stars ready to send
            for mgr in self.star_managers:
                if mgr.star_being_made and mgr.current_brightness >= mgr.max_brightness:
                    mgr.send_star()
            time.sleep(0.01)

    def read_loop(self):
        while self.running:
            messages = self.serial.read_messages()
            for raw_msg in messages:
                parsed = self.parse_message(raw_msg)
                if parsed:
                    self.handle_message(parsed)
            time.sleep(0.01)

    def parse_message(self, raw_message: str):
        """Parse message format: !!SOURCE:[TARGET]:MESSAGE_TYPE:REQUEST_TYPE{params}##"""
        try:
            content = raw_message.replace('!!', '').replace('##', '').strip()
            parts = content.split(':')
            if len(parts) < 3:
                return None
            source = parts[0]
            request_type = parts[3] if len(parts) > 3 else parts[2]
            return {"source": source, "request_type": request_type}
        except:
            return None

    def handle_message(self, msg: dict):
        # Only handle confirms
        self.comm.handle_incoming_confirm(msg["source"], msg["request_type"])

if __name__ == "__main__":
    controller = MacMiniController()
    controller.start()
