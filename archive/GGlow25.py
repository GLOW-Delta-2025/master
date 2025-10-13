import serial
import threading
import time

# -------------------------------
# Configuration
# -------------------------------
SERIAL_PORT = "/dev/tty.usbmodem14301"  # Replace with your actual Arduino port
BAUD_RATE = 115200

# -------------------------------
# Message Helpers
# -------------------------------
def format_message(device, msg_type, command, info_dict=None):
    """Format outgoing messages according to !!DEVICE:TYPE:COMMAND:{info}##"""
    info_str = ""
    if info_dict:
        info_str = ",".join([f"{k}={v}" for k, v in info_dict.items()])
    return f"!!{device}:{msg_type}:{command}:{{{info_str}}}##"

def parse_message(message):
    """Parse incoming structured messages into device, type, command, info dict."""
    try:
        # Remove trailing hashes
        message = message.strip("#").strip()
        # Split header and body
        header, body = message.split(":{")
        body = body.rstrip("}")
        device, msg_type, command = header.strip("!").split(":")
        info = {}
        if body:
            for pair in body.split(","):
                if "=" in pair:
                    k, v = pair.split("=")
                    info[k.strip()] = v.strip()
        return device, msg_type, command, info
    except Exception as e:
        raise ValueError(f"Invalid message format: {message} | {e}")

# -------------------------------
# Serial Communication
# -------------------------------
def send_to_arduino(ser, message):
    """Send message to Arduino."""
    print(f"[TX] {message}")
    ser.write((message + "\n").encode())

def handle_incoming_message(message):
    """Handle and print incoming messages from Arduino."""
    message = message.strip()
    if not message:
        return
    if not message.startswith("!!"):
        print(f"[RAW] {message}")  # Unstructured Arduino output
        return

    try:
        device, msg_type, command, info = parse_message(message)
        print(f"[RX] From {device}: Type={msg_type}, Command={command}, Info={info}")

        # Optional: auto-confirm REQUEST messages
        if msg_type == "REQUEST":
            confirm_msg = format_message(device, "CONFIRM", command, info)
            send_to_arduino(ser, confirm_msg)
    except ValueError as e:
        print(f"Parse error: {e}")

def read_from_arduino(ser):
    """Continuously read messages from Arduino."""
    buffer = ""
    while True:
        if ser.in_waiting > 0:
            buffer += ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
            while "##" in buffer:
                msg, buffer = buffer.split("##", 1)
                handle_incoming_message(msg + "##")
        time.sleep(0.01)

# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        print(f"Connected to {SERIAL_PORT} at {BAUD_RATE} baud")
    except Exception as e:
        print(f"Error opening serial port: {e}")
        exit(1)

    # Start background thread to read Arduino messages
    rx_thread = threading.Thread(target=read_from_arduino, args=(ser,), daemon=True)
    rx_thread.start()

    try:
        while True:
            cmd = input("Enter command (e.g., 'ARM1 MIC_ON' or 'ARM1 SEND_STAR', 'exit' to quit): ").strip()
            if cmd.lower() == "exit":
                break

            elif cmd.startswith("ARM1 SEND_STAR"):
                msg = format_message("ARM1", "REQUEST", "SEND_STAR", {
                    "speed": 4,
                    "color": "white",
                    "brightness": 70,
                    "size": 6
                })
                send_to_arduino(ser, msg)

            elif cmd.startswith("ARM1 MIC_ON"):
                msg = format_message("ARM1", "REQUEST", "MIC_ON")
                send_to_arduino(ser, msg)

            elif cmd.startswith("ARM1 MIC_OFF"):
                # Example with duration parameter
                msg = format_message("ARM1", "REQUEST", "MIC_OFF", {"duration": 1420})
                send_to_arduino(ser, msg)

            else:
                print("Unknown command format. Try: 'ARM1 MIC_ON' or 'ARM1 SEND_STAR'")

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("Exiting...")

    finally:
        ser.close()
        print("Serial connection closed.")
