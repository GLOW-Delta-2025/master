#!/usr/bin/env python3
"""
X32 OSC Test - Check if X32 responds to basic OSC messages
"""

from osc4py3.as_eventloop import *
from osc4py3 import oscbuildparse
import time
import threading

def test_x32_osc(x32_ip="192.168.0.3"):
    """Test basic OSC communication with X32"""

    print(f"Testing OSC communication with X32 at {x32_ip}:10023")

    try:
        # Initialize osc4py3
        osc_startup()

        # Register catch-all handler BEFORE creating servers
        osc_method("/*", handle_any_osc)

        # Create UDP client to X32
        osc_udp_client(x32_ip, 10023, "x32")

        # Create UDP server to receive responses
        osc_udp_server("0.0.0.0", 10024, "server")

        print("✓ OSC client/server started")

        # Test 1: Send /xremote
        print("\nTest 1: Sending /xremote...")
        msg = oscbuildparse.OSCMessage("/xremote", None, "")
        osc_send(msg, "x32")
        osc_process()
        time.sleep(0.5)

        # Test 2: Send /xinfo (should get response)
        print("Test 2: Sending /xinfo...")
        msg = oscbuildparse.OSCMessage("/xinfo", None, "")
        osc_send(msg, "x32")
        osc_process()
        time.sleep(0.5)

        # Test 3: Try to get channel fader level (should get response)
        print("Test 3: Requesting /ch/01/mix/fader...")
        msg = oscbuildparse.OSCMessage("/ch/01/mix/fader", None, "")
        osc_send(msg, "x32")
        osc_process()
        time.sleep(0.5)

        # Test 4: Try meters subscription
        print("Test 4: Subscribing to /meters/1...")
        msg = oscbuildparse.OSCMessage("/meters", None, "/meters/1")
        osc_send(msg, "x32")
        osc_process()
        time.sleep(1.0)

        # Test 5: Try pre-fader meters
        print("Test 5: Subscribing to /meters/0...")
        msg = oscbuildparse.OSCMessage("/meters", None, "/meters/0")
        osc_send(msg, "x32")
        osc_process()
        time.sleep(1.0)

        print("\nWaiting 5 seconds for any responses...")
        start_time = time.time()
        while time.time() - start_time < 5:
            osc_process()
            time.sleep(0.1)

        print("Test complete. If no responses received, X32 may not support OSC.")

    except Exception as e:
        print(f"✗ OSC test failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        try:
            osc_terminate()
        except:
            pass

def handle_any_osc(address, tags, data, client_address):
    """Catch any OSC messages received"""
    print(f"← Received: {address} {tags} {data[:10] if data else 'no data'}")

if __name__ == "__main__":
    test_x32_osc()