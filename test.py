#!/usr/bin/env python3
import sounddevice as sd
import numpy as np
import threading, queue, time, sys, termios, tty, select

DEVICE_INDEX = None          # None => default input; zet bv. 1 als je weet welke
CHANNEL_LIST = [3,4,5,7,6]   # Fysieke (1-based) mixer channel nummers die je wilt testen
PRINT_INTERVAL = 0.25        # seconden tussen level prints
LISTEN_ENABLED_START = False # begin zonder monitor
SOLO_CHANNEL = None          # None => alle kanalen gemixt; anders index in CHANNEL_LIST

# Interne mapping: sounddevice gebruikt 0-based channel offsets. Als jouw interface
# alleen een subset exposeert, moet je hier mogelijk aanpassen.
# We nemen aan dat device heeft minstens max(CHANNEL_LIST) kanalen.
def _make_select_mask():
    max_ch = max(CHANNEL_LIST)
    mask = []
    for i in range(max_ch):
        # channel i (0-based) is fysiek i+1
        phys = i+1
        mask.append(phys in CHANNEL_LIST)
    return mask

channel_mask = _make_select_mask()
active_indices = [i for i, flag in enumerate(channel_mask) if flag]

listen_enabled = LISTEN_ENABLED_START
levels = {}
last_print = time.time()
out_q = queue.Queue(maxsize=20)

def _fmt_db(x):
    if x <= 1e-9:
        return "-inf"
    return f"{20*np.log10(x):.1f}"

def audio_callback(indata, frames, time_info, status):
    global last_print
    if status:
        print(f"[STATUS] {status}", flush=True)
    # Select only desired channels
    subset = indata[:, active_indices] if active_indices else indata
    # Compute per-channel peak & rms
    for ci, ch_global in enumerate(active_indices):
        data = subset[:, ci]
        peak = np.max(np.abs(data))
        rms = np.sqrt(np.mean(data**2))
        levels[ch_global+1] = (rms, peak)  # store 1-based channel number
    now = time.time()
    if now - last_print >= PRINT_INTERVAL:
        last_print = now
        line = []
        for ch in CHANNEL_LIST:
            # ch (phys) -> internal index ch-1
            if ch in levels:
                rms, peak = levels[ch]
                line.append(f"{ch}:RMS={_fmt_db(rms)}dB PEAK={_fmt_db(peak)}dB")
            else:
                line.append(f"{ch}:---")
        print("[LEVEL] " + " | ".join(line), flush=True)
    # Listening (monitor)
    if listen_enabled:
        if SOLO_CHANNEL and SOLO_CHANNEL in CHANNEL_LIST:
            idx = active_indices.index(SOLO_CHANNEL-1) if (SOLO_CHANNEL-1) in active_indices else None
            if idx is not None:
                mix = subset[:, idx]
            else:
                mix = np.zeros(frames, dtype=subset.dtype)
        else:
            mix = np.mean(subset, axis=1)
        # Prevent clipping
        mix = np.clip(mix * 1.0, -1.0, 1.0)
        try:
            out_q.put_nowait(mix.copy())
        except queue.Full:
            pass

def output_thread():
    # Simple blocking write to default output device
    with sd.OutputStream(samplerate=sd.query_devices(DEVICE_INDEX, 'input')['default_samplerate'],
                         channels=1, dtype='float32') as out:
        while running:
            try:
                buf = out_q.get(timeout=0.5)
            except queue.Empty:
                buf = np.zeros(512, dtype='float32')
            out.write(buf.reshape(-1,1))

def list_devices():
    print("\n[DEVICES]")
    for i, d in enumerate(sd.query_devices()):
        io = []
        if d['max_input_channels'] > 0: io.append("IN")
        if d['max_output_channels'] > 0: io.append("OUT")
        print(f"  {i}: {d['name']} ({'/'.join(io)})")
    print("")

def get_key(timeout=0.1):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        r,_,_ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1)
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def print_help():
    print("""
[KEYS]
  h  help
  l  toggle listen monitor
  m  toggle mix/solo prompt
  1..9 set SOLO channel (must be in CHANNEL_LIST)
  a  clear SOLO (back to mix)
  d  list devices
  q  quit
""")

running = True

def main():
    global listen_enabled, SOLO_CHANNEL, running
    print_help()
    list_devices()
    dev = DEVICE_INDEX
    if dev is None:
        print("[INFO] Gebruik default input device.")
    else:
        print(f"[INFO] Gebruik input device index {dev}")
    # Determine samplerate from device (fallback 48000)
    try:
        samplerate = int(sd.query_devices(dev, 'input')['default_samplerate'])
    except Exception:
        samplerate = 48000
    print(f"[INFO] Samplerate={samplerate}Hz")
    print(f"[INFO] Fysieke channels getest: {CHANNEL_LIST} (mask={active_indices})")

    out_thr = threading.Thread(target=output_thread, daemon=True)
    out_thr.start()

    with sd.InputStream(device=dev, channels=max(active_indices)+1 if active_indices else 1,
                        samplerate=samplerate, callback=audio_callback, dtype='float32'):
        print("[RUN] Audio input gestart.")
        while running:
            k = get_key()
            if not k: 
                continue
            if k == 'q':
                running = False
            elif k == 'h':
                print_help()
            elif k == 'l':
                listen_enabled = not listen_enabled
                print(f"[LISTEN] {'aan' if listen_enabled else 'uit'} (solo={SOLO_CHANNEL})")
            elif k == 'a':
                SOLO_CHANNEL = None
                print("[SOLO] Disabled (mix).")
            elif k == 'd':
                list_devices()
            elif k.isdigit():
                ch_sel = int(k)
                if ch_sel in CHANNEL_LIST:
                    SOLO_CHANNEL = ch_sel
                    listen_enabled = True
                    print(f"[SOLO] Luister naar kanaal {SOLO_CHANNEL}")
                else:
                    print(f"[SOLO][WARN] Kanaal {ch_sel} niet in CHANNEL_LIST")
            elif k == 'm':
                if SOLO_CHANNEL:
                    SOLO_CHANNEL = None
                    print("[SOLO] Mix (geen solo).")
                else:
                    print("[SOLO] Geen solo actief; druk cijfer 1..9 voor solo.")
            else:
                print(f"[KEY] Onbekend: {k}")
    print("[EXIT] Stoppen...")
    time.sleep(0.5)

if __name__ == "__main__":
    main()