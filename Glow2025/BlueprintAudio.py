from flask import Blueprint, jsonify, request, send_file,render_template
from lib.main import AudioProcessor
from lib.config import get_config, save_local_config

audio_bp = Blueprint("audio_bp", __name__, template_folder="templates")

@audio_bp.route("/")
def audio_index():
    return render_template("audio.html")

_cfg = get_config()
processor = AudioProcessor.from_config(_cfg)

if 'spike_threshold_db' in _cfg:
    processor.spike_threshold_db = float(_cfg['spike_threshold_db'])
if 'alpha_rise' in _cfg:
    processor.alpha_rise = float(_cfg['alpha_rise'])
if 'alpha_decay' in _cfg:
    processor.alpha_decay = float(_cfg['alpha_decay'])


def _parse_channels(val):
    """Normalize channels input into a tuple of ints.

    Accepts a comma-separated string like "4,7", a whitespace-separated
    string, or a list/tuple of numbers/strings.
    """
    if val is None:
        return None
    if isinstance(val, str):
        # split on commas or whitespace
        parts = [p for p in (v.strip() for v in val.replace(',', ' ').split()) if p]
    elif isinstance(val, (list, tuple)):
        parts = [str(p).strip() for p in val]
    else:
        # try to coerce single number
        try:
            return (int(val),)
        except Exception:
            return None
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            pass
    if not out:
        return None
    return tuple(out)


def _parse_names(val):
    if val is None:
        return []
    if isinstance(val, str):
        parts = [p.strip() for p in val.split(',') if p.strip()]
    elif isinstance(val, (list, tuple)):
        parts = [str(p).strip() for p in val]
    else:
        parts = [str(val)]
    return parts

@audio_bp.route("/status", methods=["GET"])
def status():
    spikes, avg_db, noise_db = processor.read_and_clear_window()
    # include channel names mapping
    resp = {'running': processor.running,
            'spikes': spikes,
            'avg_db': avg_db,
            'noise_db': noise_db,
            'channel_names': processor.channel_names}
    return jsonify(resp)


@audio_bp.route('/start', methods=['POST'])
def start():
    data = request.json or {}
    # allow overriding params
    try:
        if 'device' in data:
            processor.device = int(data['device'])
        if 'channels' in data:
            chs = _parse_channels(data.get('channels'))
            if chs is not None:
                processor.channels = chs
                processor._ensure_state()
        if 'names' in data:
            parts = _parse_names(data.get('names'))
            # map provided names to channels in order
            for i, ch in enumerate(processor.channels):
                if i < len(parts):
                    processor.channel_names[ch] = parts[i]
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


@audio_bp.route('/stop', methods=['POST'])
def stop():
    processor.stop()
    return jsonify({'ok': True})


@audio_bp.route('/config', methods=['POST'])
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
            chs = _parse_channels(data.get('channels'))
            if chs is not None and chs != processor.channels:
                processor.channels = chs
                processor._ensure_state()
                need_restart = True
        if 'names' in data:
            parts = _parse_names(data.get('names'))
            for i, ch in enumerate(processor.channels):
                if i < len(parts):
                    processor.channel_names[ch] = parts[i]
        if 'threshold' in data:
            processor.spike_threshold_db = float(data['threshold'])
        if 'alpha_rise' in data:
            processor.alpha_rise = float(data['alpha_rise'])
        if 'alpha_decay' in data:
            processor.alpha_decay = float(data['alpha_decay'])

        if need_restart and processor.running:
            processor.stop()
            processor.start()
        # persist the requested configuration so other processes can pick it up
        try:
            to_save = {}
            if 'device' in data:
                to_save['device'] = int(data['device'])
            if 'channels' in data:
                chs = _parse_channels(data.get('channels'))
                if chs is not None:
                    to_save['channels'] = list(chs)
            if 'names' in data:
                parts = _parse_names(data.get('names'))
                if parts:
                    to_save['names'] = parts
            if 'threshold' in data:
                try:
                    to_save['spike_threshold_db'] = float(data['threshold'])
                except Exception:
                    pass
            if 'alpha_rise' in data:
                to_save['alpha_rise'] = float(data['alpha_rise'])
            if 'alpha_decay' in data:
                to_save['alpha_decay'] = float(data['alpha_decay'])
            # accept UI 'print_interval' or 'interval' and map to canonical 'poll_interval'
            if 'print_interval' in data:
                try:
                    to_save['poll_interval'] = float(data['print_interval'])
                except Exception:
                    pass
            if 'interval' in data:
                try:
                    to_save['poll_interval'] = float(data['interval'])
                except Exception:
                    pass
            if to_save:
                save_local_config(to_save)
        except Exception:
            pass
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    # return current merged config
    return jsonify({'ok': True, 'config': get_config()})

@audio_bp.route('/config', methods=['GET'])
def config_get():
    try:
        cfg = get_config()
        return jsonify({'ok': True, 'config': cfg})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
