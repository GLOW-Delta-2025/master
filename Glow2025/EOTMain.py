#!/usr/bin/env python3
"""
GLOW 25' - Mac Mini Control System (Five Arms, Router-Aware, Robust)
Audio-reactive lighting control with 5 arms and Teensy router.
 
Protocol expectations (as seen by the Mac Mini):
- Outgoing (we send):            !![DEVICE]:REQUEST:<CMD>{...}##
  (Router will forward as):      !!MASTER:[DEVICE]:REQUEST:<CMD>{...}##
- Incoming CONFIRM from device:  !![DEVICE]:MASTER:CONFIRM:<CMD>##
- Incoming events to master:     !![DEVICE]:MASTER:REQUEST:<EVENT>##
 
Behavior:
- Peaks (keys 1–5) create/update a star on the corresponding arm.
- When conditions hit, we SEND_STAR and wait for CONFIRM with retries (ACK_TIMEOUT).
- Only after STAR_ARRIVED do we count the star and update TOP & CENTER.
- When stars_collected >= MAX_STARS_FOR_CLIMAX:
    * Send BUILDUP_CLIMAX_CENTER (NO PARAMS; retry/confirm).
    * Wait for CLIMAX_READY from CENTER, then send START_CLIMAX_CENTER (NO PARAMS) & START_CLIMAX_TOP (NO PARAMS).
    * Arms are disabled during buildup/climax.
    * Wait for BOTH CLIMAX_DONE_CENTER and CLIMAX_DONE_TOP, then reset the whole show state.
 
Robustness:
- Generic confirm/retry engine for ALL requests (with MAX_SEND_RETRIES).
- WARN if STAR_ARRIVED takes too long; ERROR & reset if it never comes.
- Idempotent handlers (ignore duplicates/late messages safely).
- Keep-alive PINGs to all devices on a configurable interval; health tracking.
- Param payloads per device:
    * ARM:    SPEED 2–10 (int), COLOR int (0–255), BRIGHTNESS, SIZE (1–20)
    * CENTER: (only for ADD_STAR_CENTER) SPEED 8–25 (int), COLOR hex, BRIGHTNESS, SIZE
    * TOP:    (only for ADD_STAR_TOP)    COLOR int (0–255), BRIGHTNESS, SIZE
"""
 
import time
import threading
import serial
import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, List, Dict, Tuple as Tup


# Optional audio spike library integration
try:
    from lib.audiolib import AudioProcessorLib
except Exception:
    AudioProcessorLib = None
 
# ---------------- CONFIG ----------------
SERIAL_PORT = "/dev/tty.usbmodem83171401" #"/dev/tty.usbmodem14301" 
SERIAL_BAUD = 115200
 
NUM_ARMS = 5
MAX_STARS_FOR_CLIMAX = 25
 
PEAK_TIMEOUT = 10.0         # seconds without peaks before auto-send
STAR_SEND_TIME = 20.0       # seconds from MAKE_STAR confirm before auto-send
MAX_BRIGHTNESS = 255
UPDATE_STEP = 50            # brightness bump per peak
 
ACK_TIMEOUT = 0.75          # wait for CONFIRM before retrying any command
MAX_SEND_RETRIES = 3        # max attempts for any command
ARRIVAL_WARN_AFTER = 8.0    # warn if no STAR_ARRIVED after this many seconds
ARRIVAL_TIMEOUT = 15.0      # give up on STAR_ARRIVED after this many seconds and reset arm
WARN_THROTTLE = 2.0         # throttle warnings to at most once per arm per 2 seconds
 
# Keep-alive
DEFAULT_KEEPALIVE_INTERVAL = 10.0   # seconds; adjustable live
HEALTH_FAIL_THRESHOLD = 3           # consecutive failed pings to mark offlines
 
# Device-specific parameter ranges
ARM_SPEED_MIN, ARM_SPEED_MAX = 2, 10
CENTER_SPEED_MIN, CENTER_SPEED_MAX = 8, 25
 
# Climax timeout (configurable)
CLIMAX_TIMEOUT_SECONDS = 30.0       # default; can be changed via set_climax_timeout()
 
# Logging
DEBUG_FRAMES = False       # set True to see ignored frames/noise
VERSION = "2025-10-27"
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
 
ARMS = [DeviceType[f"ARM{i}"] for i in range(1, NUM_ARMS + 1)]
ALL_DEVICES = ARMS + [DeviceType.CENTER, DeviceType.TOP]
 
class RequestType(Enum):
    # Star flow
    MAKE_STAR = "MAKE_STAR"
    UPDATE_STAR = "UPDATE_STAR"
    SEND_STAR = "SEND_STAR"
    STAR_ARRIVED = "STAR_ARRIVED"
 
    # Star count visuals
    ADD_STAR_TOP = "ADD_STAR_TOP"
    ADD_STAR_CENTER = "ADD_STAR_CENTER"
 
    # Climax flow
    BUILDUP_CLIMAX_CENTER = "BUILDUP_CLIMAX_CENTER"  # Mac → CENTER (NO PARAMS)
    CLIMAX_READY = "CLIMAX_READY"                    # CENTER → Mac (REQUEST)
    START_CLIMAX_CENTER = "START_CLIMAX_CENTER"      # Mac → CENTER (NO PARAMS)
    START_CLIMAX_TOP = "START_CLIMAX_TOP"            # Mac → TOP (NO PARAMS)
    CLIMAX_DONE_CENTER = "CLIMAX_DONE_CENTER"        # CENTER → Mac (REQUEST)
    CLIMAX_DONE_TOP = "CLIMAX_DONE_TOP"              # TOP → Mac (REQUEST)
 
    # Keep-alive
    PING = "PING"
 
class StarState(Enum):
    IDLE = 0
    WAIT_CONFIRM = 1      # waiting for CONFIRM:MAKE_STAR
    ACTIVE = 2            # building brightness via peaks (after MAKE_STAR confirmed)
    DISPATCHING = 3       # SEND_STAR sent (via retry engine), waiting for CONFIRM
    IN_ANIMATION = 4      # CONFIRM(SEND_STAR) received, waiting for STAR_ARRIVED
 
class ClimaxState(Enum):
    IDLE = 0
    BUILDUP_WAIT_ACK = 1      # sent BUILDUP_CLIMAX_CENTER, waiting for CONFIRM
    BUILDUP_WAIT_READY = 2    # waiting for REQUEST:CLIMAX_READY (from CENTER)
    RUNNING = 3               # after START_* has been sent
 
@dataclass
class Message:
    request_type: RequestType
    target_device: DeviceType
    brightness: Optional[int] = None           # kept for convenience
    params: Optional[Dict[str, object]] = None # payload; values can be int or str (for hex)
 
    def to_command(self) -> str:
        cmd = f"!!{self.target_device.value}:REQUEST:{self.request_type.value}"
        # Merge brightness into params if provided
        payload = dict(self.params) if self.params else {}
        if self.brightness is not None:
            payload.setdefault("BRIGHTNESS", int(self.brightness))
        if payload:
            # Keep a friendly order for standard fields
            key_order = ["SPEED", "COLOR", "BRIGHTNESS", "SIZE"]
            parts = []
            for k in key_order:
                if k in payload:
                    v = payload[k]
                    if isinstance(v, str):
                        parts.append(f"{k}={v}")
                    else:
                        parts.append(f"{k}={int(v)}")
            # Include any extra fields not in key_order
            for k, v in payload.items():
                if k not in {"SPEED", "COLOR", "BRIGHTNESS", "SIZE"}:
                    if isinstance(v, str):
                        parts.append(f"{k}={v}")
                    else:
                        parts.append(f"{k}={int(v)}")
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
 
        # Device-specific star attributes
        self.speed: int = ARM_SPEED_MIN
        self.color_int: int = 128  # 0-255 (ARM form)
        self.size: int = 5
 
        # SEND_STAR handshake
        self.retry_count = 0
        self.last_send_attempt: Optional[float] = None
        self.awaiting_ack = False
        self.awaiting_arrival = False
        self.confirmed_at: Optional[float] = None
 
        # Warn throttle
        self.last_warn: Optional[float] = None
 
# ---------- Generic request tracker (retries for ALL requests) ----------
@dataclass
class RequestTracker:
    device: DeviceType
    cmd: RequestType
    last_sent: float
    retries: int
    brightness: Optional[int] = None
    params: Optional[Dict[str, object]] = None
 
PendingKey = Tup[str, str]  # (device.value, request_type.value)
 
class MacMiniController:
    def __init__(self):
        print("[BOOT] Running file:", __file__, "version:", VERSION)
        self.serial = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.1)
        time.sleep(2)
        print(f"[SERIAL] Connected to Teensy on {SERIAL_PORT}")
 
        self.arms = {arm: Star(arm) for arm in ARMS}
 
        # RLock to avoid deadlocks when helpers call helpers under the same lock
        self.lock = threading.RLock()
 
        self.stars_collected = 0
 
        # Climax control
        self.climax_state = ClimaxState.IDLE
        self.climax_done_center = False
        self.climax_done_top = False
        self.climax_started_at: Optional[float] = None
        self.climax_timeout_seconds = CLIMAX_TIMEOUT_SECONDS
 
        self._stop = False
 
        # Optional audio processor library (spike detector) integration
        self.audio_lib = None
        if AudioProcessorLib is not None:
            try:
                # Map audio channels to arms via simple modulo mapping.
                self.audio_lib = AudioProcessorLib(start_stream=True, device=1, channels=[3,4,5,7,6])
                def _spike_cb(ch, spike, avg_db, noise_db):
                    try:
                        arm_num = self.audio_lib.processor.channels.index(ch) if ch in self.audio_lib.processor.channels else None
                        arm_num = (arm_num or 0) + 1
                        print(f"[AUDIO] Spike on channel {ch} -> triggering ARM{arm_num}")
                        self.trigger_peak(arm_num)
                    except Exception as e:
                        print(f"[AUDIO][ERROR] spike callback failed: {e}")
                self.audio_lib.register_spike_callback(_spike_cb)
                self.audio_lib.start()
                print("[AUDIO] AudioProcessorLib started and spike callback registered.")
            except Exception as e:
                print(f"[AUDIO][ERROR] Failed to start AudioProcessorLib: {e}")
 
        # pending confirmations for all outgoing requests
        self.pending_confirms: Dict[PendingKey, RequestTracker] = {}
 
        # keep-alive state
        self.keepalive_interval = DEFAULT_KEEPALIVE_INTERVAL
        self._next_keepalive_due = time.time() + self.keepalive_interval
 
        # device health
        now = time.time()
        self.health: Dict[DeviceType, Dict[str, object]] = {
            dev: {"online": True, "failures": 0, "last_seen": now}
            for dev in ALL_DEVICES
        }
 
        # Shared show color in two forms; keep them in sync
        self.show_color_arm: int = 128              # 0-255
        self.show_color_center: str = "#808080"     # hex code
 
        threading.Thread(target=self.receive_loop, daemon=True).start()
 
        print("[MAC MINI] Initialized - Manual 5 ARM control ready")
        print("[TEST] Keys: 1–5 peaks · 'k' cycle keep-alive (30/60/120s) · 'h' health · 'R' manual reset · Ctrl+C exit.")
 
    # --------- Helpers: color, speed, size ----------
    def _sync_color_from_arm(self):
        v = int(max(0, min(255, self.show_color_arm)))
        hexv = f"#{v:02X}{v:02X}{v:02X}"
        self.show_color_center = hexv
 
    def _sync_color_from_center(self):
        s = self.show_color_center.strip()
        if s.startswith("#"):
            s = s[1:]
        if len(s) == 6:
            try:
                r = int(s[0:2], 16)
                g = int(s[2:4], 16)
                b = int(s[4:6], 16)
                self.show_color_arm = max(0, min(255, (r + g + b) // 3))
            except ValueError:
                pass
 
    def set_show_color(self, arm_value: Optional[int] = None, center_hex: Optional[str] = None):
        """Change color dynamically; keeps both forms in sync."""
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
        print(f"[CLIMAX] Timeout set to {int(self.climax_timeout_seconds)}s.")
 
    # ---------------- SEND COMMANDS ----------------
    def send_command(self, msg: Message):
        if msg.request_type == RequestType.CLIMAX_READY:
            print("[BUGGUARD] Refusing to send CLIMAX_READY from Mac. Expected as incoming CENTER event.")
            return
 
        cmd = msg.to_command()
        try:
            self.serial.write(cmd.encode('utf-8'))
            self.serial.flush()
            print(f"[SEND] {cmd}")
        except Exception as e:
            print(f"[ERROR] Serial write failed: {e}")
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
 
    # ---------------- RECEIVE LOOP ----------------
    def receive_loop(self):
        buffer = ""
        while not self._stop:
            try:
                if self.serial.in_waiting:
                    data = self.serial.read(self.serial.in_waiting).decode('utf-8', errors='ignore')
                    print(data)
                    buffer += data
                    while '!!' in buffer and '##' in buffer:
                        start = buffer.find('!!')
                        end = buffer.find('##', start) + 2
                        if end - 2 <= start:
                            buffer = buffer[start+2:]
                            continue
                        frame = buffer[start:end]
                        buffer = buffer[end:]
                        self.handle_received(frame)
                else:
                    time.sleep(0.01)
            except Exception as e:
                print(f"[ERROR] Receive loop: {e}")
                time.sleep(0.05)
 
    # ---------------- FRAME PARSER ----------------
    @staticmethod
    def _parse_frame(frame: str) -> Optional[Tuple[str, str, str, str, Optional[str]]]:
        s = frame.strip()
        if not (s.startswith("!!") and s.endswith("##")):
            return None
        core = s[2:-2]
        core = core.replace("\r", "").replace("\n", "").replace("\t", "")
        parts = [p.strip() for p in core.split(":")]
        if len(parts) < 4:
            return None
        src_raw, dest_raw, verb_raw = parts[0], parts[1], parts[2]
        cmd_and_payload = ":".join(parts[3:]).strip()
        payload = None
        lb = cmd_and_payload.find("{")
        if lb != -1:
            cmd_raw = cmd_and_payload[:lb].strip()
            right = cmd_and_payload[lb + 1 :]
            if right.endswith("}"):
                right = right[:-1]
            payload = right.strip()
        else:
            cmd_raw = cmd_and_payload.strip()
        verb = verb_raw.replace(" ", "").upper()
        cmd = cmd_raw.replace(" ", "").upper()
        src = f"[{src_raw}]"
        dest = f"[{dest_raw}]"
        return src, dest, verb, cmd, payload
 
    @staticmethod
    def _norm_addr(token: Optional[str]) -> Optional[str]:
        if token is None:
            return None
        t = token.strip()
        if t.startswith("[") and t.endswith("]"):
            t = t[1:-1]
        return t
 
    # ---------------- HANDLE RECEIVED ----------------
    def handle_received(self, frame: str):
        parsed = self._parse_frame(frame)
        if not parsed:
            if DEBUG_FRAMES:
                print(f"[RECEIVED] (ignored/unparsed) {frame.strip()}")
            return
 
        src, dest, verb, cmd, payload = parsed
        src = self._norm_addr(src)
        dest = self._norm_addr(dest)
 
        if not (src and dest == "MASTER"):
            if DEBUG_FRAMES:
                print(f"[RECEIVED] (ignored) src={src} dest={dest} verb={verb} cmd={cmd} payload={payload}")
            return
 
        try:
            dev = DeviceType[src]
        except Exception:
            if DEBUG_FRAMES:
                print(f"[RECEIVED] (ignored unknown device) {src}")
            return
 
        # ---- MAKE_STAR ACK ----
        if verb == "CONFIRM" and cmd == RequestType.MAKE_STAR.value and dev.name.startswith("ARM"):
            with self.lock:
                star = self.arms[dev]
                self.pending_confirms.pop((dev.value, RequestType.MAKE_STAR.value), None)
                if star.state == StarState.WAIT_CONFIRM:
                    now = time.time()
                    star.state = StarState.ACTIVE
                    star.start_time = now
                    star.last_peak_time = now
                    print(f"[ACK] {dev.value} confirmed MAKE_STAR; now ACTIVE")
            return
 
        # ---- Generic CONFIRM ----
        if verb == "CONFIRM":
            if dev in self.health:
                self.health[dev]["online"] = True
                self.health[dev]["failures"] = 0
                self.health[dev]["last_seen"] = time.time()
            self.pending_confirms.pop((dev.value, cmd), None)
            if cmd == RequestType.SEND_STAR.value and dev.name.startswith("ARM"):
                self._on_confirm_send_star(dev)
            return
 
        # ---- REQUESTS ----
        if verb == "REQUEST" and cmd == RequestType.STAR_ARRIVED.value and dev.name.startswith("ARM"):
            self._on_star_arrived(dev)
            return
 
        if verb == "REQUEST" and cmd == RequestType.CLIMAX_READY.value and dev == DeviceType.CENTER:
            with self.lock:
                if self.climax_state == ClimaxState.BUILDUP_WAIT_READY:
                    # START CLIMAX (NO PARAMS)
                    self.send_command(Message(RequestType.START_CLIMAX_CENTER, DeviceType.CENTER))
                    self.send_command(Message(RequestType.START_CLIMAX_TOP, DeviceType.TOP))
                    self.climax_state = ClimaxState.RUNNING
                    self.climax_started_at = time.time()   # start timeout window
                    print("[CLIMAX] CLIMAX_READY received → START_CLIMAX_CENTER & START_CLIMAX_TOP (no params) sent.")
            return
 
        if verb == "REQUEST" and cmd == RequestType.CLIMAX_DONE_CENTER.value and dev == DeviceType.CENTER:
            with self.lock:
                self.climax_done_center = True
                print("[CLIMAX] CENTER reports CLIMAX_DONE_CENTER.")
                self._check_climax_completion()
            return
 
        if verb == "REQUEST" and cmd == RequestType.CLIMAX_DONE_TOP.value and dev == DeviceType.TOP:
            with self.lock:
                self.climax_done_top = True
                print("[CLIMAX] TOP reports CLIMAX_DONE_TOP.")
                self._check_climax_completion()
            return
 
        if DEBUG_FRAMES:
            print(f"[RECEIVED] (ignored) {src}->{dest} {verb}:{cmd}")
 
    # ---------------- CLIMAX COMPLETION CHECK ----------------
    def _check_climax_completion(self):
        both = self.climax_done_center and self.climax_done_top
        if both:
            print("[CLIMAX] Both CENTER and TOP done → resetting show state…")
            self._reset_show_cycle()
            print(f"[RESET] stars_collected={self.stars_collected} "
                  f"climax_state={self.climax_state.name} "
                  f"pending_confirms={len(self.pending_confirms)}")
            print("[CLIMAX] Show state reset (ready for new cycle).")
        else:
            waiting = []
            if not self.climax_done_center:
                waiting.append("CENTER")
            if not self.climax_done_top:
                waiting.append("TOP")
            print(f"[CLIMAX] Waiting for: {', '.join(waiting)}…")
 
    # ---------------- CONFIRM & ARRIVAL HANDLERS ----------------
    def _on_confirm_send_star(self, arm: DeviceType):
        with self.lock:
            star = self.arms[arm]
            if star.state == StarState.DISPATCHING and star.awaiting_ack:
                star.awaiting_ack = False
                star.awaiting_arrival = True
                star.state = StarState.IN_ANIMATION
                star.confirmed_at = time.time()
                star.last_warn = None
                print(f"[ACK] {arm.value} confirmed SEND_STAR; waiting for STAR_ARRIVED")
 
    def _on_star_arrived(self, arm: DeviceType):
        with self.lock:
            star = self.arms[arm]
            if star.state in (StarState.IN_ANIMATION, StarState.DISPATCHING):
                star.awaiting_arrival = False
 
                if self.climax_state == ClimaxState.IDLE:
                    self.stars_collected += 1
                    print(f"[ARRIVED] {arm.value} animation complete. Total stars: {self.stars_collected}")
 
                    # Capture brightness before reset for center/top params
                    b = star.brightness
                    size = self._size_from_brightness(b)
 
                    # TOP — send params (NO SPEED), COLOR int (0–255)
                    self.send_command(Message(
                        RequestType.ADD_STAR_TOP, DeviceType.TOP,
                        params={
                            "COLOR": star.color_int,
                            "BRIGHTNESS": b,
                            "SIZE": size
                        }
                    ))
 
                    # CENTER — note: keys are intentionally lowercase per your current firmware expectation
                    self.send_command(Message(
                        RequestType.ADD_STAR_CENTER, DeviceType.CENTER,
                        params={
                            "speed": self._rand_speed_for(DeviceType.CENTER),
                            # "COLOR": self.show_color_center,  # left commented per your current code
                            "brightness": b,
                            "size": size
                        }
                    ))
                    print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!![COUNT] stars_collected={self.stars_collected}")
 
                    if self.stars_collected >= MAX_STARS_FOR_CLIMAX:
                        self.send_command(Message(RequestType.BUILDUP_CLIMAX_CENTER, DeviceType.CENTER))
                        self.climax_state = ClimaxState.BUILDUP_WAIT_ACK
                        print(f"[CLIMAX] Threshold {self.stars_collected}/{MAX_STARS_FOR_CLIMAX} reached → BUILDUP_CLIMAX_CENTER (no params).")
                else:
                    print(f"[ARRIVED] {arm.value} (ignored for count; climax phase active)")
 
                self._reset_arm(star)
 
    # ---------------- RESET HELPERS ----------------
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
 
    # ---------------- KEEP-ALIVE ----------------
    def _send_keepalives_if_due(self, now: float):
        if now < self._next_keepalive_due:
            return
        self._next_keepalive_due = now + self.keepalive_interval
        for dev in ALL_DEVICES:
            key = (dev.value, RequestType.PING.value)
            if key in self.pending_confirms:
                continue
            self.send_command(Message(RequestType.PING, dev))
        print(f"[PING] Keep-alive sent to all devices (interval={int(self.keepalive_interval)}s).")
 
    def set_keepalive_interval(self, seconds: float):
        with self.lock:
            self.keepalive_interval = max(5.0, float(seconds))
            self._next_keepalive_due = time.time() + self.keepalive_interval
        print(f"[PING] Keep-alive interval set to {int(self.keepalive_interval)}s.")
 
    def _handle_ping_failure(self, dev: DeviceType):
        if dev not in self.health:
            return
        h = self.health[dev]
        h["failures"] = int(h.get("failures", 0)) + 1
        if h["failures"] >= HEALTH_FAIL_THRESHOLD:
            if h.get("online", True):
                print(f"[PING][WARN] {dev.value} missed {HEALTH_FAIL_THRESHOLD} keep-alives → marking OFFLINE.")
            h["online"] = False
        else:
            print(f"[PING][WARN] {dev.value} missed keep-alive (fail {h['failures']}/{HEALTH_FAIL_THRESHOLD}).")
 
    def _print_health(self):
        print("\n[HEALTH] Device status:")
        for dev in ALL_DEVICES:
            h = self.health[dev]
            age = time.time() - float(h["last_seen"])
            online = "ONLINE " if h["online"] else "OFFLINE"
            print(f"  - {dev.value:6s}  {online}  failures={h['failures']}  last_seen={age:4.1f}s ago")
        print("")
 
    # ---------------- PEAK TRIGGER ----------------
    def trigger_peak(self, arm_num: int):
        arm = DeviceType[f"ARM{arm_num}"]
        with self.lock:
            if self.climax_state in (ClimaxState.BUILDUP_WAIT_ACK, ClimaxState.BUILDUP_WAIT_READY, ClimaxState.RUNNING):
                print(f"[PEAK] Ignored: climax phase is active ({self.climax_state.name})")
                return
 
            star = self.arms[arm]
            now = time.time()
 
            if star.state == StarState.IDLE:
                star.state = StarState.WAIT_CONFIRM
                star.active = True
                star.brightness = UPDATE_STEP
                star.start_time = None
                star.last_peak_time = None
                star.speed = self._rand_speed_for(arm)
                star.color_int = int(self.show_color_arm)
                star.size = self._size_from_brightness(star.brightness)
 
                self.send_command(Message(
                    RequestType.MAKE_STAR, arm,
                    params={
                        "SPEED": star.speed,
                        "COLOR": star.color_int,
                        "BRIGHTNESS": star.brightness,
                        "SIZE": star.size
                    }
                ))
                print(f"[PEAK] New star on {arm.value} (brightness={star.brightness})")
 
            elif star.state == StarState.WAIT_CONFIRM:
                print(f"[PEAK] Ignored: {arm.value} waiting for MAKE_STAR confirm")
 
            elif star.state == StarState.ACTIVE:
                if (arm.value, RequestType.MAKE_STAR.value) in self.pending_confirms:
                    print(f"[PEAK] Ignored: {arm.value} MAKE_STAR not yet confirmed (pending ACK)")
                    return
                star.brightness = min(star.brightness + UPDATE_STEP, MAX_BRIGHTNESS)
                star.last_peak_time = now
                star.size = self._size_from_brightness(star.brightness)
                self.send_command(Message(
                    RequestType.UPDATE_STAR, arm,
                    params={
                        "SPEED": star.speed,
                        "COLOR": star.color_int,
                        "BRIGHTNESS": star.brightness,
                        "SIZE": star.size
                    }
                ))
                print(f"[PEAK] Update {arm.value} -> brightness={star.brightness}")
            else:
                print(f"[PEAK] Ignored: {arm.value} is {star.state.name}")
 
    # ---------------- MAIN LOOP ----------------
    def run(self):
        try:
            while True:
                now = time.time()
 
                # Generic retry engine for ALL pending requests
                for key, tracker in list(self.pending_confirms.items()):
                    if (now - tracker.last_sent) >= ACK_TIMEOUT:
                        if tracker.retries < MAX_SEND_RETRIES:
                            self.pending_confirms[key].retries += 1
                            self.pending_confirms[key].last_sent = now
                            self.send_command(Message(tracker.cmd, tracker.device,
                                                      brightness=tracker.brightness,
                                                      params=tracker.params))
                        else:
                            # Retries exhausted: clean up and recover appropriately
                            del self.pending_confirms[key]
                            print(f"[ERROR] {tracker.device.value} no CONFIRM after {MAX_SEND_RETRIES} {tracker.cmd.value} attempts.")
 
                            # Arm safety: reset arm so it doesn't hang in WAIT_CONFIRM/ACTIVE/DISPATCHING
                            if tracker.device.name.startswith("ARM") and tracker.cmd in (
                                RequestType.MAKE_STAR,
                                RequestType.UPDATE_STAR,
                                RequestType.SEND_STAR,
                            ):
                                with self.lock:
                                    self._reset_arm(self.arms[tracker.device])
                                print(f"[RECOVER] {tracker.device.value} reset after {tracker.cmd.value} failed.")
 
                            # Climax safety: abort buildup if CENTER never confirms
                            if tracker.cmd == RequestType.BUILDUP_CLIMAX_CENTER and tracker.device == DeviceType.CENTER:
                                with self.lock:
                                    self.climax_state = ClimaxState.IDLE
                                    self.climax_done_center = False
                                    self.climax_done_top = False
                                    self.climax_started_at = None
                                print("[CLIMAX][RECOVER] Aborted buildup (no CONFIRM). Show state reset to IDLE.")
 
                            # Health handling for PINGs
                            if tracker.cmd == RequestType.PING:
                                self._handle_ping_failure(tracker.device)
 
                # Keep-alive
                self._send_keepalives_if_due(now)
 
                # Handle star lifecycle & auto-dispatch & timeouts
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
                        elif star.state == StarState.IN_ANIMATION:
                            if star.confirmed_at:
                                waited = now - star.confirmed_at
                                if waited > ARRIVAL_TIMEOUT:
                                    print(f"[ERROR] {star.arm.value} no STAR_ARRIVED after {ARRIVAL_TIMEOUT:.1f}s. Resetting arm.")
                                    self._reset_arm(star)
                                elif waited > ARRIVAL_WARN_AFTER:
                                    if (star.last_warn is None) or (now - star.last_warn >= WARN_THROTTLE):
                                        print(f"[WARN] {star.arm.value} STAR_ARRIVED taking long (> {ARRIVAL_WARN_AFTER:.1f}s, waiting {waited:.1f}s)")
                                        star.last_warn = now
 
                # Perform serial writes outside the lock
                for s in send_queue:
                    self._try_send_star(s)
 
                # Transition after BUILDUP ack
                if self.climax_state == ClimaxState.BUILDUP_WAIT_ACK:
                    if (DeviceType.CENTER.value, RequestType.BUILDUP_CLIMAX_CENTER.value) not in self.pending_confirms:
                        self.climax_state = ClimaxState.BUILDUP_WAIT_READY
                        print("[CLIMAX] BUILDUP_CLIMAX_CENTER confirmed → waiting for CLIMAX_READY from CENTER.")
 
                # ---- CLIMAX timeout check ----
                with self.lock:
                    if self.climax_state == ClimaxState.RUNNING and self.climax_started_at is not None:
                        waited = time.time() - self.climax_started_at
                        if waited > self.climax_timeout_seconds:
                            print(f"[CLIMAX][TIMEOUT] No DONE from TOP and/or CENTER after {int(self.climax_timeout_seconds)}s "
                                  f"(center_done={self.climax_done_center}, top_done={self.climax_done_top}). Resetting show state.")
                            self._reset_show_cycle()
 
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.shutdown()
 
    def _try_send_star(self, star: Star):
        self.send_command(Message(
            RequestType.SEND_STAR, star.arm,
            params={
                "SPEED": star.speed,
                "COLOR": star.color_int,
                "BRIGHTNESS": star.brightness,
                "SIZE": star.size
            }
        ))
        star.last_send_attempt = time.time()
        star.retry_count += 1
 
    # ---------------- MANUAL INPUT LOOP ----------------
    def input_loop(self):
        import sys, termios, tty, select
        def get_key():
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                i, _, _ = select.select([sys.stdin], [], [], 0.1)
                if i:
                    ch = sys.stdin.read(1)
                    return ch
                return None
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
 
        try:
            while True:
                key = get_key()
                if not key:
                    continue
                if key.isdigit():
                    n = int(key)
                    if 1 <= n <= NUM_ARMS:
                        self.trigger_peak(n)
                        print(f"[KEY] Peak for ARM{n}")
                elif key in ("r", "R"):
                    with self.lock:
                        self._reset_show_cycle()
                    print("[DEV] Manual reset via keyboard.")
                elif key in ("k", "K"):
                    presets = [30.0, 60.0, 120.0]
                    try:
                        idx = presets.index(self.keepalive_interval)
                        self.set_keepalive_interval(presets[(idx + 1) % len(presets)])
                    except ValueError:
                        self.set_keepalive_interval(60.0)
                elif key in ("h", "H"):
                    self._print_health()
                elif key in ("c", "C"):
                    new_val = (self.show_color_arm + 32) % 256
                    self.set_show_color(arm_value=new_val)
                    print(f"[COLOR] show_color_arm={self.show_color_arm} show_color_center={self.show_color_center}")
                elif key in ("t", "T"):
                    # quick test: cycle climax timeout presets
                    presets = [30.0, 60.0, 90.0, 120.0]
                    self.set_climax_timeout(presets[(presets.index(self.climax_timeout_seconds) + 1) % len(presets)]
                                            if self.climax_timeout_seconds in presets else 60.0)
        except KeyboardInterrupt:
            self.shutdown()
 
    def shutdown(self):
        print("\n[MAC MINI] Shutting down...")
        self._stop = True
        try:
            if self.audio_lib is not None:
                self.audio_lib.stop()
                print("[AUDIO] AudioProcessorLib stopped.")
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