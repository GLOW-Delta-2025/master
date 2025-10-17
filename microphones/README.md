# Audio Processor

A real-time audio spike and anomaly detection system with an adaptive noise floor. Monitors multiple audio channels and reports detected spikes, as well as longer-duration audio events ("stars").

## Features

- **Multi-channel monitoring**: Listen to multiple audio channels simultaneously
- **Adaptive noise floor**: Dynamically tracks background noise levels with configurable rise/decay rates
- **Spike detection**: Identifies audio peaks above the noise floor with configurable sensitivity
- **Same-source deduplication**: Uses correlation analysis to suppress duplicate detections from the same audio source across multiple channels
- **Star generation**: Automatically generates event markers based on spike accumulation and activity patterns
- **Multiple interfaces**: Web UI, library API, or direct command-line use

## Quick Start

### Web Interface

```bash
python web.py
```

Visit `http://127.0.0.1:5000` in your browser to configure channels, threshold, and smoothing parameters.

### Library Usage

Use `audiolib.py` to integrate spike detection into your Python application:

```python
from audiolib import AudioProcessorLib
import time

# Create processor for channels 6 and 7 on device 1
lib = AudioProcessorLib(device=1, channels=(6, 7), spike_threshold_db=4.0)

# Define callbacks for spikes and stars
def on_spike(channel, spike_obj, avg_db, noise_db):
    print(f"Channel {channel}: SPIKE detected at {spike_obj['db']:.1f} dB")
    print(f"  Delta above noise: {spike_obj['delta']:.1f} dB")
    print(f"  Average level: {avg_db:.1f} dB, Noise floor: {noise_db:.1f} dB")

def on_star(channel, star_obj):
    print(f"Channel {channel}: STAR generated - reason={star_obj['reason']}, spikes={star_obj['spike_count']}")

# Register callbacks
lib.register_spike_callback(on_spike)
lib.register_star_callback(on_star)

# Start polling
lib.start()

try:
    # Run indefinitely
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    lib.stop()
```

## Configuration

### Core Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `device` | 1 | Audio device index (use `sounddevice.query_devices()` to list) |
| `channels` | (6, 7) | Tuple of zero-based channel indices to monitor |
| `spike_threshold_db` | 6.0 | Minimum dB above noise floor to trigger a spike |
| `alpha_rise` | 0.95 | Smoothing factor when noise floor rises (0–1, higher = slower) |
| `alpha_decay` | 0.6 | Smoothing factor when noise floor falls (0–1, lower = faster) |

### Star Generation Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `star_max_spikes` | 10 | Number of spikes to accumulate before generating a star |
| `star_timeout` | 10.0 | Seconds of silence after activity to trigger a timeout star |
| `star_hard_limit` | 10.0 | Seconds since last star before forcing one (if conditions met) |
| `star_decibel_avg` | -60.0 | Minimum average dB level to consider "audio present" for hard-limit star |
| `star_activity_db_delta` | 1.0 | dB above noise floor to mark audio as "active" |

## Library API

### AudioProcessorLib

```python
# Initialize
lib = AudioProcessorLib(
    device=1,
    channels=(6, 7),
    poll_interval=0.5,
    start_stream=True,
    spike_threshold_db=4.0,
    # ... other processor kwargs
)

# Lifecycle
lib.start(start_stream=None)     # Start polling (optionally start audio stream)
lib.stop(stop_stream=True)       # Stop polling and optionally close audio stream

# Callbacks
lib.register_spike_callback(fn)  # fn(channel, spike_obj, avg_db, noise_db)
lib.register_star_callback(fn)   # fn(channel, star_obj)

# Configuration (while running)
lib.config(
    threshold=5.0,
    alpha_rise=0.9,
    alpha_decay=0.5,
    names=['Left', 'Right']
)
```

## Requirements

- Python 3.7+
- `sounddevice` (for audio I/O)
- `numpy` (for signal processing)
- `flask` (for web UI only)

## Example: Monitor with Custom Names

```python
from audiolib import AudioProcessorLib

lib = AudioProcessorLib(device=1, channels=(6, 7))
lib.config(names=['Front Left', 'Front Right'])

def on_spike(ch, spike, avg, noise):
    ch_name = {6: 'Front Left', 7: 'Front Right'}.get(ch, f'ch{ch}')
    print(f"{ch_name}: {spike['db']:.1f} dB")

lib.register_spike_callback(on_spike)
lib.start()
```

## Troubleshooting

- **No spikes detected**: Lower `spike_threshold_db` or increase `alpha_decay` (smaller value = faster response)
- **Too many false positives**: Raise `spike_threshold_db` or decrease `alpha_decay` (slower response)
- **Wrong audio device**: List devices with `python -c "import sounddevice; sounddevice.query_devices()" and update `device` index
- **Channel mismatch**: Remember channels are zero-based (channel 6 = physical channel 7)
