#!/usr/bin/env python3
"""
X32 Channel Meters + RTA Scanner
Simple example combining basic dB metering with RTA scanning
Uses osc4py3 for better network compatibility
"""

from osc4py3.as_eventloop import *
from osc4py3 import oscbuildparse
import struct
import time
import threading
import socket

class X32SimpleMonitor:
    """Simple X32 monitor with channel meters and rotating RTA"""
    
    def __init__(self, x32_ip="192.168.0.3"):
        self.x32_ip = x32_ip
        self.client = None
        
        # Storage
        self.channel_levels = [0.0] * 32  # dB levels for channels 1-32
        self.current_rta_channel = 0
        self.current_rta_data = [0.0] * 100
        
        self.running = False
        
    def _preflight_check(self):
        """Test network connectivity before starting"""
        print(f"[NET] Testing connection to {self.x32_ip}:10023...")
        try:
            # Create test socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2.0)
            
            # Don't actually send, just check we can get a route
            s.connect((self.x32_ip, 10023))
            local_ip = s.getsockname()[0]
            s.close()
            
            # Get interface info
            import subprocess
            try:
                result = subprocess.run(['route', '-n', 'get', self.x32_ip], 
                                      capture_output=True, text=True, timeout=2)
                for line in result.stdout.split('\n'):
                    if 'interface:' in line:
                        iface = line.split(':')[1].strip()
                        print(f"[NET] Using interface: {iface}")
                        break
            except:
                pass
            
            print(f"[NET] ✓ Route OK: {local_ip} -> {self.x32_ip}:10023")
            return True
            
        except OSError as e:
            print(f"[NET] ✗ No route to {self.x32_ip}:10023")
            print(f"[NET] Error: {e}")
            print("\n[FIX] Troubleshooting:")
            print("  1. Disable macOS Firewall: System Settings → Network → Firewall")
            print("  2. Turn off Wi-Fi (use Ethernet only)")
            print(f"  3. Add route: sudo route add -host {self.x32_ip} -interface en0")
            print("  4. Check Mac IP is same subnet as X32")
            return False
        
    def handle_channel_meters(self, address, tags, data, client_address):
        """
        Handle /meters/1 - Channel post-fader levels
        Returns 32 float values (0.0 to 1.0)
        """
        if not data or len(data) == 0:
            return
            
        blob = data[0]
        
        # Debug: print first time we receive data
        if not hasattr(self, '_meter_received'):
            self._meter_received = True
            # Show first 5 raw float values
            raw_vals = [struct.unpack('>f', blob[i*4:(i+1)*4])[0] for i in range(min(5, len(blob)//4))]
            print(f"[DEBUG] Received /meters/1: {len(blob)} bytes, raw values[0-4]: {raw_vals}")
        
        # Parse 32 float values (4 bytes each)
        for i in range(min(32, len(blob) // 4)):
            raw_value = struct.unpack('>f', blob[i*4:(i+1)*4])[0]
            
            # Convert to dB
            if raw_value > 0.0:
                db_value = 20 * (raw_value ** 0.5) - 60  # Approximate meter scaling
            else:
                db_value = -128.0
                
            self.channel_levels[i] = db_value
    
    def handle_rta(self, address, tags, data, client_address):
        """Handle RTA data from /meters/15"""
        if not data or len(data) == 0:
            return
            
        blob = data[0]
        
        # Debug: print first time we receive RTA
        if not hasattr(self, '_rta_received'):
            self._rta_received = True
            print(f"[DEBUG] Received /meters/15 RTA: {len(blob)} bytes")
        
        rta_values = []
        
        # Parse 50 x 32-bit values -> 100 x 16-bit shorts
        for i in range(0, len(blob), 4):
            if i + 4 > len(blob):
                break
                
            int32_value = struct.unpack('>I', blob[i:i+4])[0]
            
            # Extract two 16-bit signed values
            short1 = (int32_value >> 16) & 0xFFFF
            short2 = int32_value & 0xFFFF
            
            if short1 >= 0x8000:
                short1 -= 0x10000
            if short2 >= 0x8000:
                short2 -= 0x10000
                
            # Convert to dB
            rta_values.extend([short1 / 256.0, short2 / 256.0])
        
        self.current_rta_data = rta_values[:100]
    
    def start(self):
        """Start monitoring"""
        print(f"[NET] Connecting to {self.x32_ip}:10023...")
        
        try:
            # Initialize osc4py3
            osc_startup()
            
            # Create UDP client to X32
            osc_udp_client(self.x32_ip, 10023, "x32")
            
            # Create UDP server to receive responses
            osc_udp_server("0.0.0.0", 10024, "server")
            
            # Register handlers
            osc_method("/meters/1", self.handle_channel_meters)
            osc_method("/meters/15", self.handle_rta)
            
            print(f"[NET] ✓ OSC client/server started")
        except Exception as e:
            print(f"[NET] ✗ Failed to start OSC: {e}")
            return
            
        self.running = True
        
        # Start OSC processing thread
        osc_thread = threading.Thread(target=self._osc_loop, daemon=True)
        osc_thread.start()
        
        # Subscribe to meters
        try:
            time.sleep(0.5)  # Let OSC initialize
            
            # CRITICAL: Send /xremote FIRST to enable remote control
            print("[NET] Sending /xremote to enable remote mode...")
            xremote_msg = oscbuildparse.OSCMessage("/xremote", None, "")
            osc_send(xremote_msg, "x32")
            osc_process()
            time.sleep(0.3)
            
            # Now subscribe to meters
            print("[NET] Subscribing to meters...")
            # Try /meters/1 for post-fader instead of /meters/0
            msg1 = oscbuildparse.OSCMessage("/meters", None, "/meters/1")
            msg2 = oscbuildparse.OSCMessage("/meters", None, "/meters/15")
            osc_send(msg1, "x32")
            osc_send(msg2, "x32")
            osc_process()
            print("[NET] ✓ Subscribed to meters (post-fader + RTA)")
        except Exception as e:
            print(f"[START] ✗ Failed to send to X32: {e}")
            import traceback
            traceback.print_exc()
            print(f"\n[FIX] X32 Setup → Remote must be ENABLED")
            print(f"     Check X32 IP matches (currently using {self.x32_ip})")
            print(f"     Try rebooting the X32")
            self.running = False
            return
        
        # Start keepalive
        keepalive_thread = threading.Thread(target=self._keepalive)
        keepalive_thread.daemon = True
        keepalive_thread.start()
        
        time.sleep(0.5)
        print("Monitoring started\n")
    
    def _osc_loop(self):
        """Process OSC messages continuously"""
        while self.running:
            osc_process()
            time.sleep(0.01)
    
    def _keepalive(self):
        """Send keepalive messages"""
        while self.running:
            try:
                msg = oscbuildparse.OSCMessage("/xremote", None, "")
                osc_send(msg, "x32")
                osc_process()
            except:
                pass
            time.sleep(9.0)
    
    def scan_rta_channels(self, channels=[0, 1, 2, 3, 4], interval=0.2):
        """
        Rotate through channels for RTA monitoring
        
        Args:
            channels: List of channel IDs (0-31 for Ch 1-32)
            interval: Seconds per channel
        """
        print(f"RTA scanning channels: {[ch+1 for ch in channels]}\n")
        
        while self.running:
            for ch in channels:
                # Set RTA source (0-31 = Ch 1-32 PRE-EQ)
                try:
                    msg = oscbuildparse.OSCMessage("/-stat/rtasource", None, ch)
                    osc_send(msg, "x32")
                    osc_process()
                    self.current_rta_channel = ch
                except:
                    pass
                time.sleep(interval)
    
    def display_status(self, channels_to_show=5):
        """Display current status"""
        while self.running:
            print("\033[2J\033[H")  # Clear screen
            
            # Show channel levels
            print("=== CHANNEL METERS (POST-FADER) ===")
            for i in range(channels_to_show):
                db = self.channel_levels[i]
                
                # Create meter bar
                bar_len = int((db + 60) / 60 * 40)  # -60 to 0 dB -> 40 chars
                bar_len = max(0, min(40, bar_len))
                
                bar = '█' * bar_len
                print(f"Ch {i+1:2d}: {db:>6.1f} dB |{bar}")
            
            # Show current RTA
            print(f"\n=== RTA - Channel {self.current_rta_channel + 1} ===")
            
            # Show simplified RTA (every 5th band)
            frequencies = [20, 40, 80, 160, 315, 630, 1250, 2500, 5000, 10000, 20000]
            indices = [0, 10, 20, 30, 42, 52, 62, 72, 82, 92, 99]
            
            for freq, idx in zip(frequencies, indices):
                if idx < len(self.current_rta_data):
                    db = self.current_rta_data[idx]
                    bar_len = int((db + 80) / 80 * 30)  # -80 to 0 dB -> 30 chars
                    bar_len = max(0, min(30, bar_len))
                    bar = '█' * bar_len
                    print(f"{freq:>6.0f} Hz: {db:>6.1f} dB |{bar}")
            
            time.sleep(0.5)  # Update display twice per second
    
    def stop(self):
        """Stop monitoring"""
        self.running = False
        try:
            osc_terminate()
        except:
            pass


def main():
    """Example: Monitor 5 channels with meters and rotating RTA"""
    
    # Configure - Match your audio setup
    X32_IP = "192.168.0.3"  # X32 IP address
    CHANNELS = [2, 3, 4, 6, 5]  # Physical channels 3,4,5,7,6 (0-based: 2,3,4,6,5)
    RTA_INTERVAL = 0.3  # Seconds per channel for RTA scanning
    
    # Create monitor
    monitor = X32SimpleMonitor(x32_ip=X32_IP)
    monitor.start()
    
    # Start RTA scanner thread
    rta_thread = threading.Thread(
        target=monitor.scan_rta_channels,
        args=(CHANNELS, RTA_INTERVAL)
    )
    rta_thread.daemon = True
    rta_thread.start()
    
    # Display status
    try:
        monitor.display_status(channels_to_show=5)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        monitor.stop()


if __name__ == "__main__":
    main()
