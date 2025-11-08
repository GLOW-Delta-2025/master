#!/usr/bin/env python3
"""
X32 Multi-Point Channel Monitor
Monitors audio at different points in the X32 signal chain:
- /meters/0: PRE-FADER (post-EQ, post-comp)
- /meters/1: POST-FADER (final output level)
- /meters/13: PRE-EQ (raw input)

Allows monitoring at Input (pre-EQ), Post-EQ, Post-Compression, and Output stages.
"""

from dataclasses import dataclass, field
from typing import List, Dict
import threading
import time
import math
import struct

from osc4py3.as_eventloop import *
from osc4py3 import oscbuildparse


@dataclass
class MeterPoint:
    """A single measurement point in the audio chain"""
    name: str  # "Pre-EQ", "Post-Comp", "Post-Fader", etc.
    osc_path: str  # OSC path like "/meters/13" or "/meters/1"
    raw: float = 0.0
    rms: float = 0.0
    dbfs: float = -120.0
    peak_hold: float = -120.0
    last_update: float = 0.0
    alpha: float = 0.3  # RMS smoothing factor

    def update(self, raw_value: float) -> None:
        """Update this meter point with new raw value"""
        self.raw = raw_value
        self.rms = self.alpha * raw_value + (1 - self.alpha) * self.rms
        
        # Convert to dBFS
        if self.rms > 0.0:
            self.dbfs = 20.0 * math.log10(self.rms + 1e-12)
        else:
            self.dbfs = -120.0
        
        # Track peak
        if self.dbfs > self.peak_hold:
            self.peak_hold = self.dbfs
        
        self.last_update = time.time()

    def reset_peak(self) -> None:
        """Reset peak hold value"""
        self.peak_hold = -120.0


@dataclass
class ChannelMultiPoint:
    """Holds all measurement points for a single channel"""
    channel_num: int  # 1-32
    points: Dict[str, MeterPoint] = field(default_factory=dict)
    
    def add_point(self, name: str, osc_path: str) -> None:
        """Add a new measurement point"""
        self.points[name] = MeterPoint(name=name, osc_path=osc_path)
    
    def update_point(self, point_name: str, raw_value: float) -> None:
        """Update specific measurement point"""
        if point_name in self.points:
            self.points[point_name].update(raw_value)
    
    def reset_peaks(self) -> None:
        """Reset all peak holds"""
        for point in self.points.values():
            point.reset_peak()


class X32MultiPointMonitor:
    """
    Monitor X32 audio at multiple points in the signal chain.
    
    Available meter paths:
    - /meters/13: PRE-EQ (raw input from preamp)
    - /meters/14: POST-EQ (after tone shaping)
    - /meters/0:  POST-INSERT (after dynamics/insert FX)
    - /meters/1:  POST-FADER (final channel output)
    """
    
    # Standard X32 meter tap points
    METER_POINTS = {
        "Input": "/meters/13",      # Pre-EQ raw input
        "Post-EQ": "/meters/14",    # After EQ
        "Post-Comp": "/meters/0",   # After dynamics (pre-fader)
        "Output": "/meters/1",      # Post-fader output
    }
    
    def __init__(
        self,
        x32_ip: str = "192.168.0.3",
        x32_port: int = 10023,
        listen_port: int = 10024,
        channel_count: int = 32,
        enabled_points: List[str] = None,
    ):
        self.x32_ip = x32_ip
        self.x32_port = x32_port
        self.listen_port = listen_port
        self.channel_count = channel_count
        
        # Which measurement points to enable
        if enabled_points is None:
            enabled_points = ["Input", "Post-EQ", "Post-Comp", "Output"]
        self.enabled_points = enabled_points
        
        # Create channel structures
        self.channels: List[ChannelMultiPoint] = []
        for ch in range(channel_count):
            channel = ChannelMultiPoint(channel_num=ch + 1)
            for point_name in enabled_points:
                if point_name in self.METER_POINTS:
                    channel.add_point(point_name, self.METER_POINTS[point_name])
            self.channels.append(channel)
        
        self._lock = threading.Lock()
        self._running = False
        self._keepalive_thread = None
        self._osc_thread = None

    def start(self) -> None:
        """Start monitoring"""
        if self._running:
            return

        print(f"[INIT] Starting X32 Multi-Point Monitor...")
        print(f"[INIT] X32 address: {self.x32_ip}:{self.x32_port}")
        print(f"[INIT] Listening on: 0.0.0.0:{self.listen_port}")
        print(f"[INIT] Monitoring points: {', '.join(self.enabled_points)}")
        
        self._running = True
        
        # Initialize osc4py3
        osc_startup()
        
        # Create UDP client for sending to X32
        osc_udp_client(self.x32_ip, self.x32_port, "x32")
        
        # Create UDP server for receiving from X32
        osc_udp_server("0.0.0.0", self.listen_port, "monitor")
        
        # Register handlers for each enabled measurement point
        for point_name in self.enabled_points:
            osc_path = self.METER_POINTS.get(point_name)
            if osc_path:
                osc_method(osc_path, lambda addr, tags, data, client, pn=point_name: 
                          self._handle_meter_blob(addr, tags, data, client, pn))
        
        time.sleep(0.5)  # Let OSC initialize

        # CRITICAL: Send /xremote FIRST
        print(f"[NET] Sending /xremote to enable remote control...")
        self._send_xremote()
        osc_process()
        time.sleep(0.3)
        
        # Subscribe to all enabled meter paths
        print(f"[NET] Subscribing to meter paths...")
        for point_name in self.enabled_points:
            osc_path = self.METER_POINTS.get(point_name)
            if osc_path:
                print(f"[NET]   → {osc_path} ({point_name})")
                msg = oscbuildparse.OSCMessage("/meters", None, [osc_path])
                osc_send(msg, "x32")
                osc_process()
                time.sleep(0.1)
        
        print(f"[INIT] ✓ Initialization complete. Waiting for meter data...")

        # Start OSC processing thread
        self._osc_thread = threading.Thread(target=self._osc_loop, daemon=True)
        self._osc_thread.start()
        
        # Start keepalive thread
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

    def stop(self) -> None:
        """Stop monitoring"""
        self._running = False
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=1.0)
        if self._osc_thread:
            self._osc_thread.join(timeout=1.0)
        try:
            osc_terminate()
        except:
            pass
        print("\n[STOP] Multi-point monitor closed.")

    def _osc_loop(self) -> None:
        """Process OSC messages continuously"""
        while self._running:
            osc_process()
            time.sleep(0.01)
    
    def _handle_meter_blob(self, address, tags, data, client_address, point_name: str):
        """
        Parse incoming meter blob and update channel levels for specific point.
        
        Args:
            point_name: Name of measurement point ("Input", "Post-EQ", etc.)
        """
        if not data or len(data) == 0:
            return
        
        blob = data[0]
        
        # Debug: print first reception for this point
        debug_attr = f'_first_{point_name}_received'
        if not hasattr(self, debug_attr):
            setattr(self, debug_attr, True)
            print(f"[DEBUG] ✓ First {point_name} data! Path: {address}, Size: {len(blob)} bytes")

        # Parse channel values (4 bytes per channel, big-endian float)
        with self._lock:
            for ch in range(min(self.channel_count, len(blob) // 4)):
                offset = ch * 4
                if offset + 4 <= len(blob):
                    raw_value = struct.unpack('>f', blob[offset:offset+4])[0]
                    self.channels[ch].update_point(point_name, raw_value)

    def _keepalive_loop(self) -> None:
        """Send periodic keepalive and renew subscriptions"""
        subscription_counter = 0
        while self._running:
            # Send /xremote keepalive every 9 seconds
            self._send_xremote()
            
            # Renew subscriptions every 27 seconds (every 3rd keepalive)
            subscription_counter += 1
            if subscription_counter >= 3:
                subscription_counter = 0
                for point_name in self.enabled_points:
                    osc_path = self.METER_POINTS.get(point_name)
                    if osc_path:
                        msg = oscbuildparse.OSCMessage("/meters", None, [osc_path])
                        osc_send(msg, "x32")
            
            time.sleep(9.0)

    def _send_xremote(self) -> None:
        """Send /xremote keepalive"""
        try:
            msg = oscbuildparse.OSCMessage("/xremote", None, [])
            osc_send(msg, "x32")
        except:
            pass

    def snapshot(self) -> List[ChannelMultiPoint]:
        """Get current snapshot of all channels"""
        with self._lock:
            # Deep copy of channel data
            return [
                ChannelMultiPoint(
                    channel_num=ch.channel_num,
                    points={
                        name: MeterPoint(
                            name=point.name,
                            osc_path=point.osc_path,
                            raw=point.raw,
                            rms=point.rms,
                            dbfs=point.dbfs,
                            peak_hold=point.peak_hold,
                            last_update=point.last_update,
                        )
                        for name, point in ch.points.items()
                    }
                )
                for ch in self.channels
            ]

    def reset_peaks(self) -> None:
        """Reset all peak holds"""
        with self._lock:
            for ch in self.channels:
                ch.reset_peaks()


def print_loop(
    monitor: X32MultiPointMonitor,
    interval: float = 1.0,
    channels_to_show: int = 4,
) -> None:
    """Console visualizer showing all measurement points"""
    try:
        while True:
            snapshot = monitor.snapshot()
            print("\033[2J\033[H", end="")  # Clear terminal
            print("X32 Multi-Point Channel Monitor\n")
            print("=" * 80)
            
            for idx in range(min(channels_to_show, len(snapshot))):
                channel = snapshot[idx]
                print(f"\n📊 Channel {channel.channel_num}")
                print("-" * 80)
                
                # Show each measurement point
                for point_name, point in channel.points.items():
                    age = time.time() - point.last_update
                    
                    # Create visual meter bar
                    bar_len = int((point.dbfs + 60) / 60 * 30)  # -60 to 0 dB = 30 chars
                    bar_len = max(0, min(30, bar_len))
                    bar = '█' * bar_len + '░' * (30 - bar_len)
                    
                    print(f"  {point_name:12s}: {point.dbfs:>6.1f} dB |{bar}| "
                          f"Peak: {point.peak_hold:>6.1f} dB ({age:>5.1f}s ago)")
            
            print("\n" + "=" * 80)
            print(f"Press Ctrl+C to stop | Points: {', '.join(monitor.enabled_points)}")
            time.sleep(interval)
            
    except KeyboardInterrupt:
        print("\nStopping monitor...")
        monitor.stop()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="X32 Multi-Point Monitor - Track audio at different processing stages"
    )
    parser.add_argument("--ip", default="192.168.0.3", help="X32 mixer IP address")
    parser.add_argument("--port", type=int, default=10023, help="X32 OSC port")
    parser.add_argument("--listen", type=int, default=10024, help="Local UDP port")
    parser.add_argument("--channels", type=int, default=32, help="Number of channels")
    parser.add_argument(
        "--display", type=int, default=4, help="Number of channels to display"
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="Display refresh interval (seconds)"
    )
    parser.add_argument(
        "--points",
        nargs="+",
        choices=["Input", "Post-EQ", "Post-Comp", "Output"],
        default=["Input", "Post-EQ", "Post-Comp", "Output"],
        help="Which measurement points to monitor",
    )

    args = parser.parse_args()

    monitor = X32MultiPointMonitor(
        x32_ip=args.ip,
        x32_port=args.port,
        listen_port=args.listen,
        channel_count=args.channels,
        enabled_points=args.points,
    )
    
    monitor.start()
    print_loop(monitor, interval=args.interval, channels_to_show=args.display)


if __name__ == "__main__":
    main()
