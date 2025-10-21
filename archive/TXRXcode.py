import serial
import threading
import time

SERIAL_PORT = "/dev/tty.usbmodem14301"  # adjust for your setup
BAUD_RATE = 115200

ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)

def format_message(device, command_type, command, params=None):
    param_str = ""
    if params:
        param_str = "{" + ",".join(f"{k}={v}" for k, v in params.items()) + "}"
    else:
        param_str = "{}"
    return f"!!{device}:{command_type}:{command}{param_str}##"

def listen_serial():
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if line:
            print(f"[RX] {line}")

threading.Thread(target=listen_serial, daemon=True).start()

print("Glow Command Console (type 'exit' to quit)")
while True:
    cmd = input(">> ").strip()
    if cmd.lower() == "exit":
        break

    if cmd.startswith(">>"):
        cmd = cmd[2:].strip()

    parts = cmd.split()
    if len(parts) < 2:
        print("Usage: <device> <command> [param=value ...]")
        continue

    device, command = parts[0], parts[1]
    params = {}

    for p in parts[2:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k] = v

    msg = format_message(device, "REQUEST", command, params)
    print(f"[TX] {msg}")
    ser.write((msg + "\n").encode())
    time.sleep(0.1)
