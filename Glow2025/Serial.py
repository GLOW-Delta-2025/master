import serial
import threading
import time

# -------------------------------
# Configuratie
# -------------------------------
SERIAL_PORT = "/dev/tty.usbmodem14301"  # vervang door jouw Arduino/Teensy poort
BAUD_RATE = 115200

# -------------------------------
# Lees thread
# -------------------------------
def read_from_serial(ser):
    buffer = ""
    while True:
        if ser.in_waiting > 0:
            buffer += ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                print(f"[RX] {line}")
        time.sleep(0.01)

# -------------------------------
# Hoofdprogramma
# -------------------------------
if __name__ == "__main__":
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        print(f"Verbonden met {SERIAL_PORT} op {BAUD_RATE} baud")
    except Exception as e:
        print(f"Kan de seriële poort niet openen: {e}")
        exit(1)

    # Start achtergrond thread om Arduino/Teensy output te lezen
    threading.Thread(target=read_from_serial, args=(ser,), daemon=True).start()

    try:
        while True:
            # Typ hier wat je wilt versturen naar Arduino
            data = input("Typ iets om te sturen (of 'exit' om te stoppen): ")
            if data.lower() == "exit":
                break

            ser.write((data + "\n").encode())
            print(f"[TX] {data}")

    except KeyboardInterrupt:
        print("Stoppen...")

    finally:
        ser.close()
        print("Seriële verbinding gesloten.")
