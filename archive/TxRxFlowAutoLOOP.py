import serial
import threading
import time
import re

# ---------------- Configuration ----------------
SERIAL_PORT = "/dev/tty.usbmodem14301"  # Adjust for your setup
BAUD_RATE = 115200

MAX_STARS = 10
BUILDUP_DURATION = 10   # seconds
CLIMAX_DURATION = 15    # seconds
AUTO_TEST_INTERVAL = 1  # seconds between simulated stars

ARM_DEVICES = ["ARM1", "ARM2", "ARM3", "ARM4", "ARM5"]

# ---------------- State ----------------
star_counter = 0
flow_active = False
waiting_for_star = False
waiting_for_buildup = False
waiting_for_climax = False

# ---------------- Serial ----------------
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)

# Regex for parsing messages
msg_regex = re.compile(r"!!(?P<device>[^:]+):(?P<source>[^:]+):(?P<type>[^:]+):(?P<command>[^{#]+)(?:\{(?P<params>[^}]*)\})?##")

# ---------------- Helper Functions ----------------
def format_message(device, command_type, command, params=None):
    param_str = ""
    if params:
        param_str = "{" + ",".join(f"{k}={v}" for k, v in params.items()) + "}"
    else:
        param_str = "{}"
    return f"!!{device}:{command_type}:{command}{param_str}##"

def send_command(device, command, params=None):
    msg = format_message(device, "REQUEST", command, params)
    print(f"[TX] {msg}")
    ser.write((msg + "\n").encode())
    time.sleep(0.05)

# ---------------- Event Handlers ----------------
def handle_star_arrived(params):
    global star_counter, waiting_for_star
    print(f"[EVENT] STAR_ARRIVED received: {params}")
    # Add star to CENTER and TOP
    send_command("CENTER", "ADD_STAR", params)
    send_command("TOP", "ADD_STAR", params)
    star_counter += 1
    waiting_for_star = False

    if star_counter >= MAX_STARS:
        print("Max stars reached. Initiating build-up...")
        threading.Thread(target=build_up_sequence, daemon=True).start()

def handle_climax_ready():
    print("[EVENT] CLIMAX_READY received. Starting climax...")
    threading.Thread(target=climax_sequence, daemon=True).start()

def build_up_sequence():
    global waiting_for_buildup
    waiting_for_buildup = True
    print(f"[FLOW] Build-up starting for {BUILDUP_DURATION}s...")
    send_command("CENTER", "BUILDUP_CLIMAX_CENTER", {"SPEED": 100})
    time.sleep(BUILDUP_DURATION)
    waiting_for_buildup = False

    # Send CLIMAX_READY request to MAC (Teensy will route back to us)
    send_command("MASTER", "CLIMAX_READY")

def climax_sequence():
    global waiting_for_climax
    waiting_for_climax = True
    print(f"[FLOW] Climax starting for {CLIMAX_DURATION}s...")
    send_command("CENTER", "START_CLIMAX_CENTER", {"TIME": CLIMAX_DURATION * 1000})
    send_command("TOP", "START_CLIMAX_TOP", {"TIME": CLIMAX_DURATION * 1000})
    time.sleep(CLIMAX_DURATION)
    waiting_for_climax = False

    print("[FLOW] Climax finished. Resetting system...")
    reset_flow()

def reset_flow():
    global star_counter, flow_active
    send_command("BROADCAST", "RESET")
    star_counter = 0
    flow_active = False
    print("[FLOW] System ready for next cycle.\n")

# ---------------- Serial Listener ----------------
def listen_serial():
    global waiting_for_star
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        print(f"[RX] {line}")

        # Parse messages with optional parameters
        match = msg_regex.match(line)
        if match:
            device = match.group("device")
            source = match.group("source")
            msg_type = match.group("type")
            command = match.group("command").strip()
            params_str = match.group("params")
            params = {}
            if params_str:
                for p in params_str.split(","):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        try:
                            params[k] = int(v)
                        except:
                            params[k] = v

            # Handle STAR_ARRIVED requests from Teensy (MASTER source)
            if msg_type.upper() == "REQUEST" and command.upper() == "STAR_ARRIVED" and source.upper() == "MASTER":
                handle_star_arrived(params)

            # Handle CLIMAX_READY request from Teensy
            elif msg_type.upper() == "REQUEST" and command.upper() == "CLIMAX_READY" and source.upper() == "MASTER":
                handle_climax_ready()

threading.Thread(target=listen_serial, daemon=True).start()

# ---------------- AUTO_TEST FLOW ----------------
def auto_test_loop():
    global flow_active, waiting_for_star
    flow_active = True
    while True:
        print("--- Starting new AUTO_TEST cycle ---")
        for i in range(MAX_STARS):
            arm = ARM_DEVICES[i % len(ARM_DEVICES)]
            # Simulate star creation
            params = {"SPEED": 50, "COLOR": 128, "BRIGHTNESS": 200, "SIZE": 5}
            send_command(arm, "MAKE_STAR", params)
            time.sleep(0.1)  # wait for Teensy confirm
            send_command(arm, "SEND_STAR")
            waiting_for_star = True
            # Wait until STAR_ARRIVED is handled
            while waiting_for_star:
                time.sleep(0.05)
            time.sleep(AUTO_TEST_INTERVAL)

        # Wait for build-up and climax to finish
        while waiting_for_buildup or waiting_for_climax:
            time.sleep(0.1)

# ---------------- Console ----------------
print("Glow Command Console (type 'exit' to quit)")
print("Commands: AUTO_TEST_LOOP (fully autonomous)")

while True:
    cmd = input(">> ").strip()
    if cmd.lower() == "exit":
        break

    if cmd == "AUTO_TEST_LOOP" and not flow_active:
        threading.Thread(target=auto_test_loop, daemon=True).start()
        print("[INFO] AUTO_TEST_LOOP started: system will run continuously.")
        continue

    print("[INFO] Unknown command. Type AUTO_TEST_LOOP to start or exit to quit.")
