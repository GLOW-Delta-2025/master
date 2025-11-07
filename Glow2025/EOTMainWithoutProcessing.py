#!/usr/bin/env python3
"""
GLOW 25' - Mac Mini Control System (Five Arms, Router-Aware, Robust)
Simplified: audio processing removed; listens for gate-like activity to trigger stars.
"""

import time
import threading
import serial
import random
import numpy as np
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, List, Dict, Tuple as Tup

# Audio (simple listener only)
try:
    import sounddevice as sd
    _SD_AVAILABLE = True
except Exception:
    _SD_AVAILABLE = False

# ---------------- CONFIG ----------------
SERIAL_PORT = "/dev/tty.usbmodem83171401"
SERIAL_BAUD = 115200

NUM_ARMS = 5
MAX_STARS_FOR_CLIMAX = 50

PEAK_TIMEOUT = 10.0
STAR_SEND_TIME = 20.0
MAX_BRIGHTNESS = 255
UPDATE_STEP = 20

ACK_TIMEOUT = 0.75
MAX_SEND_RETRIES = 3
ARRIVAL_WARN_AFTER = 8.0
ARRIVAL_TIMEOUT = 15.0
WARN_THROTTLE = 2.0

# Keep-alive
DEFAULT_KEEPALIVE_INTERVAL = 10.0
HEALTH_FAIL_THRESHOLD = 3

# Speed ranges
ARM_SPEED_MIN, ARM_SPEED_MAX = 2, 10
CENTER_SPEED_MIN, CENTER_SPEED_MAX = 8, 25

# Climax
CLIMAX_TIMEOUT_SECONDS = 30.0

DEBUG_FRAMES = False
VERSION = "2025-10-27"

# -------- Simple audio gate activity (no loudness processing) --------
AUDIO_DEVICE_INDEX = None          # None = default CoreAudio input; set int for X32 device index
AUDIO_USE_CHANNELS = 3, 4, 5, 7, 6  # e.g. [3,4,5,7,6] (1-based). None = all channels
GATE_OPEN_THRESHOLD = 0.020        # amplitude declaring gate open
GATE_CLOSE_THRESHOLD = 0.010       # amplitude declaring gate closed
GATE_MIN_FRAMES_OPEN = 2           # consecutive callbacks above open threshold to accept open
AUDIO_VERBOSE = False              # toggle at runtime (add key if desired)
AUDIO_REFRACTORY_S = 0.20          # min seconds between star triggers
# --------------------------------------------------------------------

class DeviceType(Enum):
    MASTER = "MASTER"
    ARM1 = "ARM1"
    ARM2 = "ARM2"
    ARM3 = "ARM3"
    ARM4 = "ARM4"
    ARM5 = "ARM5"
    CENTER = "CENTER"
    TOP = "TOP"

ARMS = [DeviceType[f"ARM{i}"] for i in range(1, NUM_ARMS + 1)]
ALL_DEVICES = ARMS + [DeviceType.CENTER, DeviceType.TOP]

class RequestType(Enum):
    MAKE_STAR = "MAKE_STAR"
    UPDATE_STAR = "UPDATE_STAR"
    SEND_STAR = "SEND_STAR"
    STAR_ARRIVED = "STAR_ARRIVED"
    ADD_STAR_TOP = "ADD_STAR_TOP"
    ADD_STAR_CENTER = "ADD_STAR_CENTER"
    BUILDUP_CLIMAX_CENTER = "BUILDUP_CLIMAX_CENTER"
    CLIMAX_READY = "CLIMAX_READY"
    START_CLIMAX_CENTER = "START_CLIMAX_CENTER"
    START_CLIMAX_TOP = "START_CLIMAX_TOP"
    CLIMAX_DONE_CENTER = "CLIMAX_DONE_CENTER"
    CLIMAX_DONE_TOP = "CLIMAX_DONE_TOP"
    PING = "PING"

class StarState(Enum):
    IDLE = 0
    WAIT_CONFIRM = 1
    ACTIVE = 2
    DISPATCHING = 3
    IN_ANIMATION = 4

class ClimaxState(Enum):
    IDLE = 0
    BUILDUP_WAIT_ACK = 1
    BUILDUP_WAIT_READY = 2
    RUNNING = 3

@dataclass
class Message:
    request_type: RequestType
    target_device: DeviceType
    brightness: Optional[int] = None
    params: Optional[Dict[str, object]] = None
    def to_command(self) -> str:
        cmd = f"!!{self.target_device.value}:REQUEST:{self.request_type.value}"
        payload = dict(self.params) if self.params else {}
        if self.brightness is not None:
            payload.setdefault("BRIGHTNESS", int(self.brightness))
        if payload:
            order = ["SPEED", "COLOR", "BRIGHTNESS", "SIZE"]
            parts = []
            for k in order:
                if k in payload:
                    v = payload[k]
                    parts.append(f"{k}={v}")
            for k,v in payload.items():
                if k not in {"SPEED","COLOR","BRIGHTNESS","SIZE"}:
                    parts.append(f"{k}={v}")
            cmd += "{" + ",".join(parts) + "}"
        cmd += "##"
        return cmd

class Star:
    def __init__(self, arm: DeviceType):
        self.arm = arm
        self.state = StarState.IDLE
        self.active = False
        self.brightness = 0
        self.start_time: Optional[float] = None
        self.last_peak_time: Optional[float] = None
        self.speed: int = ARM_SPEED_MIN
        self.color_int: int = 128
        self.size: int = 5
        self.retry_count = 0
        self.last_send_attempt: Optional[float] = None
        self.awaiting_ack = False
        self.awaiting_arrival = False
        self.confirmed_at: Optional[float] = None
        self.last_warn: Optional[float] = None

@dataclass
class RequestTracker:
    device: DeviceType
    cmd: RequestType
    last_sent: float
    retries: int
    brightness: Optional[int] = None
    params: Optional[Dict[str, object]] = None

PendingKey = Tup[str,str]

class MacMiniController:
    def __init__(self):
        print("[BOOT] Running file:", __file__, "version:", VERSION)
        self.serial = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.1)
        time.sleep(2)
        print(f"[SERIAL] Connected {SERIAL_PORT}")
        self.arms = {arm: Star(arm) for arm in ARMS}
        self.lock = threading.RLock()
        self.stars_collected = 0
        self.climax_state = ClimaxState.IDLE
        self.climax_done_center = False
        self.climax_done_top = False
        self.climax_started_at: Optional[float] = None
        self.climax_timeout_seconds = CLIMAX_TIMEOUT_SECONDS
        self._stop = False

        # Audio listener
        self._audio_stream = None
        self._audio_cycle_next = 1
        self._last_audio_trigger = 0.0
        self._gate_open = False
        self._gate_open_streak = 0
        if _SD_AVAILABLE:
            try:
                dev = AUDIO_DEVICE_INDEX
                info = sd.query_devices(dev, 'input')
                sr = int(info.get('default_samplerate') or 48000)
                max_in = int(info.get('max_input_channels') or 1)
                if isinstance(AUDIO_USE_CHANNELS, (list, tuple)) and AUDIO_USE_CHANNELS:
                    open_channels = max(AUDIO_USE_CHANNELS)
                    monitor_idx = [i-1 for i in AUDIO_USE_CHANNELS if i >= 1]
                else:
                    open_channels = max_in
                    monitor_idx = None
                open_channels = max(1, open_channels)

                def _audio_cb(indata, frames, time_info, status):
                    if status:
                        print(f"[AUDIO][STATUS] {status}")
                    if indata is None:
                        return
                    buf = indata
                    if monitor_idx:
                        if buf.shape[1] < (max(monitor_idx)+1):
                            return
                        buf = buf[:, monitor_idx]
                    activity_amp = float(np.max(np.abs(buf)))
                    if AUDIO_VERBOSE:
                        print(f"[AUDIO][AMP] {activity_amp:.4f}")
                    if activity_amp >= GATE_OPEN_THRESHOLD:
                        self._gate_open_streak += 1
                        if (not self._gate_open) and self._gate_open_streak >= GATE_MIN_FRAMES_OPEN:
                            if (time.time() - self._last_audio_trigger) >= AUDIO_REFRACTORY_S:
                                self._last_audio_trigger = time.time()
                                self._gate_open = True
                                arm = self._audio_cycle_next
                                self._audio_cycle_next = (self._audio_cycle_next % NUM_ARMS) + 1
                                print(f"[AUDIO][GATE] OPEN amp={activity_amp:.4f} -> ARM{arm}")
                                self.trigger_peak(arm)
                    elif activity_amp <= GATE_CLOSE_THRESHOLD:
                        self._gate_open = False
                        self._gate_open_streak = 0

                self._audio_stream = sd.InputStream(
                    device=dev,
                    channels=open_channels,
                    samplerate=sr,
                    dtype='float32',
                    blocksize=512,
                    callback=_audio_cb
                )
                self._audio_stream.start()
                print(f"[AUDIO] Listening device={dev} sr={sr} open_channels={open_channels} monitor={AUDIO_USE_CHANNELS or 'ALL'} thresholds open={GATE_OPEN_THRESHOLD} close={GATE_CLOSE_THRESHOLD}")
            except Exception as e:
                print(f"[AUDIO][WARN] init failed: {e}")
        else:
            print("[AUDIO][WARN] sounddevice not installed.")

        self.pending_confirms: Dict[PendingKey, RequestTracker] = {}
        self.keepalive_interval = DEFAULT_KEEPALIVE_INTERVAL
        self._next_keepalive_due = time.time() + self.keepalive_interval

        now = time.time()
        self.health: Dict[DeviceType, Dict[str, object]] = {
            dev: {"online": True, "failures": 0, "last_seen": now}
            for dev in ALL_DEVICES
        }

        self.show_color_arm: int = 128
        self.show_color_center: str = "#808080"

        threading.Thread(target=self.receive_loop, daemon=True).start()
        print("[MAC MINI] Ready. Keys: 1–5 peaks · k keep-alive · h health · c color · r reset · Ctrl+C exit")

    # Helpers
    def _sync_color_from_arm(self):
        v = int(max(0, min(255, self.show_color_arm)))
        self.show_color_center = f"#{v:02X}{v:02X}{v:02X}"

    def _sync_color_from_center(self):
        s = self.show_color_center.strip().lstrip("#")
        if len(s) == 6:
            try:
                r = int(s[0:2], 16); g = int(s[2:4], 16); b = int(s[4:6], 16)
                self.show_color_arm = max(0, min(255, (r+g+b)//3))
            except ValueError:
                pass

    def set_show_color(self, arm_value: Optional[int] = None, center_hex: Optional[str] = None):
        with self.lock:
            if arm_value is not None:
                self.show_color_arm = int(max(0, min(255, arm_value)))
                self._sync_color_from_arm()
            elif center_hex is not None:
                self.show_color_center = center_hex
                self._sync_color_from_center()

    @staticmethod
    def _size_from_brightness(br: int) -> int:
        br = max(0, min(255, int(br)))
        return max(1, min(20, int(round(1 + br * (19.0 / 255.0)))))

    @staticmethod
    def _rand_speed_for(dev: DeviceType) -> int:
        if dev == DeviceType.CENTER:
            return random.randint(CENTER_SPEED_MIN, CENTER_SPEED_MAX)
        return random.randint(ARM_SPEED_MIN, ARM_SPEED_MAX)

    def set_climax_timeout(self, seconds: float):
        with self.lock:
            self.climax_timeout_seconds = max(5.0, float(seconds))
        print(f"[CLIMAX] Timeout -> {int(self.climax_timeout_seconds)}s")

    # Send command
    def send_command(self, msg: Message):
        if msg.request_type == RequestType.CLIMAX_READY:
            print("[BUGGUARD] Not sending CLIMAX_READY (incoming only).")
            return
        cmd = msg.to_command()
        try:
            self.serial.write(cmd.encode('utf-8'))
            self.serial.flush()
            print(f"[SEND] {cmd}")
        except Exception as e:
            print(f"[ERROR] serial write: {e}")
            return
        key: PendingKey = (msg.target_device.value, msg.request_type.value)
        now = time.time()
        prev = self.pending_confirms.get(key)
        retries = prev.retries if prev else 0
        self.pending_confirms[key] = RequestTracker(
            device=msg.target_device,
            cmd=msg.request_type,
            last_sent=now,
            retries=retries,
            brightness=msg.brightness,
            params=msg.params
        )

    # Receive loop
    def receive_loop(self):
        buffer = ""
        while not self._stop:
            try:
                if self.serial.in_waiting:
                    data = self.serial.read(self.serial.in_waiting).decode('utf-8', errors='ignore')
                    buffer += data
                    while '!!' in buffer and '##' in buffer:
                        start = buffer.find('!!')
                        end = buffer.find('##', start)
                        if end == -1:
                            break
                        end += 2
                        if end - 2 <= start:
                            buffer = buffer[start+2:]
                            continue
                        frame = buffer[start:end]
                        buffer = buffer[end:]
                        self.handle_received(frame)
                else:
                    time.sleep(0.01)
            except Exception as e:
                print(f"[ERROR] receive_loop: {e}")
                time.sleep(0.05)

    @staticmethod
    def _parse_frame(frame: str) -> Optional[Tuple[str,str,str,str,Optional[str]]]:
        s = frame.strip()
        if not (s.startswith("!!") and s.endswith("##")):
            return None
        core = s[2:-2].replace("\r","").replace("\n","").replace("\t","")
        parts = [p.strip() for p in core.split(":")]
        if len(parts) < 4: return None
        src_raw, dest_raw, verb_raw = parts[0], parts[1], parts[2]
        cmd_payload = ":".join(parts[3:]).strip()
        payload = None
        lb = cmd_payload.find("{")
        if lb != -1:
            cmd_raw = cmd_payload[:lb].strip()
            rest = cmd_payload[lb+1:]
            if rest.endswith("}"):
                rest = rest[:-1]
            payload = rest.strip()
        else:
            cmd_raw = cmd_payload.strip()
        return f"[{src_raw}]", f"[{dest_raw}]", verb_raw.upper(), cmd_raw.upper(), payload

    @staticmethod
    def _norm_addr(token: Optional[str]) -> Optional[str]:
        if not token: return None
        t = token.strip()
        if t.startswith("[") and t.endswith("]"):
            t = t[1:-1]
        return t

    def handle_received(self, frame: str):
        parsed = self._parse_frame(frame)
        if not parsed:
            if DEBUG_FRAMES: print(f"[RX][IGN] {frame.strip()}")
            return
        src, dest, verb, cmd, payload = parsed
        src = self._norm_addr(src); dest = self._norm_addr(dest)
        if not (src and dest == "MASTER"):
            if DEBUG_FRAMES: print(f"[RX][IGN] src={src} dest={dest} verb={verb} cmd={cmd}")
            return
        try:
            dev = DeviceType[src]
        except Exception:
            if DEBUG_FRAMES: print(f"[RX][IGN] unknown dev {src}")
            return

        if verb == "CONFIRM" and cmd == RequestType.MAKE_STAR.value and dev.name.startswith("ARM"):
            with self.lock:
                star = self.arms[dev]
                self.pending_confirms.pop((dev.value, RequestType.MAKE_STAR.value), None)
                if star.state == StarState.WAIT_CONFIRM:
                    now = time.time()
                    star.state = StarState.ACTIVE
                    star.start_time = now
                    star.last_peak_time = now
                    print(f"[ACK] {dev.value} MAKE_STAR -> ACTIVE")
            return

        if verb == "CONFIRM":
            if dev in self.health:
                self.health[dev]["online"] = True
                self.health[dev]["failures"] = 0
                self.health[dev]["last_seen"] = time.time()
            self.pending_confirms.pop((dev.value, cmd), None)
            if cmd == RequestType.SEND_STAR.value and dev.name.startswith("ARM"):
                self._on_confirm_send_star(dev)
            return

        if verb == "REQUEST" and cmd == RequestType.STAR_ARRIVED.value and dev.name.startswith("ARM"):
            self._on_star_arrived(dev)
            return

        if verb == "REQUEST" and cmd == RequestType.CLIMAX_READY.value and dev == DeviceType.CENTER:
            with self.lock:
                if self.climax_state == ClimaxState.BUILDUP_WAIT_READY:
                    self.send_command(Message(RequestType.START_CLIMAX_CENTER, DeviceType.CENTER))
                    self.send_command(Message(RequestType.START_CLIMAX_TOP, DeviceType.TOP))
                    self.climax_state = ClimaxState.RUNNING
                    self.climax_started_at = time.time()
                    print("[CLIMAX] READY → START sent")
            return

        if verb == "REQUEST" and cmd == RequestType.CLIMAX_DONE_CENTER.value and dev == DeviceType.CENTER:
            with self.lock:
                self.climax_done_center = True
                print("[CLIMAX] CENTER done")
                self._check_climax_completion()
            return

        if verb == "REQUEST" and cmd == RequestType.CLIMAX_DONE_TOP.value and dev == DeviceType.TOP:
            with self.lock:
                self.climax_done_top = True
                print("[CLIMAX] TOP done")
                self._check_climax_completion()
            return

        if DEBUG_FRAMES:
            print(f"[RX][IGN] {src} {verb}:{cmd}")

    def _check_climax_completion(self):
        if self.climax_done_center and self.climax_done_top:
            print("[CLIMAX] Both done → reset")
            self._reset_show_cycle()
            print(f"[RESET] stars={self.stars_collected} state={self.climax_state.name}")
        else:
            waiting = []
            if not self.climax_done_center: waiting.append("CENTER")
            if not self.climax_done_top: waiting.append("TOP")
            print("[CLIMAX] Waiting for:", ",".join(waiting))

    def _on_confirm_send_star(self, arm: DeviceType):
        with self.lock:
            star = self.arms[arm]
            if star.state == StarState.DISPATCHING and star.awaiting_ack:
                star.awaiting_ack = False
                star.awaiting_arrival = True
                star.state = StarState.IN_ANIMATION
                star.confirmed_at = time.time()
                star.last_warn = None
                print(f"[ACK] {arm.value} SEND_STAR → awaiting STAR_ARRIVED")

    def _on_star_arrived(self, arm: DeviceType):
        with self.lock:
            star = self.arms[arm]
            if star.state in (StarState.IN_ANIMATION, StarState.DISPATCHING):
                star.awaiting_arrival = False
                if self.climax_state == ClimaxState.IDLE:
                    self.stars_collected += 1
                    print(f"[ARRIVED] {arm.value} complete. Total stars: {self.stars_collected}")
                    b = star.brightness
                    size = self._size_from_brightness(b)
                    self.send_command(Message(
                        RequestType.ADD_STAR_TOP, DeviceType.TOP,
                        params={"COLOR": star.color_int, "BRIGHTNESS": b, "SIZE": size}
                    ))
                    self.send_command(Message(
                        RequestType.ADD_STAR_CENTER, DeviceType.CENTER,
                        params={"speed": self._rand_speed_for(DeviceType.CENTER),
                                "brightness": b, "size": size}
                    ))
                    print(f"[COUNT] stars_collected={self.stars_collected}")
                    if self.stars_collected >= MAX_STARS_FOR_CLIMAX:
                        self.send_command(Message(RequestType.BUILDUP_CLIMAX_CENTER, DeviceType.CENTER))
                        self.climax_state = ClimaxState.BUILDUP_WAIT_ACK
                        print(f"[CLIMAX] Threshold {self.stars_collected}/{MAX_STARS_FOR_CLIMAX} reached → BUILDUP sent")
                else:
                    print(f"[ARRIVED] {arm.value} (ignored; climax active)")
                self._reset_arm(star)

    def _reset_arm(self, star: Star):
        star.state = StarState.IDLE
        star.active = False
        star.brightness = 0
        star.start_time = None
        star.last_peak_time = None
        star.retry_count = 0
        star.last_send_attempt = None
        star.awaiting_ack = False
        star.awaiting_arrival = False
        star.confirmed_at = None
        star.last_warn = None

    def _reset_show_cycle(self):
        with self.lock:
            for s in self.arms.values():
                self._reset_arm(s)
            self.pending_confirms.clear()
            self.stars_collected = 0
            self.climax_state = ClimaxState.IDLE
            self.climax_done_center = False
            self.climax_done_top = False
            self.climax_started_at = None

    def _send_keepalives_if_due(self, now: float):
        if now < self._next_keepalive_due:
            return
        self._next_keepalive_due = now + self.keepalive_interval
        for dev in ALL_DEVICES:
            if (dev.value, RequestType.PING.value) in self.pending_confirms:
                continue
            self.send_command(Message(RequestType.PING, dev))
        print(f"[PING] sent interval={int(self.keepalive_interval)}s")

    def set_keepalive_interval(self, seconds: float):
        with self.lock:
            self.keepalive_interval = max(5.0, float(seconds))
            self._next_keepalive_due = time.time() + self.keepalive_interval
        print(f"[PING] interval -> {int(self.keepalive_interval)}s")

    def _handle_ping_failure(self, dev: DeviceType):
        h = self.health.get(dev)
        if not h: return
        h["failures"] = int(h.get("failures",0)) + 1
        if h["failures"] >= HEALTH_FAIL_THRESHOLD:
            if h.get("online", True):
                print(f"[PING][WARN] {dev.value} missed {HEALTH_FAIL_THRESHOLD} → OFFLINE")
            h["online"] = False
        else:
            print(f"[PING][WARN] {dev.value} miss ({h['failures']}/{HEALTH_FAIL_THRESHOLD})")

    def _print_health(self):
        print("\n[HEALTH]")
        for dev in ALL_DEVICES:
            h = self.health[dev]
            age = time.time() - float(h["last_seen"])
            online = "ONLINE" if h["online"] else "OFFLINE"
            print(f"  {dev.value:6s} {online:7s} failures={h['failures']} last_seen={age:4.1f}s")
        print("")

    def trigger_peak(self, arm_num: int):
        arm = DeviceType[f"ARM{arm_num}"]
        with self.lock:
            if self.climax_state in (ClimaxState.BUILDUP_WAIT_ACK, ClimaxState.BUILDUP_WAIT_READY, ClimaxState.RUNNING):
                print(f"[PEAK] Ignored (climax phase: {self.climax_state.name})")
                return
            star = self.arms[arm]; now = time.time()
            if star.state == StarState.IDLE:
                star.state = StarState.WAIT_CONFIRM
                star.active = True
                star.brightness = UPDATE_STEP
                star.speed = self._rand_speed_for(arm)
                star.color_int = int(self.show_color_arm)
                star.size = self._size_from_brightness(star.brightness)
                self.send_command(Message(RequestType.MAKE_STAR, arm,
                                          params={"SPEED": star.speed, "COLOR": star.color_int,
                                                  "BRIGHTNESS": star.brightness, "SIZE": star.size}))
                print(f"[PEAK] New star {arm.value} brightness={star.brightness}")
            elif star.state == StarState.WAIT_CONFIRM:
                print(f"[PEAK] Ignored: {arm.value} waiting ACK")
            elif star.state == StarState.ACTIVE:
                if (arm.value, RequestType.MAKE_STAR.value) in self.pending_confirms:
                    print(f"[PEAK] Ignored: {arm.value} MAKE_STAR pending")
                    return
                star.brightness = min(star.brightness + UPDATE_STEP, MAX_BRIGHTNESS)
                star.last_peak_time = now
                star.size = self._size_from_brightness(star.brightness)
                self.send_command(Message(RequestType.UPDATE_STAR, arm,
                                          params={"SPEED": star.speed, "COLOR": star.color_int,
                                                  "BRIGHTNESS": star.brightness, "SIZE": star.size}))
                print(f"[PEAK] Update {arm.value} brightness={star.brightness}")
            else:
                print(f"[PEAK] Ignored: {arm.value} state={star.state.name}")

    def run(self):
        try:
            while True:
                now = time.time()
                # Retry engine
                for key, tracker in list(self.pending_confirms.items()):
                    if (now - tracker.last_sent) >= ACK_TIMEOUT:
                        if tracker.retries < MAX_SEND_RETRIES:
                            tracker.retries += 1
                            tracker.last_sent = now
                            self.send_command(Message(tracker.cmd, tracker.device,
                                                      brightness=tracker.brightness, params=tracker.params))
                        else:
                            del self.pending_confirms[key]
                            print(f"[ERROR] {tracker.device.value} no CONFIRM after {MAX_SEND_RETRIES} {tracker.cmd.value}")
                            if tracker.device.name.startswith("ARM") and tracker.cmd in (
                                RequestType.MAKE_STAR, RequestType.UPDATE_STAR, RequestType.SEND_STAR):
                                with self.lock:
                                    self._reset_arm(self.arms[tracker.device])
                                print(f"[RECOVER] reset {tracker.device.value}")
                            if tracker.cmd == RequestType.BUILDUP_CLIMAX_CENTER and tracker.device == DeviceType.CENTER:
                                with self.lock:
                                    self.climax_state = ClimaxState.IDLE
                                    self.climax_done_center = False
                                    self.climax_done_top = False
                                    self.climax_started_at = None
                                print("[CLIMAX][RECOVER] Buildup aborted")
                            if tracker.cmd == RequestType.PING:
                                self._handle_ping_failure(tracker.device)

                self._send_keepalives_if_due(now)

                send_queue: List[Star] = []
                with self.lock:
                    for star in self.arms.values():
                        if star.state == StarState.ACTIVE:
                            elapsed = now - (star.start_time or now)
                            idle = now - (star.last_peak_time or now)
                            if elapsed >= STAR_SEND_TIME or idle >= PEAK_TIMEOUT or star.brightness >= MAX_BRIGHTNESS:
                                star.state = StarState.DISPATCHING
                                star.active = False
                                star.awaiting_ack = True
                                star.retry_count = 0
                                send_queue.append(star)
                        elif star.state == StarState.IN_ANIMATION and star.confirmed_at:
                            waited = now - star.confirmed_at
                            if waited > ARRIVAL_TIMEOUT:
                                print(f"[ERROR] {star.arm.value} no STAR_ARRIVED after {ARRIVAL_TIMEOUT:.1f}s → reset")
                                self._reset_arm(star)
                            elif waited > ARRIVAL_WARN_AFTER:
                                if (star.last_warn is None) or (now - star.last_warn >= WARN_THROTTLE):
                                    print(f"[WARN] {star.arm.value} waiting STAR_ARRIVED {waited:.1f}s")
                                    star.last_warn = now

                for s in send_queue:
                    self._try_send_star(s)

                if self.climax_state == ClimaxState.BUILDUP_WAIT_ACK:
                    if (DeviceType.CENTER.value, RequestType.BUILDUP_CLIMAX_CENTER.value) not in self.pending_confirms:
                        self.climax_state = ClimaxState.BUILDUP_WAIT_READY
                        print("[CLIMAX] BUILDUP confirmed → wait CLIMAX_READY")

                with self.lock:
                    if self.climax_state == ClimaxState.RUNNING and self.climax_started_at:
                        waited = now - self.climax_started_at
                        if waited > self.climax_timeout_seconds:
                            print(f"[CLIMAX][TIMEOUT] No DONE after {int(self.climax_timeout_seconds)}s → reset show")
                            self._reset_show_cycle()

                time.sleep(0.1)
        except KeyboardInterrupt:
            self.shutdown()

    def _try_send_star(self, star: Star):
        self.send_command(Message(
            RequestType.SEND_STAR, star.arm,
            params={"SPEED": star.speed, "COLOR": star.color_int,
                    "BRIGHTNESS": star.brightness, "SIZE": star.size}
        ))
        star.last_send_attempt = time.time()
        star.retry_count += 1

    def input_loop(self):
        import sys, termios, tty, select
        def get_key():
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                r,_,_ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    return sys.stdin.read(1)
                return None
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        try:
            while True:
                k = get_key()
                if not k: continue
                if k.isdigit():
                    n = int(k)
                    if 1 <= n <= NUM_ARMS:
                        self.trigger_peak(n)
                        print(f"[KEY] Peak ARM{n}")
                elif k in ("r","R"):
                    with self.lock: self._reset_show_cycle()
                    print("[RESET] Manual")
                elif k in ("k","K"):
                    presets=[30.0,60.0,120.0]
                    try:
                        idx=presets.index(self.keepalive_interval)
                        self.set_keepalive_interval(presets[(idx+1)%len(presets)])
                    except ValueError:
                        self.set_keepalive_interval(60.0)
                elif k in ("h","H"):
                    self._print_health()
                elif k in ("c","C"):
                    self.set_show_color(arm_value=(self.show_color_arm+32)%256)
                    print(f"[COLOR] arm={self.show_color_arm} center={self.show_color_center}")
                elif k in ("t","T"):
                    presets=[30.0,60.0,90.0,120.0]
                    self.set_climax_timeout(presets[(presets.index(self.climax_timeout_seconds)+1)%len(presets)]
                                            if self.climax_timeout_seconds in presets else 60.0)
                elif k == "v":
                    global AUDIO_VERBOSE
                    AUDIO_VERBOSE = not AUDIO_VERBOSE
                    print(f"[AUDIO] verbose={'ON' if AUDIO_VERBOSE else 'OFF'}")
                elif k == "d":
                    if _SD_AVAILABLE:
                        try:
                            for i,d in enumerate(sd.query_devices()):
                                print(f"{i}: {d['name']} IN={d['max_input_channels']} OUT={d['max_output_channels']}")
                        except Exception as e:
                            print("[AUDIO][DEVICES][ERR]", e)
                else:
                    print(f"[KEY] Unknown '{k}'")
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        print("\n[MAC MINI] Shutting down...")
        self._stop = True
        try:
            if self._audio_stream:
                self._audio_stream.stop()
                self._audio_stream.close()
                print("[AUDIO] stream closed")
        except Exception:
            pass
        try:
            self.serial.close()
        except Exception:
            pass

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    controller = MacMiniController()
    threading.Thread(target=controller.run, daemon=True).start()
    controller.input_loop()