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
    * Send BUILDUP_CLIMAX_CENTER (retry/confirm).
    * Wait for CLIMAX_READY from CENTER, then send START_CLIMAX_CENTER & START_CLIMAX_TOP.
    * Arms are disabled during buildup/climax.
    * Wait for BOTH CLIMAX_DONE_CENTER and CLIMAX_DONE_TOP, then reset the whole show state.

Robustness:
- Generic confirm/retry engine for ALL requests (with MAX_SEND_RETRIES).
- WARN if STAR_ARRIVED takes too long; ERROR & reset if it never comes.
- Idempotent handlers (ignore duplicates/late messages safely).
"""

import time
import threading
import serial
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, List, Dict, Tuple as Tup

# ---------------- CONFIG ----------------
SERIAL_PORT = "/dev/tty.usbmodem14301"
SERIAL_BAUD = 115200

NUM_ARMS = 5
MAX_STARS_FOR_CLIMAX = 10

PEAK_TIMEOUT = 10.0         # seconds without peaks before auto-send
STAR_SEND_TIME = 20.0       # seconds from MAKE_STAR confirm before auto-send
MAX_BRIGHTNESS = 255
UPDATE_STEP = 50            # brightness bump per peak

ACK_TIMEOUT = 0.75          # wait for CONFIRM before retrying any command
MAX_SEND_RETRIES = 3        # max attempts for any command
ARRIVAL_WARN_AFTER = 8.0    # warn if no STAR_ARRIVED after this many seconds
ARRIVAL_TIMEOUT = 15.0      # give up on STAR_ARRIVED after this many seconds and reset arm
WARN_THROTTLE = 2.0         # throttle warnings to at most once per arm per 2 seconds

# Logging
DEBUG_FRAMES = False       # set True to see ignored frames/noise
VERSION = "2025-10-22-CLIMAX-RESET-FIX-RLOCK"
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
    BUILDUP_CLIMAX_CENTER = "BUILDUP_CLIMAX_CENTER"  # Mac → CENTER
    CLIMAX_READY = "CLIMAX_READY"                    # CENTER → Mac (REQUEST)
    START_CLIMAX_CENTER = "START_CLIMAX_CENTER"      # Mac → CENTER
    START_CLIMAX_TOP = "START_CLIMAX_TOP"            # Mac → TOP
    CLIMAX_DONE_CENTER = "CLIMAX_DONE_CENTER"        # CENTER → Mac (REQUEST)
    CLIMAX_DONE_TOP = "CLIMAX_DONE_TOP"              # TOP → Mac (REQUEST)

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
    brightness: Optional[int] = None

    def to_command(self) -> str:
        # We send: "!!DEVICE:REQUEST:<CMD>{BRIGHTNESS?}##"
        cmd = f"!!{self.target_device.value}:REQUEST:{self.request_type.value}"
        if self.brightness is not None:
            cmd += f"{{{self.brightness}}}"
        cmd += "##"
        return cmd

class Star:
    def __init__(self, arm: DeviceType):
        self.arm = arm
        self.state = StarState.IDLE
        self.active = False
               # brightness set by peaks
        self.brightness = 0
        self.start_time: Optional[float] = None
        self.last_peak_time: Optional[float] = None

        # SEND_STAR handshake
        self.retry_count = 0
        self.last_send_attempt: Optional[float] = None
        self.awaiting_ack = False        # waiting for CONFIRM:SEND_STAR
        self.awaiting_arrival = False    # waiting for REQUEST:STAR_ARRIVED
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

PendingKey = Tup[str, str]  # (device.value, request_type.value)

class MacMiniController:
    def __init__(self):
        print("[BOOT] Running file:", __file__, "version:", VERSION)
        self.serial = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.1)
        time.sleep(2)
        print(f"[SERIAL] Connected to Teensy on {SERIAL_PORT}")

        self.arms = {arm: Star(arm) for arm in ARMS}

        # >>> RLock to avoid deadlocks when helpers call helpers under the same lock
        self.lock = threading.RLock()

        self.stars_collected = 0

        # Climax control
        self.climax_state = ClimaxState.IDLE
        self.climax_done_center = False
        self.climax_done_top = False

        self._stop = False

        # pending confirmations for all outgoing requests
        self.pending_confirms: Dict[PendingKey, RequestTracker] = {}

        threading.Thread(target=self.receive_loop, daemon=True).start()

        print("[MAC MINI] Initialized - Manual 5 ARM control ready")
        print("[TEST] Press keys 1–5 to trigger peaks for respective arms. 'R' to manual-reset. Ctrl+C to exit.")

    # ---------------- SEND COMMANDS ----------------
    def send_command(self, msg: Message):
        # Safety rail: Mac must never send CLIMAX_READY
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

        # Register/refresh tracker for this REQUEST
        key: PendingKey = (msg.target_device.value, msg.request_type.value)
        now = time.time()
        prev = self.pending_confirms.get(key)
        retries = prev.retries if prev else 0
        self.pending_confirms[key] = RequestTracker(
            device=msg.target_device,
            cmd=msg.request_type,
            last_sent=now,
            retries=retries,
            brightness=msg.brightness
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
        """
        Require: src:dest:verb:command{[payload]?}
        Returns (src, dest, verb, cmd, payload) or None if invalid.
        Examples:
          !!ARM3:MASTER:CONFIRM:SEND_STAR##
          !!ARM3:MASTER:REQUEST:STAR_ARRIVED##
        """
        s = frame.strip()
        if not (s.startswith("!!") and s.endswith("##")):
            return None

        # Strip delimiters
        core = s[2:-2]

        # Remove control whitespace that can appear mid-token (serial wrap)
        core = core.replace("\r", "").replace("\n", "").replace("\t", "")

        # Split and trim parts
        parts = [p.strip() for p in core.split(":")]
        if len(parts) < 4:
            return None

        src_raw, dest_raw, verb_raw = parts[0], parts[1], parts[2]
        cmd_and_payload = ":".join(parts[3:]).strip()

        # Extract command and optional payload
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

        # Normalize
        verb = verb_raw.replace(" ", "").upper()
        cmd = cmd_raw.replace(" ", "").upper()

        # Preserve addresses
        src = f"[{src_raw}]"
        dest = f"[{dest_raw}]"

        return src, dest, verb, cmd, payload

    @staticmethod
    def _norm_addr(token: Optional[str]) -> Optional[str]:
        if token is None:
            return None
        t = token.strip()
        if t.startswith("") and t.endswith(""):
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

        # Map src into a DeviceType if possible
        try:
            dev = DeviceType[src]
        except Exception:
            if DEBUG_FRAMES:
                print(f"[RECEIVED] (ignored unknown device) {src}")
            return

        # ---- MAKE_STAR ACK (starts the star build timers) ----
        if verb == "CONFIRM" and cmd == RequestType.MAKE_STAR.value and dev.name.startswith("ARM"):
            with self.lock:
                star = self.arms[dev]
                # clear pending for this confirm
                self.pending_confirms.pop((dev.value, RequestType.MAKE_STAR.value), None)
                if star.state == StarState.WAIT_CONFIRM:
                    now = time.time()
                    star.state = StarState.ACTIVE
                    star.start_time = now     # start timer only after confirm
                    star.last_peak_time = now
                    print(f"[ACK] {dev.value} confirmed MAKE_STAR; now ACTIVE")
            return

        # ---- Generic CONFIRM handling (stop retries) ----
        if verb == "CONFIRM":
            self.pending_confirms.pop((dev.value, cmd), None)

            # SEND_STAR confirmation advances the arm state
            if cmd == RequestType.SEND_STAR.value and dev.name.startswith("ARM"):
                self._on_confirm_send_star(dev)
            return

        # ---- STAR_ARRIVED from an ARM ----
        if verb == "REQUEST" and cmd == RequestType.STAR_ARRIVED.value and dev.name.startswith("ARM"):
            self._on_star_arrived(dev)
            return

        # ---- CLIMAX_READY from CENTER ----
        if verb == "REQUEST" and cmd == RequestType.CLIMAX_READY.value and dev == DeviceType.CENTER:
            with self.lock:
                if self.climax_state == ClimaxState.BUILDUP_WAIT_READY:
                    # Fire both START commands (retry/confirm handled generically)
                    self.send_command(Message(RequestType.START_CLIMAX_CENTER, DeviceType.CENTER))
                    self.send_command(Message(RequestType.START_CLIMAX_TOP, DeviceType.TOP))
                    self.climax_state = ClimaxState.RUNNING
                    print("[CLIMAX] CLIMAX_READY received → START_CLIMAX_CENTER & START_CLIMAX_TOP sent.")
            return

        # ---- CLIMAX_DONE_CENTER from CENTER ----
        if verb == "REQUEST" and cmd == RequestType.CLIMAX_DONE_CENTER.value and dev == DeviceType.CENTER:
            with self.lock:
                self.climax_done_center = True
                print("[CLIMAX] CENTER reports CLIMAX_DONE_CENTER.")
                self._check_climax_completion()
            return

        # ---- CLIMAX_DONE_TOP from TOP ----
        if verb == "REQUEST" and cmd == RequestType.CLIMAX_DONE_TOP.value and dev == DeviceType.TOP:
            with self.lock:
                self.climax_done_top = True
                print("[CLIMAX] TOP reports CLIMAX_DONE_TOP.")
                self._check_climax_completion()
            return

        # Silently ignore others
        if DEBUG_FRAMES:
            print(f"[RECEIVED] (ignored) {src}->{dest} {verb}:{cmd}")

    # ---------------- CLIMAX COMPLETION CHECK ----------------
    def _check_climax_completion(self):
        """Reset the show only when both CENTER and TOP report done."""
        both = self.climax_done_center and self.climax_done_top
        if both:
            print("[CLIMAX] Both CENTER and TOP done → resetting show state…")
            self._reset_show_cycle()
            # Snapshot after reset so we know peaks should work
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
            # else: duplicate/late confirm — ignore

    def _on_star_arrived(self, arm: DeviceType):
        with self.lock:
            star = self.arms[arm]
            if star.state in (StarState.IN_ANIMATION, StarState.DISPATCHING):
                star.awaiting_arrival = False

                if self.climax_state == ClimaxState.IDLE:
                    # Normal counting + visuals only while not in buildup/climax
                    self.stars_collected += 1
                    print(f"[ARRIVED] {arm.value} animation complete. Total stars: {self.stars_collected}")

                    self.send_command(Message(RequestType.ADD_STAR_TOP, DeviceType.TOP))
                    self.send_command(Message(RequestType.ADD_STAR_CENTER, DeviceType.CENTER))

                    # Trigger pre-climax when threshold reached
                    if self.stars_collected >= MAX_STARS_FOR_CLIMAX:
                        self.send_command(Message(RequestType.BUILDUP_CLIMAX_CENTER, DeviceType.CENTER))
                        self.climax_state = ClimaxState.BUILDUP_WAIT_ACK
                        print(f"[CLIMAX] Threshold {self.stars_collected}/{MAX_STARS_FOR_CLIMAX} reached → BUILDUP_CLIMAX_CENTER.")
                else:
                    # During buildup/climax: do not count or add visuals
                    print(f"[ARRIVED] {arm.value} (ignored for count; climax phase active)")

                self._reset_arm(star)
            # else: late/stray arrival — ignore

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
        """Reset all state so a new cycle can begin immediately."""
        with self.lock:
            # Reset all arms
            for star in self.arms.values():
                self._reset_arm(star)
            # Clear pending confirmations (cancel any retries still in-flight)
            self.pending_confirms.clear()
            # Reset counters and climax state and latches
            self.stars_collected = 0
            self.climax_state = ClimaxState.IDLE
            self.climax_done_center = False
            self.climax_done_top = False

    # ---------------- PEAK TRIGGER ----------------
    def trigger_peak(self, arm_num: int):
        arm = DeviceType[f"ARM{arm_num}"]
        with self.lock:
            # During buildup/climax we ignore peaks
            if self.climax_state in (ClimaxState.BUILDUP_WAIT_ACK, ClimaxState.BUILDUP_WAIT_READY, ClimaxState.RUNNING):
                print(f"[PEAK] Ignored: climax phase is active ({self.climax_state.name})")
                return

            star = self.arms[arm]
            now = time.time()

            if star.state == StarState.IDLE:
                # Start new star: wait for MAKE_STAR confirm before allowing updates
                star.state = StarState.WAIT_CONFIRM
                star.active = True
                star.brightness = UPDATE_STEP
                star.start_time = None           # timers start on confirm
                star.last_peak_time = None
                star.retry_count = 0
                star.awaiting_ack = False
                star.awaiting_arrival = False
                self.send_command(Message(RequestType.MAKE_STAR, arm, star.brightness))
                print(f"[PEAK] New star on {arm.value} (brightness={star.brightness})")

            elif star.state == StarState.WAIT_CONFIRM:
                print(f"[PEAK] Ignored: {arm.value} waiting for MAKE_STAR confirm")

            elif star.state == StarState.ACTIVE:
                # Guard: block updates if MAKE_STAR is still pending confirm
                if (arm.value, RequestType.MAKE_STAR.value) in self.pending_confirms:
                    print(f"[PEAK] Ignored: {arm.value} MAKE_STAR not yet confirmed (pending ACK)")
                    return

                # Update existing star
                star.brightness = min(star.brightness + UPDATE_STEP, MAX_BRIGHTNESS)
                star.last_peak_time = now
                self.send_command(Message(RequestType.UPDATE_STAR, arm, star.brightness))
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
                            # Increment retry count and re-send same command with same brightness
                            self.pending_confirms[key].retries += 1
                            self.pending_confirms[key].last_sent = now
                            self.send_command(Message(tracker.cmd, tracker.device, tracker.brightness))
                        else:
                            # Give up on this command
                            del self.pending_confirms[key]
                            print(f"[ERROR] {tracker.device.value} no CONFIRM after {MAX_SEND_RETRIES} {tracker.cmd.value} attempts.")
                            # If SEND_STAR fails for an arm, reset that arm so it doesn't get stuck
                            if tracker.cmd == RequestType.SEND_STAR and tracker.device.name.startswith("ARM"):
                                with self.lock:
                                    self._reset_arm(self.arms[tracker.device])

                # Handle star lifecycle & auto-dispatch & timeouts
                send_queue: List[Star] = []
                with self.lock:
                    for star in self.arms.values():
                        if star.state == StarState.ACTIVE:
                            # Auto-dispatch rules after MAKE_STAR confirmed
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

                # Climax state transitions driven by confirms/requests
                # When BUILDUP_CLIMAX_CENTER is confirmed, move to wait for CLIMAX_READY
                if self.climax_state == ClimaxState.BUILDUP_WAIT_ACK:
                    if (DeviceType.CENTER.value, RequestType.BUILDUP_CLIMAX_CENTER.value) not in self.pending_confirms:
                        self.climax_state = ClimaxState.BUILDUP_WAIT_READY
                        print("[CLIMAX] BUILDUP_CLIMAX_CENTER confirmed → waiting for CLIMAX_READY from CENTER.")

                time.sleep(0.1)
        except KeyboardInterrupt:
            self.shutdown()

    def _try_send_star(self, star: Star):
        # Send SEND_STAR via the same command path (tracked/retried generically)
        self.send_command(Message(RequestType.SEND_STAR, star.arm, star.brightness))
        star.last_send_attempt = time.time()
        star.retry_count += 1

    # ---------------- MANUAL INPUT LOOP ----------------
    def input_loop(self):
        import sys, termios, tty, select
        def get_key():
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                i, _, _ = select.select([sys.stdin], [], [], 0.1)
                if i:
                    return sys.stdin.read(1)
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
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        print("\n[MAC MINI] Shutting down...")
        self._stop = True
        try:
            self.serial.close()
        except Exception:
            pass

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    controller = MacMiniController()
    threading.Thread(target=controller.run, daemon=True).start()
    controller.input_loop()
