#!/usr/bin/env python3
"""
GLOW 25' - Mac Mini Control System (Five Arms, Router-Aware, Robust)
Audio-reactive lighting control with 5 arms and Teensy router.

Now integrated with audiolib for microphone peak detection → arm triggering.

Configuration is loaded from config.json, with sensible defaults if missing.
"""

import time
import threading
import serial
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, List, Dict

from lib.audiolib import AudioProcessorLib
from lib.config import get_config

# Load configuration
_config = get_config()

# Extract audio config from the loaded config file
AUDIO_DEVICE = int(_config.get('device', 6))
AUDIO_CHANNELS = tuple(_config.get('channels', (0, 1, 2, 3, 4, 5)))
AUDIO_POLL_INTERVAL = float(_config.get('poll_interval', 0.50))
AUDIO_THRESHOLD_DB = float(_config.get('spike_threshold_db', 10))

# Audio channel to arm mapping (customize as needed)
AUDIO_CHANNEL_TO_ARM = {
    AUDIO_CHANNELS[0]: 1 if len(AUDIO_CHANNELS) > 0 else None,
    AUDIO_CHANNELS[1]: 2 if len(AUDIO_CHANNELS) > 1 else None,
    AUDIO_CHANNELS[2]: 3 if len(AUDIO_CHANNELS) > 1 else None,
    AUDIO_CHANNELS[3]: 4 if len(AUDIO_CHANNELS) > 1 else None,
    AUDIO_CHANNELS[4]: 5 if len(AUDIO_CHANNELS) > 1 else None,
}
# Remove None entries
AUDIO_CHANNEL_TO_ARM = {k: v for k, v in AUDIO_CHANNEL_TO_ARM.items() if v is not None}

# Static configuration
SERIAL_PORT = "/dev/pts/2"
SERIAL_BAUD = 115200

NUM_ARMS = 5
MAX_STARS_FOR_CLIMAX = 10

PEAK_TIMEOUT = 10.0  # seconds without peaks before auto-send
STAR_SEND_TIME = 20.0  # seconds from MAKE_STAR confirm before auto-send
MAX_BRIGHTNESS = 255
UPDATE_STEP = 50  # brightness bump per peak

ACK_TIMEOUT = 0.75  # wait for CONFIRM before retrying any command
MAX_SEND_RETRIES = 3  # max attempts for any command
ARRIVAL_WARN_AFTER = 8.0  # warn if no STAR_ARRIVED after this many seconds
ARRIVAL_TIMEOUT = 15.0  # give up on STAR_ARRIVED after this many seconds and reset arm
WARN_THROTTLE = 2.0  # throttle warnings to at most once per arm per 2 seconds

# Keep-alive
DEFAULT_KEEPALIVE_INTERVAL = 60.0  # seconds; adjustable live
HEALTH_FAIL_THRESHOLD = 3  # consecutive failed pings to mark offline

# Logging
DEBUG_FRAMES = False  # set True to see ignored frames/noise
VERSION = "2025-10-23-AUDIO-REACTIVE-CONFIG-DRIVEN"


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
    BUILDUP_CLIMAX_CENTER = "BUILDUP_CLIMAX_CENTER"
    CLIMAX_READY = "CLIMAX_READY"
    START_CLIMAX_CENTER = "START_CLIMAX_CENTER"
    START_CLIMAX_TOP = "START_CLIMAX_TOP"
    CLIMAX_DONE_CENTER = "CLIMAX_DONE_CENTER"
    CLIMAX_DONE_TOP = "CLIMAX_DONE_TOP"

    # Keep-alive
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

    def to_command(self) -> str:
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
        self.brightness = 0
        self.start_time: Optional[float] = None
        self.last_peak_time: Optional[float] = None

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


PendingKey = Tuple[str, str]


class MacMiniController:
    def __init__(self):
        print("[BOOT] Running file:", __file__, "version:", VERSION)
        print(
            f"[CONFIG] Loaded from config.json: device={AUDIO_DEVICE}, channels={AUDIO_CHANNELS}, threshold={AUDIO_THRESHOLD_DB}dB, poll_interval={AUDIO_POLL_INTERVAL}s")

        self.serial = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.1)
        time.sleep(2)
        print(f"[SERIAL] Connected to Teensy on {SERIAL_PORT}")

        self.arms = {arm: Star(arm) for arm in ARMS}
        self.lock = threading.RLock()
        self.stars_collected = 0
        self.climax_state = ClimaxState.IDLE
        self.climax_done_center = False
        self.climax_done_top = False
        self._stop = False
        self.pending_confirms: Dict[PendingKey, RequestTracker] = {}

        now = time.time()
        self.health: Dict[DeviceType, Dict[str, object]] = {
            dev: {"online": True, "failures": 0, "last_seen": now}
            for dev in ALL_DEVICES
        }

        self.keepalive_interval = DEFAULT_KEEPALIVE_INTERVAL
        self._next_keepalive_due = time.time() + self.keepalive_interval

        # Initialize audio processor with config-driven settings
        self.audio_lib = AudioProcessorLib(
            device=AUDIO_DEVICE,
            channels=AUDIO_CHANNELS,
            poll_interval=AUDIO_POLL_INTERVAL,
            start_stream=True
        )
        self.audio_lib.config(threshold=AUDIO_THRESHOLD_DB)
        self.audio_lib.register_spike_callback(self._on_audio_spike)
        self.audio_lib.start()
        print(
            f"[AUDIO] Initialized on device {AUDIO_DEVICE}, channels {AUDIO_CHANNELS}, threshold {AUDIO_THRESHOLD_DB}dB, poll_interval {AUDIO_POLL_INTERVAL}s")
        print(f"[AUDIO] Channel-to-ARM mapping: {AUDIO_CHANNEL_TO_ARM}")

        threading.Thread(target=self.receive_loop, daemon=True).start()

        print("[MAC MINI] Initialized - Audio-reactive 5 ARM control ready")
        print("[AUDIO] Peaks on configured channels will trigger arm animations")
        print("[TEST] Manual: 'k' cycle keep-alive · 'h' health · 'R' reset · Ctrl+C exit.")

    # -------------- AUDIO SPIKE CALLBACK ---------------
    def _on_audio_spike(self, channel: int, spike_obj, avg_db, noise_db):
        """Callback from audiolib when a peak is detected."""
        arm_num = AUDIO_CHANNEL_TO_ARM.get(channel)
        if arm_num is not None:
            print(f"[SPIKE] Channel {channel} → ARM{arm_num} (avg={avg_db:.1f}dB, noise={noise_db:.1f}dB)")
            self.trigger_peak(arm_num)
        else:
            if DEBUG_FRAMES:
                print(f"[SPIKE] Channel {channel} (unmapped) avg={avg_db:.1f}dB, noise={noise_db:.1f}dB")

    # [Rest of methods remain the same as in your original file]
    # (Includes: send_command, receive_loop, _parse_frame, _norm_addr, handle_received,
    #  _check_climax_completion, _on_confirm_send_star, _on_star_arrived, _reset_arm,
    #  _reset_show_cycle, _send_keepalives_if_due, set_keepalive_interval, _handle_ping_failure,
    #  _mark_device_seen, _print_health, trigger_peak, run, _try_send_star, input_loop, shutdown)

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
            brightness=msg.brightness
        )

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
                            buffer = buffer[start + 2:]
                            continue
                        frame = buffer[start:end]
                        buffer = buffer[end:]
                        self.handle_received(frame)
                else:
                    time.sleep(0.01)
            except Exception as e:
                print(f"[ERROR] Receive loop: {e}")
                time.sleep(0.05)

    @staticmethod
    def _parse_frame(frame: str) -> Optional[Tuple[str, str, str, str, Optional[str]]]:
        s = frame.strip()
        if not (s.startswith("!!") and s.endswith("##")):
            return None

        core = s[2:-2].replace("\r", "").replace("\n", "").replace("\t", "")
        parts = [p.strip() for p in core.split(":")]
        if len(parts) < 4:
            return None

        src_raw, dest_raw, verb_raw = parts[0], parts[1], parts[2]
        cmd_and_payload = ":".join(parts[3:]).strip()

        payload = None
        lb = cmd_and_payload.find("{")
        if lb != -1:
            cmd_raw = cmd_and_payload[:lb].strip()
            right = cmd_and_payload[lb + 1:]
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
                    print("[CLIMAX] CLIMAX_READY received → START_CLIMAX_CENTER & START_CLIMAX_TOP sent.")
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

    def _check_climax_completion(self):
        both = self.climax_done_center and self.climax_done_top
        if both:
            print("[CLIMAX] Both CENTER and TOP done → resetting show state…")
            self._reset_show_cycle()
            print(
                f"[RESET] stars_collected={self.stars_collected} climax_state={self.climax_state.name} pending_confirms={len(self.pending_confirms)}")
            print("[CLIMAX] Show state reset (ready for new cycle).")
        else:
            waiting = []
            if not self.climax_done_center:
                waiting.append("CENTER")
            if not self.climax_done_top:
                waiting.append("TOP")
            print(f"[CLIMAX] Waiting for: {', '.join(waiting)}…")

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

                    self.send_command(Message(RequestType.ADD_STAR_TOP, DeviceType.TOP))
                    self.send_command(Message(RequestType.ADD_STAR_CENTER, DeviceType.CENTER))

                    if self.stars_collected >= MAX_STARS_FOR_CLIMAX:
                        self.send_command(Message(RequestType.BUILDUP_CLIMAX_CENTER, DeviceType.CENTER))
                        self.climax_state = ClimaxState.BUILDUP_WAIT_ACK
                        print(
                            f"[CLIMAX] Threshold {self.stars_collected}/{MAX_STARS_FOR_CLIMAX} reached → BUILDUP_CLIMAX_CENTER.")
                else:
                    print(f"[ARRIVED] {arm.value} (ignored for count; climax phase active)")

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
            for star in self.arms.values():
                self._reset_arm(star)
            self.pending_confirms.clear()
            self.stars_collected = 0
            self.climax_state = ClimaxState.IDLE
            self.climax_done_center = False
            self.climax_done_top = False

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

    def _mark_device_seen(self, dev: DeviceType):
        if dev not in self.health:
            return
        self.health[dev]["online"] = True
        self.health[dev]["failures"] = 0
        self.health[dev]["last_seen"] = time.time()

    def _print_health(self):
        print("\n[HEALTH] Device status:")
        for dev in ALL_DEVICES:
            h = self.health[dev]
            age = time.time() - float(h["last_seen"])
            online = "ONLINE " if h["online"] else "OFFLINE"
            print(f"  - {dev.value:6s}  {online}  failures={h['failures']}  last_seen={age:4.1f}s ago")
        print("")

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
                star.retry_count = 0
                star.awaiting_ack = False
                star.awaiting_arrival = False
                self.send_command(Message(RequestType.MAKE_STAR, arm, star.brightness))
                print(f"[PEAK] New star on {arm.value} (brightness={star.brightness})")

            elif star.state == StarState.WAIT_CONFIRM:
                print(f"[PEAK] Ignored: {arm.value} waiting for MAKE_STAR confirm")

            elif star.state == StarState.ACTIVE:
                if (arm.value, RequestType.MAKE_STAR.value) in self.pending_confirms:
                    print(f"[PEAK] Ignored: {arm.value} MAKE_STAR not yet confirmed (pending ACK)")
                    return

                star.brightness = min(star.brightness + UPDATE_STEP, MAX_BRIGHTNESS)
                star.last_peak_time = now
                self.send_command(Message(RequestType.UPDATE_STAR, arm, star.brightness))
                print(f"[PEAK] Update {arm.value} -> brightness={star.brightness}")

            else:
                print(f"[PEAK] Ignored: {arm.value} is {star.state.name}")

    def run(self):
        try:
            while True:
                now = time.time()

                for key, tracker in list(self.pending_confirms.items()):
                    if (now - tracker.last_sent) >= ACK_TIMEOUT:
                        if tracker.retries < MAX_SEND_RETRIES:
                            self.pending_confirms[key].retries += 1
                            self.pending_confirms[key].last_sent = now
                            self.send_command(Message(tracker.cmd, tracker.device, tracker.brightness))
                        else:
                            del self.pending_confirms[key]
                            print(
                                f"[ERROR] {tracker.device.value} no CONFIRM after {MAX_SEND_RETRIES} {tracker.cmd.value} attempts.")
                            if tracker.cmd == RequestType.SEND_STAR and tracker.device.name.startswith("ARM"):
                                with self.lock:
                                    self._reset_arm(self.arms[tracker.device])
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

                        elif star.state == StarState.IN_ANIMATION:
                            if star.confirmed_at:
                                waited = now - star.confirmed_at
                                if waited > ARRIVAL_TIMEOUT:
                                    print(
                                        f"[ERROR] {star.arm.value} no STAR_ARRIVED after {ARRIVAL_TIMEOUT:.1f}s. Resetting arm.")
                                    self._reset_arm(star)
                                elif waited > ARRIVAL_WARN_AFTER:
                                    if (star.last_warn is None) or (now - star.last_warn >= WARN_THROTTLE):
                                        print(
                                            f"[WARN] {star.arm.value} STAR_ARRIVED taking long (> {ARRIVAL_WARN_AFTER:.1f}s, waited {waited:.1f}s)")
                                        star.last_warn = now

                for s in send_queue:
                    self._try_send_star(s)

                if self.climax_state == ClimaxState.BUILDUP_WAIT_ACK:
                    if (DeviceType.CENTER.value, RequestType.BUILDUP_CLIMAX_CENTER.value) not in self.pending_confirms:
                        self.climax_state = ClimaxState.BUILDUP_WAIT_READY
                        print("[CLIMAX] BUILDUP_CLIMAX_CENTER confirmed → waiting for CLIMAX_READY from CENTER.")

                time.sleep(0.1)
        except KeyboardInterrupt:
            self.shutdown()

    def _try_send_star(self, star: Star):
        self.send_command(Message(RequestType.SEND_STAR, star.arm, star.brightness))
        star.last_send_attempt = time.time()
        star.retry_count += 1

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
                elif key in ("k", "K"):
                    presets = [30.0, 60.0, 120.0]
                    try:
                        idx = presets.index(self.keepalive_interval)
                        self.set_keepalive_interval(presets[(idx + 1) % len(presets)])
                    except ValueError:
                        self.set_keepalive_interval(60.0)
                elif key in ("h", "H"):
                    self._print_health()
                elif key in ("a", "A"):
                    print("[AUDIO] Current threshold: {:.1f}dB".format(self.audio_lib.processor.spike_threshold_db))
                    print("[AUDIO] Enter new threshold (dB) or press Enter to cancel:")
                    try:
                        user_input = input().strip()
                        if user_input:
                            new_thresh = float(user_input)
                            self.audio_lib.config(threshold=new_thresh)
                            print(f"[AUDIO] Threshold updated to {new_thresh:.1f}dB")
                    except ValueError:
                        print("[AUDIO] Invalid input, keeping current threshold.")
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        print("\n[MAC MINI] Shutting down...")
        self._stop = True
        try:
            self.audio_lib.stop(stop_stream=True)
            print("[AUDIO] Audio processor stopped.")
        except Exception as e:
            print(f"[AUDIO] Error stopping audio: {e}")
        try:
            self.serial.close()
        except Exception:
            pass


if __name__ == "__main__":
    controller = MacMiniController()
    threading.Thread(target=controller.run, daemon=True).start()
    controller.input_loop()
