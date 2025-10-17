import sounddevice as sd
import numpy as np
import time as pytime

# --- configuration ---
DEVICE = 1                 # your X32 device index
CHANNELS_TO_MONITOR = [6, 7]  # zero-based indices -> physical channels 7 and 8
SAMPLERATE = 48000
PRINT_INTERVAL = 0.5  # seconds

# Spike detection configuration
SPIKE_THRESHOLD_DB = 6.0    # smaller threshold = more aggressive detection (dB)
NOISE_FLOOR_INIT_DB = -50.0 # starting estimate of noise floor per channel (dBFS)
# Asymmetric linear RMS alphas: quick to follow drops, slower to raise (avoid lifting on spikes)
NOISE_ALPHA_RISE = 0.95     # when rms_chunk > baseline -> update slowly
NOISE_ALPHA_DECAY = 0.6     # when rms_chunk < baseline -> update quickly
MIN_VALID_DB = -100.0       # ignore extremely quiet values

# Get number of input channels from the device
device_info = sd.query_devices(DEVICE)
NUM_CHANNELS = device_info['max_input_channels']

missing = [ch for ch in CHANNELS_TO_MONITOR if ch >= NUM_CHANNELS]
if missing:
    print(f"Requested channels {[m+1 for m in missing]} exceed available inputs ({NUM_CHANNELS}).")
    raise SystemExit(1)


# Accumulator for windowed level calculation
_level_state = {
    'sum_squares': {ch: 0.0 for ch in CHANNELS_TO_MONITOR},
    'count': 0,
    'last_print': pytime.monotonic(),
    # store noise floor as linear RMS for dynamic adaptation
    'noise_lin': {ch: 10 ** (NOISE_FLOOR_INIT_DB / 20.0) for ch in CHANNELS_TO_MONITOR},
    # Spike record within current window: store max dB and delta over floor
    'spike': {ch: None for ch in CHANNELS_TO_MONITOR},
}

def audio_callback(indata, frames, time, status):
    if status:
        print(status)

    # Per-callback chunk analysis: detect spikes vs noise floor
    eps = 1e-12
    for ch in CHANNELS_TO_MONITOR:
        data = indata[:, ch].astype(np.float64, copy=False)
        # current chunk RMS and dBFS
        mean_square_chunk = float(np.dot(data, data)) / max(len(data), 1)
        rms_chunk = np.sqrt(max(mean_square_chunk, 0.0))
        db_chunk = 20.0 * np.log10(max(rms_chunk, eps))


        # Update noise floor in linear RMS domain with asymmetric EMA
        noise_lin = _level_state['noise_lin'][ch]
        if rms_chunk > noise_lin:
            alpha = NOISE_ALPHA_RISE
        else:
            alpha = NOISE_ALPHA_DECAY
        new_noise_lin = alpha * noise_lin + (1.0 - alpha) * rms_chunk
        _level_state['noise_lin'][ch] = new_noise_lin

        # Spike detection: compare dB of current chunk against baseline dB
        noise_db = 20.0 * np.log10(max(new_noise_lin, eps))
        if db_chunk > MIN_VALID_DB and db_chunk >= noise_db + SPIKE_THRESHOLD_DB:
            delta = db_chunk - noise_db
            prev = _level_state['spike'][ch]
            if prev is None or db_chunk > prev['db']:
                _level_state['spike'][ch] = {'db': db_chunk, 'delta': delta}

        # Accumulate energy for optional windowed average (not printed unless needed)
        _level_state['sum_squares'][ch] += float(np.dot(data, data))
    _level_state['count'] += frames

    # Print every PRINT_INTERVAL seconds
    now = pytime.monotonic()
    if now - _level_state['last_print'] >= PRINT_INTERVAL:
        # If spikes occurred in this window, print them
        messages = []
        for ch in CHANNELS_TO_MONITOR:
            s = _level_state['spike'][ch]
            if s is not None:
                nf_db = 20.0 * np.log10(max(_level_state['noise_lin'][ch], 1e-12))
                messages.append(f"ch{ch+1}: {s['db']:.1f} dBFS (+{s['delta']:.1f} over {nf_db:.1f})")
        if messages:
            print("SPIKE", " ".join(messages))

        # Reset window
        for ch in CHANNELS_TO_MONITOR:
            _level_state['sum_squares'][ch] = 0.0
            _level_state['spike'][ch] = None
        _level_state['count'] = 0
        _level_state['last_print'] = now


# --- open stream ---
with sd.InputStream(
    device=DEVICE,
    channels=NUM_CHANNELS,
    callback=audio_callback,
    samplerate=SAMPLERATE,
    dtype='float32',
):
    print("Listening on channels:", ", ".join(str(ch + 1) for ch in CHANNELS_TO_MONITOR))
    print(f"Detecting spikes every {PRINT_INTERVAL:.1f}s; threshold {SPIKE_THRESHOLD_DB:.1f} dB above noise floor")
    input("Press Enter to stop...\n")
