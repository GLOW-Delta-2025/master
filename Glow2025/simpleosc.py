#!/usr/bin/env python3
"""
X32 Channel Meters + RTA Scanner
Simple example combining basic dB metering with RTA scanning
"""

from pythonosc import udp_client, dispatcher, osc_server
import struct
import time
import threading

class X32SimpleMonitor:
    """Simple X32 monitor with channel meters and rotating RTA"""
    
    def __init__(self, x32_ip="192.168.1.100"):
        self.x32_ip = x32_ip
        self.client = udp_client.SimpleUDPClient(x32_ip, 10023)
        
        # Storage
        self.channel_levels = [0.0] * 32  # dB levels for channels 1-32
        self.current_rta_channel = 0
        self.current_rta_data = [0.0] * 100
        
        # Setup dispatcher
        self.disp = dispatcher.Dispatcher()
        self.disp.map("/meters/1", self.handle_channel_meters)  # Post-fader meters
        self.disp.map("/meters/15", self.handle_rta)
        
        self.running = False
        
    def handle_channel_meters(self, address, *args):
        """
        Handle /meters/1 - Channel post-fader levels
        Returns 32 float values (0.0 to 1.0)
        """
        if not args or len(args) == 0:
            return
            
        blob = args[0]
        
        # Parse 32 float values (4 bytes each)
        for i in range(min(32, len(blob) // 4)):
            raw_value = struct.unpack('>f', blob[i*4:(i+1)*4])[0]
            
            # Convert to dB
            if raw_value > 0.0:
                db_value = 20 * (raw_value ** 0.5) - 60  # Approximate meter scaling
            else:
                db_value = -128.0
                
            self.channel_levels[i] = db_value
    
    def handle_rta(self, address, *args):
        """Handle RTA data from /meters/15"""
        if not args or len(args) == 0:
            return
            
        blob = args[0]
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
        self.running = True
        
        # Start OSC server
        server = osc_server.ThreadingOSCUDPServer(
            ("0.0.0.0", 10024), self.disp
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        # Subscribe to meters
        self.client.send_message("/meters", "/meters/1")   # Channel meters
        self.client.send_message("/meters", "/meters/15")  # RTA data
        
        # Start keepalive
        keepalive_thread = threading.Thread(target=self._keepalive)
        keepalive_thread.daemon = True
        keepalive_thread.start()
        
        time.sleep(0.5)
        print("Monitoring started\n")
    
    def _keepalive(self):
        """Send keepalive messages"""
        while self.running:
            self.client.send_message("/xremote", "")
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
                self.client.send_message("/-stat/rtasource", ch)
                self.current_rta_channel = ch
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


def main():
    """Example: Monitor 5 channels with meters and rotating RTA"""
    
    # Configure
    X32_IP = "192.168.1.100"  # Change to your X32 IP
    CHANNELS = [0, 1, 2, 3, 4]  # Channels 1-5
    RTA_INTERVAL = 0.3  # Seconds per channel for RTA
    
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
