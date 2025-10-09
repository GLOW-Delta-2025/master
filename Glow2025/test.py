import serial
import threading
import time
import json

# ----------------------------
# CONFIGURATION
# ----------------------------
SERIAL_PORT = "/dev/tty.usbmodem14201"  # update to your Teensy port
BAUD_RATE = 115200
TIMEOUT = 0.1

running = True


# ----------------------------
# MESSAGE HELPERS
# ----------------------------
def format_message(device, msg_type, command, info=None):
    """Format message according to protocol spec."""
    info_str = ""
    if info:
        info_str = "{" + ",".join(f"{k}={v}" for k, v in info.items()) + "}"
    return f"!!{device}:{msg_type}:{command}:{info_str}##"


def parse_message(msg):
    """Parse an incoming message according to protocol spec."""
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


def send_message(ser, msg):
    """Send a formatted message via serial."""
    ser.write((msg + "\n").encode("utf-8"))
    print(f"[TX] {msg}")


# ----------------------------
# SERIAL LISTENER
# ----------------------------
def listen_serial(ser):
    """Listen for incoming serial messages."""
    while running:
        try:
            if ser.in_waiting:
                raw = ser.readline().decode("utf-8", errors="ignore").strip()
                if not raw:
                    continue

                print(f"[RX] {raw}")
                parsed = parse_message(raw)
                if not parsed:
                    continue

                if parsed["type"] == "REQUEST":
                    confirm = format_message("MASTER", "CONFIRM", parsed["command"], parsed["info"])
                    send_message(ser, confirm)

        except Exception as e:
            print(f"[Serial error] {e}")
        time.sleep(0.05)


# ----------------------------
# MAIN
# ----------------------------
def main():
    global running
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=TIMEOUT)
        print(f"Connected to {SERIAL_PORT} at {BAUD_RATE} baud.")
    except Exception as e:
        print(f"Error opening serial port: {e}")
        return

    thread = threading.Thread(target=listen_serial, args=(ser,), daemon=True)
    thread.start()

    try:
        while True:
            cmd = input(">> ").strip()
            if cmd.lower() == "exit":
                running = False
                break
            if not cmd:
                continue

            tokens = cmd.split()
            if len(tokens) < 2:
                print("Format: <DEVICE> <COMMAND> [key=value ...]")
                continue

            device, command = tokens[0], tokens[1]
            info = {}
            for param in tokens[2:]:
                if "=" in param:
                    k, v = param.split("=", 1)
                    info[k] = v

            msg = format_message(device, "REQUEST", command, info)
            send_message(ser, msg)

    except KeyboardInterrupt:
        running = False
    finally:
        ser.close()
        print("Serial connection closed.")


if __name__ == "__main__":
    main()
