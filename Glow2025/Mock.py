import serial
import threading
import queue
import time

# ----------------------------
# CONFIGURATION
# ----------------------------
SERIAL_PORT = "/dev/tty.usbmodem14301"  # Teensy serial port
BAUD_RATE = 115200
TIMEOUT = 0.1

running = True

# Devices we want to simulate
MOCK_DEVICES = ["ARM1", "CENTER", "TOP"]
device_queues = {dev: queue.Queue() for dev in MOCK_DEVICES}

# ----------------------------
# MESSAGE HELPERS
# ----------------------------
def format_message(device, msg_type, command, info=None):
    info_str = ""
    if info:
        info_str = "{" + ",".join(f"{k}={v}" for k, v in info.items()) + "}"
    else:
        info_str = "{}"
    return f"!!{device}:{msg_type}:{command}:{info_str}##"

def parse_message(msg):
    msg = msg.strip()
    if not msg.startswith("!!") or not msg.endswith("##"):
        return None
    core = msg[2:-2]
    parts = core.split(":", 3)
    if len(parts) < 3:
        return None
    device, msg_type, command = parts[:3]
    info = {}
    if len(parts) == 4 and parts[3].startswith("{") and parts[3].endswith("}"):
        items = parts[3][1:-1].split(",")
        for item in items:
            if "=" in item:
                k, v = item.split("=", 1)
                info[k.strip()] = v.strip()
    return {"device": device, "type": msg_type, "command": command, "info": info}

def send_serial(ser, msg):
    ser.write((msg + "\n").encode("utf-8"))
    print(f"[TX -> Teensy] {msg}")

# ----------------------------
# MOCK DEVICE THREADS
# ----------------------------
def mock_device_handler(device, ser):
    """Listen for messages in device queue and optionally send responses"""
    while running:
        try:
            msg = device_queues[device].get(timeout=0.1)
            parsed = parse_message(msg)
            if not parsed:
                continue

            print(f"[{device} RECEIVED] {msg}")
            # Ask user if they want to send a confirmation
            response = input(f"Send CONFIRM for {device} {parsed['command']}? (y/n): ").strip().lower()
            if response == "y":
                confirm_msg = format_message("MASTER", "CONFIRM", parsed["command"], parsed["info"])
                send_serial(ser, confirm_msg)
            else:
                error_msg = format_message("MASTER", "REQUEST", "ERROR", {"info": f"user_rejected_{parsed['command']}"})
                send_serial(ser, error_msg)
        except queue.Empty:
            continue

# ----------------------------
# SERIAL LISTENER THREAD
# ----------------------------
def serial_listener(ser):
    while running:
        try:
            if ser.in_waiting:
                raw = ser.readline().decode("utf-8", errors="ignore").strip()
                if not raw:
                    continue
                print(f"[RX <- Teensy] {raw}")
                parsed = parse_message(raw)
                if not parsed:
                    continue
                target = parsed["device"]
                # Route to mock device if in MOCK_DEVICES
                if target in MOCK_DEVICES:
                    device_queues[target].put(raw)
                else:
                    print(f"[ROUTER] Unknown target '{target}', sending ERROR")
                    error_msg = format_message("MAC", "REQUEST", "ERROR", {"info": f"unknown_target_{target}"})
                    send_serial(ser, error_msg)
        except Exception as e:
            print(f"[Serial listener error] {e}")
        time.sleep(0.05)

# ----------------------------
# MANUAL COMMAND CONSOLE
# ----------------------------
def manual_console(ser):
    print("=== MOCK DEVICE CONTROL CONSOLE ===")
    print("Commands: <DEVICE> <COMMAND> [param=value ...], 'exit' to quit")
    while True:
        cmd = input(">> ").strip()
        if cmd.lower() == "exit":
            global running
            running = False
            break
        if not cmd:
            continue

        tokens = cmd.split()
        if len(tokens) < 2:
            print("Usage: <DEVICE> <COMMAND> [param=value ...]")
            continue

        device, command = tokens[0], tokens[1]
        info = {}
        for param in tokens[2:]:
            if "=" in param:
                k, v = param.split("=", 1)
                info[k] = v

        msg = format_message(device, "REQUEST", command, info)
        send_serial(ser, msg)

# ----------------------------
# MAIN
# ----------------------------
def main():
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=TIMEOUT)
        print(f"Connected to Teensy on {SERIAL_PORT} at {BAUD_RATE} baud.")
    except Exception as e:
        print(f"Failed to open serial port: {e}")
        return

    # Start mock device threads
    for dev in MOCK_DEVICES:
        t = threading.Thread(target=mock_device_handler, args=(dev, ser), daemon=True)
        t.start()

    # Start serial listener thread
    listener_thread = threading.Thread(target=serial_listener, args=(ser,), daemon=True)
    listener_thread.start()

    # Start manual console for sending requests
    manual_console(ser)

    ser.close()
    print("Mock simulator shutting down.")

if __name__ == "__main__":
    main()
