import asyncio
import re
from collections import deque, defaultdict
import serial_asyncio  # Make sure this is installed

class GlowMaster:
    SERIAL_PORT = "/dev/tty.usbmodem14301"
    BAUD_RATE = 115200

    MAX_STARS = 10
    BUILDUP_DURATION = 10
    CLIMAX_DURATION = 15
    AUTO_TEST_INTERVAL = 1

    MAX_RETRIES = 3
    CONFIRM_TIMEOUT = 2

    ARM_DEVICES = ["ARM1", "ARM2", "ARM3", "ARM4", "ARM5"]

    def __init__(self):
        self.star_counter = 0
        self.flow_active = False

        self.waiting_for_star = asyncio.Event()
        self.waiting_for_buildup = asyncio.Event()
        self.waiting_for_climax = asyncio.Event()

        self.msg_regex = re.compile(
            r"!!(?P<device>[^:]+):(?P<source>[^:]+):(?P<type>[^:]+):(?P<command>[^{#]+)"
            r"(?:\{(?P<params>[^}]*)\})?##"
        )

        # Key: (device, command), value: deque of Events for multiple parallel commands
        self.confirm_events = defaultdict(deque)

        # Serial simulation queues
        self.serial_rx_queue = deque()
        self.serial_writer = None

    def format_message(self, device, command_type, command, params=None):
        param_str = "{}"
        if params:
            param_str = "{" + ",".join(f"{k}={v}" for k, v in params.items()) + "}"
        return f"!!{device}:{command_type}:{command}{param_str}##"

    async def init_serial(self):
        print(f"[INFO] Connecting to {self.SERIAL_PORT}...")
        self.serial_reader, self.serial_writer = await serial_asyncio.open_serial_connection(
            url=self.SERIAL_PORT, baudrate=self.BAUD_RATE
        )
        print(f"[INFO] Connected to {self.SERIAL_PORT}")

    async def send_command(self, device, command, params=None):
        key = (device, command)
        evt = asyncio.Event()
        self.confirm_events[key].append(evt)

        msg = self.format_message(device, "REQUEST", command, params)
        attempt = 0

        while attempt < self.MAX_RETRIES:
            attempt += 1
            print(f"[TX] {msg} (try {attempt})")
            self.serial_writer.write((msg + "\n").encode())
            await self.serial_writer.drain()

            try:
                await asyncio.wait_for(evt.wait(), timeout=self.CONFIRM_TIMEOUT)
                self.confirm_events[key].popleft()  # remove confirmed event
                if not self.confirm_events[key]:
                    del self.confirm_events[key]
                print(f"[CONFIRM RECEIVED] {device}:{command}")
                return True
            except asyncio.TimeoutError:
                print(f"[WARNING] No confirmation from {device}:{command}, retrying...")

        # Failed after retries
        self.confirm_events[key].popleft()
        if not self.confirm_events[key]:
            del self.confirm_events[key]
        print(f"[ERROR] {device}:{command} did not confirm after {self.MAX_RETRIES} attempts!")
        return False

    async def handle_star_arrived(self, params):
        print(f"[EVENT] STAR_ARRIVED received: {params}")
        okc = await self.send_command("CENTER", "ADD_STAR", params)
        okt = await self.send_command("TOP", "ADD_STAR", params)
        if not (okc and okt):
            print("[ERROR] ADD_STAR failed on CENTER or TOP")

        self.star_counter += 1
        self.waiting_for_star.clear()

        if self.star_counter >= self.MAX_STARS:
            print("[FLOW] Max stars reached, starting buildup...")
            self.waiting_for_buildup.set()
            asyncio.create_task(self.build_up_sequence())

    async def handle_climax_ready(self):
        print("[EVENT] CLIMAX_READY received")
        self.waiting_for_climax.set()
        asyncio.create_task(self.climax_sequence())

    async def build_up_sequence(self):
        print(f"[FLOW] Buildup started ({self.BUILDUP_DURATION}s)")
        await self.send_command("CENTER", "BUILDUP_CLIMAX_CENTER", {"SPEED": 100})
        await asyncio.sleep(self.BUILDUP_DURATION)
        self.waiting_for_buildup.clear()
        await self.send_command("MASTER", "CLIMAX_READY")

    async def climax_sequence(self):
        print(f"[FLOW] Climax started ({self.CLIMAX_DURATION}s)")
        await self.send_command("CENTER", "START_CLIMAX_CENTER", {"TIME": self.CLIMAX_DURATION * 1000})
        await self.send_command("TOP", "START_CLIMAX_TOP", {"TIME": self.CLIMAX_DURATION * 1000})
        await asyncio.sleep(self.CLIMAX_DURATION)
        self.waiting_for_climax.clear()
        await self.reset_flow()

    async def reset_flow(self):
        await self.send_command("BROADCAST", "RESET")
        self.star_counter = 0
        self.flow_active = False
        print("[FLOW] System ready for next cycle.\n")

    async def listen_serial(self):
        while True:
            line = await self.serial_reader.readline()
            if not line:
                await asyncio.sleep(0.05)
                continue
            line = line.decode(errors="ignore").strip()
            print(f"[RX] {line}")

            match = self.msg_regex.match(line)
            if not match:
                continue

            device = match.group("device")
            source = match.group("source")
            msg_type = match.group("type").upper()
            command = match.group("command").strip().upper()
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

            if msg_type == "REQUEST":
                if command == "STAR_ARRIVED":
                    await self.handle_star_arrived(params)
                elif command == "CLIMAX_READY":
                    await self.handle_climax_ready()
            elif msg_type == "CONFIRM":
                key = (device, command)
                if key in self.confirm_events and self.confirm_events[key]:
                    self.confirm_events[key][0].set()
                else:
                    print(f"[WARN] Unexpected confirm: {device}:{command}")

    async def auto_test_loop(self):
        self.flow_active = True
        print("--- New AUTO_TEST cycle started ---")

        tasks = []
        for i in range(self.MAX_STARS):
            arm = self.ARM_DEVICES[i % len(self.ARM_DEVICES)]
            params = {"SPEED": 50, "COLOR": 128, "BRIGHTNESS": 200, "SIZE": 5}
            tasks.append(asyncio.create_task(self.handle_star_for_arm(arm, params)))
            await asyncio.sleep(self.AUTO_TEST_INTERVAL)

        await asyncio.gather(*tasks)
        await self.waiting_for_buildup.wait()
        await self.waiting_for_climax.wait()

    async def handle_star_for_arm(self, arm, params):
        ok_make = await self.send_command(arm, "MAKE_STAR", params)
        if not ok_make:
            print(f"[ERROR] MAKE_STAR failed for {arm}")

        ok_send = await self.send_command(arm, "SEND_STAR")
        if not ok_send:
            print(f"[ERROR] SEND_STAR failed for {arm}")

        self.waiting_for_star.set()
        await asyncio.sleep(0.05)

    async def run_console(self):
        while True:
            cmd = await asyncio.to_thread(input, ">> ")
            if cmd.lower() == "exit":
                break
            if cmd == "AUTO_TEST_LOOP" and not self.flow_active:
                asyncio.create_task(self.auto_test_loop())
                print("[INFO] AUTO_TEST_LOOP started: running continuously.")
            else:
                print("[INFO] Unknown command.")

async def main():
    controller = GlowMaster()
    await controller.init_serial()
    listener_task = asyncio.create_task(controller.listen_serial())
    console_task = asyncio.create_task(controller.run_console())
    await asyncio.gather(listener_task, console_task)

if __name__ == "__main__":
    asyncio.run(main())
