from flask import Flask, jsonify, request, send_file

from main import AudioProcessor

app = Flask(__name__)
processor = AudioProcessor(spike_threshold_db=4.0, alpha_rise=0.85, alpha_decay=0.4)

@app.route('/')
def index():
    return send_file(__file__.replace('web.py', 'web.html'))


@app.route('/start', methods=['POST'])
def start():
    data = request.json or {}
    # allow overriding params
    try:
        if 'device' in data:
            processor.device = int(data['device'])
        if 'channels' in data:
            processor.channels = tuple(int(c) for c in data['channels'])
            processor._ensure_state()
        if 'names' in data:
            # accept comma-separated names or list
            n = data['names']
            if isinstance(n, str):
                parts = [p.strip() for p in n.split(',')]
            else:
                parts = list(n)
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
        if 'names' in data:
            n = data['names']
            if isinstance(n, str):
                parts = [p.strip() for p in n.split(',')]
            else:
                parts = list(n)
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
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True})


@app.route('/status')
def status():
    spikes, avg_db, noise_db = processor.read_and_clear_window()
    # include channel names mapping
    resp = {'running': processor.running,
            'spikes': spikes,
            'avg_db': avg_db,
            'noise_db': noise_db,
            'channel_names': processor.channel_names}
    # include any stars generated in this window
    if isinstance(processor._state.get('stars', {}), dict) and processor._state['stars']:
        resp['stars'] = processor._state['stars']
        # clear reported stars
        processor._state['stars'] = {}
    return jsonify(resp)


if __name__ == '__main__':
    # create a tiny HTML file next to this script if missing
    try:
        open(__file__.replace('web.py', 'web.html')).close()
    except Exception:
        pass
    print('Starting web GUI on http://127.0.0.1:5000')
    app.run(debug=False)
