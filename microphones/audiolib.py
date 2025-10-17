"""
audiolib.py
Convenience wrapper to use the spike detector as a library from other programs.

The wrapper forwards configuration to the existing AudioProcessor defined in `main.py`.
"""
from typing import Callable, Iterable, Optional
import threading
import time

from main import AudioProcessor


class AudioProcessorLib:
    """Wrapper that runs an AudioProcessor and polls it periodically.

    Callbacks:
      - spike callbacks: fn(channel_index, spike_obj, avg_db, noise_db)
    """

    def __init__(self, *, device: int = 1, channels: Iterable[int] = (6, 7),
                 poll_interval: float = 0.5, start_stream: bool = True, **processor_kwargs):
        # processor_kwargs are forwarded to AudioProcessor constructor
        self.processor = AudioProcessor(device=device, channels=tuple(channels), **processor_kwargs)
        self._poll_interval = float(poll_interval)
        self._spike_callbacks = []
        self._star_callbacks = []
        self._thread: Optional[threading.Thread] = None
        self._stop_ev = threading.Event()
        self._started_stream = False
        if start_stream:
            # user can postpone starting by passing start_stream=False
            self.processor.start()
            self._started_stream = True

    # lifecycle
    def start(self, start_stream: Optional[bool] = None):
        """Start polling and optionally start the audio stream.

        start_stream: if True, start the underlying input stream.
        If None, do not change current stream state.
        """
        if start_stream is True and not self._started_stream:
            self.processor.start()
            self._started_stream = True
        self._stop_ev.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self, stop_stream: bool = True):
        self._stop_ev.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if stop_stream and self._started_stream:
            self.processor.stop()
            self._started_stream = False

    # callbacks
    def register_spike_callback(self, fn: Callable[[int, dict, float, float], None]):
        self._spike_callbacks.append(fn)

    # config helpers
    def config(self, **kwargs):
        """Apply configuration to the underlying processor.

        Accepts the same keys as the `/config` endpoint: device, channels, threshold,
        alpha_rise, alpha_decay, names, etc.
        """
        # apply fields directly
        if 'device' in kwargs:
            self.processor.device = int(kwargs['device'])
        if 'channels' in kwargs:
            self.processor.channels = tuple(int(c) for c in kwargs['channels'])
            self.processor._ensure_state()
        if 'names' in kwargs:
            n = kwargs['names']
            if isinstance(n, str):
                parts = [p.strip() for p in n.split(',')]
            else:
                parts = list(n)
            for i, ch in enumerate(self.processor.channels):
                if i < len(parts):
                    self.processor.channel_names[ch] = parts[i]
        if 'threshold' in kwargs:
            self.processor.spike_threshold_db = float(kwargs['threshold'])
        if 'alpha_rise' in kwargs:
            self.processor.alpha_rise = float(kwargs['alpha_rise'])
        if 'alpha_decay' in kwargs:
            self.processor.alpha_decay = float(kwargs['alpha_decay'])

    # polling loop
    def _poll_loop(self):
        while not self._stop_ev.is_set():
            spikes, avg_db, noise_db = self.processor.read_and_clear_window()
            # deliver spike callbacks
            for ch, s in (spikes or {}).items():
                if s is not None:
                    for cb in list(self._spike_callbacks):
                        try:
                            cb(ch, s, avg_db.get(ch), noise_db.get(ch))
                        except Exception:
                            pass
            # deliver star callbacks (read and clear stored stars)
            with self.processor._lock:
                stars = dict(self.processor._state.get('stars', {}))
                if stars:
                    self.processor._state['stars'] = {}
            for ch, st in stars.items():
                for cb in list(self._star_callbacks):
                    try:
                        cb(ch, st)
                    except Exception:
                        pass
            time.sleep(self._poll_interval)


if __name__ == '__main__':
    # small smoke test when run directly
    def spike_print(ch, spike, avg, noise):
        print('SPIKE cb', ch, spike, avg, noise)

    lib = AudioProcessorLib(device=1, channels=(6,7), spike_threshold_db=4.0)
    lib.register_spike_callback(spike_print)
    lib.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        lib.stop()
