#!/usr/bin/env python3
"""
v1_web.py
A lightweight web GUI fallback for the spike detector using Flask.

Requires: pip install flask

Run: python v1_web.py
Then open http://127.0.0.1:5000 in your browser.
"""
from flask import Flask, jsonify, request, send_file
import threading
import time
import sounddevice as sd
import numpy as np

app = Flask(__name__)


class AudioProcessor:
    def __init__(self, device=1, channels=(6, 7), samplerate=48000,
                 spike_threshold_db=6.0, noise_init_db=-50.0,
                 alpha_rise=0.95, alpha_decay=0.6):
        self.device = device
        self.channels = tuple(channels)
        self.samplerate = samplerate
        self.spike_threshold_db = spike_threshold_db
        self.alpha_rise = alpha_rise
        self.alpha_decay = alpha_decay

        self._lock = threading.Lock()
        self._state = {
            'noise_lin': {},
            'spike': {},
            'last_print': time.monotonic(),
            'sum_squares': {},
            'count': 0,
            'last_chunk_db': {},
        }
        # initialize per-channel entries
        self._ensure_state(noise_init_db)
        self.stream = None
        self.running = False

    def start(self):
        if self.running:
            return
        info = sd.query_devices(self.device)
        max_ch = info['max_input_channels']
        if any(ch >= max_ch for ch in self.channels):
            raise RuntimeError(f"Device only has {max_ch} input channels")
        # ensure internal state matches configured channels
        self._ensure_state()
        self.stream = sd.InputStream(device=self.device,
                                     channels=max_ch,
                                     samplerate=self.samplerate,
                                     dtype='float32',
                                     callback=self._cb)
        self.stream.start()
        self.running = True

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None
        self.running = False

    def _cb(self, indata, frames, time_info, status):
        eps = 1e-12
        for ch in self.channels:
            data = indata[:, ch].astype(np.float64, copy=False)
            mean_sq = float(np.dot(data, data)) / max(len(data), 1)
            rms = np.sqrt(max(mean_sq, 0.0))
            db = 20.0 * np.log10(max(rms, eps))
            with self._lock:
                self._state['last_chunk_db'][ch] = db
                noise_lin = self._state['noise_lin'][ch]
                alpha = self.alpha_rise if rms > noise_lin else self.alpha_decay
                new_noise = alpha * noise_lin + (1.0 - alpha) * rms
                self._state['noise_lin'][ch] = new_noise
                noise_db = 20.0 * np.log10(max(new_noise, eps))
                if db >= noise_db + self.spike_threshold_db and db > -100.0:
                    prev = self._state['spike'][ch]
                    delta = db - noise_db
                    if prev is None or db > prev['db']:
                        self._state['spike'][ch] = {'db': db, 'delta': delta}
                self._state['sum_squares'][ch] += float(np.dot(data, data))
        with self._lock:
            self._state['count'] += frames

    def read_and_clear_window(self):
        with self._lock:
            spikes = {ch: self._state['spike'].get(ch) for ch in self.channels}
            avg_db = {}
            eps = 1e-12
            if self._state['count'] > 0:
                for ch in self.channels:
                    mean_sq = self._state['sum_squares'][ch] / max(self._state['count'], 1)
                    rms = np.sqrt(max(mean_sq, 0.0))
                    avg_db[ch] = 20.0 * np.log10(max(rms, eps))
            else:
                for ch in self.channels:
                    avg_db[ch] = self._state['last_chunk_db'].get(ch, -50.0)
            noise_db = {ch: 20.0 * np.log10(max(self._state['noise_lin'][ch], eps)) for ch in self.channels}
            # reset
            for ch in self.channels:
                self._state['sum_squares'][ch] = 0.0
                self._state['spike'][ch] = None
            self._state['count'] = 0
            self._state['last_print'] = time.monotonic()
        return spikes, avg_db, noise_db

    def _ensure_state(self, noise_init_db=None):
        """Make sure per-channel keys exist in internal state dicts."""
        if noise_init_db is None:
            noise_init_db = -50.0
        with self._lock:
            for ch in self.channels:
                if ch not in self._state['noise_lin']:
                    self._state['noise_lin'][ch] = 10 ** (noise_init_db / 20.0)
                if ch not in self._state['spike']:
                    self._state['spike'][ch] = None
                if ch not in self._state['sum_squares']:
                    self._state['sum_squares'][ch] = 0.0
                if ch not in self._state['last_chunk_db']:
                    self._state['last_chunk_db'][ch] = noise_init_db


# More aggressive defaults: lower threshold and faster baseline adaptation
NOISE_FLOOR_INIT_DB = -50.0
processor = AudioProcessor(spike_threshold_db=4.0, alpha_rise=0.85, alpha_decay=0.4)


@app.route('/')
def index():
    return send_file(__file__.replace('v1_web.py', 'v1_web.html'))


@app.route('/start', methods=['POST'])
def start():
    data = request.json or {}
    # allow overriding params
    try:
        if 'device' in data:
            processor.device = int(data['device'])
        if 'channels' in data:
            processor.channels = tuple(int(c) for c in data['channels'])
        if 'threshold' in data:
            processor.spike_threshold_db = float(data['threshold'])
        if 'alpha_rise' in data:
            processor.alpha_rise = float(data['alpha_rise'])
        if 'alpha_decay' in data:
            processor.alpha_decay = float(data['alpha_decay'])
        processor.start()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True})


@app.route('/stop', methods=['POST'])
def stop():
    processor.stop()
    return jsonify({'ok': True})


@app.route('/config', methods=['POST'])
def config():
    data = request.json or {}
    try:
        # If device or channels changed while running, restart stream
        need_restart = False
        if 'device' in data:
            d = int(data['device'])
            if d != processor.device:
                processor.device = d
                need_restart = True
        if 'channels' in data:
            chs = tuple(int(c) for c in data['channels'])
            if chs != processor.channels:
                processor.channels = chs
                processor._ensure_state()
                need_restart = True
        if 'threshold' in data:
            processor.spike_threshold_db = float(data['threshold'])
        if 'alpha_rise' in data:
            processor.alpha_rise = float(data['alpha_rise'])
        if 'alpha_decay' in data:
            processor.alpha_decay = float(data['alpha_decay'])

        if need_restart and processor.running:
            processor.stop()
            processor.start()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True})


@app.route('/status')
def status():
    spikes, avg_db, noise_db = processor.read_and_clear_window()
    return jsonify({'running': processor.running,
                    'spikes': spikes,
                    'avg_db': avg_db,
                    'noise_db': noise_db})


if __name__ == '__main__':
    # create a tiny HTML file next to this script if missing
    try:
        open(__file__.replace('main.py', 'web.html')).close()
    except Exception:
        pass
    print('Starting web GUI on http://127.0.0.1:5000')
    app.run(debug=False)
