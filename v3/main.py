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
                 alpha_rise=0.95, alpha_decay=0.6,
                 same_source_corr=0.75,
                 min_dB_delta=4.0,
                 max_delay_ms=10.0):
        self.device = device
        self.channels = tuple(channels)
        self.samplerate = samplerate
        self.spike_threshold_db = spike_threshold_db
        self.alpha_rise = alpha_rise
        self.alpha_decay = alpha_decay
        # Pearson correlation threshold: if correlation between two channel windows
        # >= this value we treat the signals as the same source (suppress duplicates)
        self.same_source_corr = same_source_corr
        # minimum dB difference between loudest and runner-up to auto-assign to loudest
        self.min_dB_delta = float(min_dB_delta)
        # maximum allowed delay (ms) between same-source signals for NCC check
        self.max_delay_ms = float(max_delay_ms)

        self._lock = threading.Lock()
        self._state = {
            'noise_lin': {},
            'spike': {},
                'buffers': {},
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
                # store the raw samples for later correlation checks
                # keep per-callback buffer; will be concatenated/cleared in read_and_clear_window
                self._state['buffers'][ch].append(data.copy())
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

            # If multiple channels have candidate spikes, decide whether they are the same source
            # or separate noises. Build candidate list (explicit spike or avg above floor+threshold).
            candidates = []
            for ch in self.channels:
                nd = noise_db.get(ch, -999.0)
                if spikes.get(ch) is not None or avg_db.get(ch, -999.0) >= nd + self.spike_threshold_db:
                    candidates.append(ch)

            loudest = None
            loudest_db = -999.0
            for ch in candidates:
                # prefer explicit spike db if present
                s = spikes.get(ch)
                val = s['db'] if s is not None else avg_db.get(ch, -999.0)
                if val > loudest_db:
                    loudest_db = val
                    loudest = ch

            if len(candidates) > 1 and loudest is not None:
                # prepare concatenated buffers for correlation; if buffers missing, treat as uncorrelated
                def get_buf(ch):
                    parts = self._state['buffers'].get(ch, [])
                    if not parts:
                        return None
                    return np.concatenate(parts)

                loud_buf = get_buf(loudest)
                for ch in list(candidates):
                    if ch == loudest:
                        continue
                    # quick amplitude-based check: if loudest is much louder, suppress others
                    # compute runner-up val
                    s_other = spikes.get(ch)
                    val_other = s_other['db'] if s_other is not None else avg_db.get(ch, -999.0)
                    if loudest_db - val_other >= self.min_dB_delta:
                        spikes[ch] = None
                        continue
                    other_buf = get_buf(ch)
                    same = False
                    if loud_buf is None or other_buf is None:
                        same = False
                    else:
                        # make equal length by trimming to shortest
                        n = min(len(loud_buf), len(other_buf))
                        if n < 16:
                            same = False
                        else:
                            x = loud_buf[:n]
                            y = other_buf[:n]
                            # zero-mean
                            x = x - x.mean()
                            y = y - y.mean()
                            sx = x.std()
                            sy = y.std()
                            if sx <= 0 or sy <= 0:
                                same = False
                            else:
                                # compute normalized cross-correlation and allow small lag
                                # use FFT-based cross-correlation for efficiency
                                # compute full cross-correlation via FFT
                                L = 1 << (n - 1).bit_length()
                                X = np.fft.rfft(x, L)
                                Y = np.fft.rfft(y, L)
                                R = X * np.conj(Y)
                                cc = np.fft.irfft(R, L)
                                # cc is circular; shift to get linear cross-corr
                                cc = np.concatenate((cc[-n+1:], cc[:n]))
                                # normalize
                                cc = cc / (n * sx * sy)
                                max_corr_idx = int(np.argmax(np.abs(cc)))
                                max_corr = float(cc[max_corr_idx])
                                # convert index to lag in samples
                                lag = max_corr_idx - (n - 1)
                                lag_ms = abs(lag) * 1000.0 / float(self.samplerate)
                                same = (max_corr >= self.same_source_corr and lag_ms <= self.max_delay_ms)
                    # if same source, suppress the non-loudest channel spike
                    if same:
                        spikes[ch] = None

            # reset per-channel accumulation and spike state (we already prepared `spikes` to return)
            for ch in self.channels:
                self._state['sum_squares'][ch] = 0.0
                self._state['spike'][ch] = None
                # clear buffers
                self._state['buffers'][ch] = []
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
                # ensure buffers dict has a list for each channel
                if ch not in self._state['buffers']:
                    self._state['buffers'][ch] = []


# More aggressive defaults: lower threshold and faster baseline adaptation
NOISE_FLOOR_INIT_DB = -50.0
processor = AudioProcessor(spike_threshold_db=4.0, alpha_rise=0.85, alpha_decay=0.4)


@app.route('/')
def index():
    return send_file(__file__.replace('main.py', 'web.html'))


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
