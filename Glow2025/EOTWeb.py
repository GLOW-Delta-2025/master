# web_ui.py
"""
Web UI for Mac Mini Controller (single-file).
Place this file next to your controller file (save your original script as macmini_controller.py).
Run: python3 web_ui.py
Open: http://127.0.0.1:5000/

Features:
 - View & edit many config constants
 - Apply changes at runtime (updates module globals + controller attributes where sensible)
 - Save / Load config to disk (config.json)
 - Live status (health, arms, pending confirms, stars_collected)
 - Simple controls (manual reset, trigger peak, change color, set keepalive/climax timeout)
"""
import json
import threading
import time
import os
from typing import Dict, Any

from flask import Flask, jsonify, request, send_file, render_template_string

# Try to import the controller module/class from the user's script.
# Assumes your main script is saved as macmini_controller.py and exposes MacMiniController.
try:
    import EOTMain as controller_module
    MacMiniController = controller_module.MacMiniController
except Exception as e:
    raise RuntimeError(
        "Failed to import your controller module. Save your original script as "
        "'macmini_controller.py' in the same folder as this file. Import error: " + str(e)
    )

# Create / use a single controller instance in this process.
# If you already run the controller elsewhere, don't run it again; but this UI needs a live instance.
controller = None
controller_thread = None

def ensure_controller_running():
    global controller, controller_thread
    if controller is None:
        # instantiate the controller (this will open the serial port etc)
        controller = MacMiniController()
        # start main loop in a thread if not already
        controller_thread = threading.Thread(target=controller.run, daemon=True)
        controller_thread.start()
        # input_loop is blocking; we don't start it here.
    return controller

ensure_controller_running()

# Editable keys and mapping to where to apply them
# key -> (module_attr_name, apply_to_controller_function_or_None)
# If apply_fn is None, we only set the module-level variable.
EDITABLE_KEYS = {
    "SERIAL_PORT": ("SERIAL_PORT", None),
    "SERIAL_BAUD": ("SERIAL_BAUD", None),
    "NUM_ARMS": ("NUM_ARMS", None),
    "MAX_STARS_FOR_CLIMAX": ("MAX_STARS_FOR_CLIMAX", lambda val: setattr(controller, "stars_collected", min(controller.stars_collected, int(val)))),
    "PEAK_TIMEOUT": ("PEAK_TIMEOUT", None),
    "STAR_SEND_TIME": ("STAR_SEND_TIME", None),
    "MAX_BRIGHTNESS": ("MAX_BRIGHTNESS", None),
    "UPDATE_STEP": ("UPDATE_STEP", None),
    "ACK_TIMEOUT": ("ACK_TIMEOUT", None),
    "MAX_SEND_RETRIES": ("MAX_SEND_RETRIES", None),
    "ARRIVAL_WARN_AFTER": ("ARRIVAL_WARN_AFTER", None),
    "ARRIVAL_TIMEOUT": ("ARRIVAL_TIMEOUT", None),
    "WARN_THROTTLE": ("WARN_THROTTLE", None),
    "DEFAULT_KEEPALIVE_INTERVAL": ("DEFAULT_KEEPALIVE_INTERVAL", lambda v: controller.set_keepalive_interval(float(v))),
    "HEALTH_FAIL_THRESHOLD": ("HEALTH_FAIL_THRESHOLD", None),
    "ARM_SPEED_MIN": ("ARM_SPEED_MIN", None),
    "ARM_SPEED_MAX": ("ARM_SPEED_MAX", None),
    "CENTER_SPEED_MIN": ("CENTER_SPEED_MIN", None),
    "CENTER_SPEED_MAX": ("CENTER_SPEED_MAX", None),
    "CLIMAX_TIMEOUT_SECONDS": ("CLIMAX_TIMEOUT_SECONDS", lambda v: controller.set_climax_timeout(float(v))),
    "DEBUG_FRAMES": ("DEBUG_FRAMES", None),
    "UPDATE_SHOW_COLOR_ARM": ("show_color_arm", lambda v: controller.set_show_color(arm_value=int(v))),
    "UPDATE_SHOW_COLOR_CENTER": ("show_color_center", lambda v: controller.set_show_color(center_hex=str(v))),
}

CONFIG_FILE = "controller_config.json"

app = Flask(__name__)

# --- Utility helpers ---
def get_current_config() -> Dict[str, Any]:
    """Return a dict of editable config values + runtime-derived info."""
    mod = controller_module
    cfg = {}
    for k, (mod_name, apply_fn) in EDITABLE_KEYS.items():
        # special-case keys that map to controller attributes (lowercase names used above)
        if hasattr(mod, mod_name):
            cfg[k] = getattr(mod, mod_name)
        elif hasattr(controller, mod_name):
            cfg[k] = getattr(controller, mod_name)
        else:
            cfg[k] = None
    # add some read-only runtime info
    cfg["runtime"] = {
        "stars_collected": getattr(controller, "stars_collected", None),
        "climax_state": getattr(controller, "climax_state", None).name if getattr(controller, "climax_state", None) is not None else None,
        "keepalive_interval": getattr(controller, "keepalive_interval", None),
        "pending_confirms": len(getattr(controller, "pending_confirms", {})),
        "version": getattr(mod, "VERSION", None)
    }
    return cfg

def apply_config_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    """Apply changes to module globals and controller where possible. Return info about what changed."""
    mod = controller_module
    result = {"applied": {}, "errors": {}}
    for k, v in changes.items():
        if k not in EDITABLE_KEYS:
            result["errors"][k] = "Not editable via web UI."
            continue
        mod_name, apply_fn = EDITABLE_KEYS[k]
        try:
            # basic type normalization
            if isinstance(v, str):
                # try to parse booleans or numbers when applicable
                if v.lower() in ("true", "false"):
                    parsed = v.lower() == "true"
                else:
                    try:
                        if "." in v:
                            parsed = float(v)
                        else:
                            parsed = int(v)
                    except Exception:
                        parsed = v
            else:
                parsed = v
            # set on module if name exists there
            if hasattr(mod, mod_name):
                setattr(mod, mod_name, parsed)
                result["applied"][k] = f"module.{mod_name} = {parsed!r}"
            elif hasattr(controller, mod_name):
                setattr(controller, mod_name, parsed)
                result["applied"][k] = f"controller.{mod_name} = {parsed!r}"
            else:
                # fallback: create module attribute
                setattr(mod, mod_name, parsed)
                result["applied"][k] = f"module.{mod_name} (created) = {parsed!r}"
            # call apply_fn if present
            if apply_fn:
                try:
                    apply_fn(parsed)
                except Exception as e:
                    result["errors"][k] = f"apply_fn error: {e}"
        except Exception as e:
            result["errors"][k] = str(e)
    return result

def get_runtime_status() -> Dict[str, Any]:
    """Return a snapshot of controller runtime status: health, arms, pending confirms etc."""
    c = controller
    with c.lock:
        health = {dev.value: data.copy() for dev, data in c.health.items()}
        arms = {}
        for arm_dev, star in c.arms.items():
            arms[arm_dev.value] = {
                "state": star.state.name,
                "brightness": getattr(star, "brightness", None),
                "last_peak_time": getattr(star, "last_peak_time", None),
                "awaiting_ack": getattr(star, "awaiting_ack", None),
                "awaiting_arrival": getattr(star, "awaiting_arrival", None),
                "retry_count": getattr(star, "retry_count", None)
            }
        pending = [
            {"device": t.device.value, "cmd": t.cmd.value, "last_sent": t.last_sent, "retries": t.retries}
            for t in c.pending_confirms.values()
        ]
        status = {
            "stars_collected": c.stars_collected,
            "climax_state": c.climax_state.name,
            "climax_started_at": c.climax_started_at,
            "climax_timeout_seconds": c.climax_timeout_seconds,
            "keepalive_interval": c.keepalive_interval,
            "health": health,
            "arms": arms,
            "pending_confirms": pending,
            "pending_count": len(pending),
            "version": getattr(controller_module, "VERSION", None),
        }
    return status

# --- REST API ---
@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = get_current_config()
    return jsonify(cfg)

@app.route("/api/config", methods=["POST"])
def api_post_config():
    payload = request.get_json(force=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected JSON object"}), 400
    res = apply_config_changes(payload)
    # optionally save to disk if ?save=1
    if request.args.get("save") in ("1", "true", "True"):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(payload, f, indent=2)
            res["saved_to_disk"] = CONFIG_FILE
        except Exception as e:
            res.setdefault("errors", {})["save"] = str(e)
    return jsonify(res)

@app.route("/api/config/save", methods=["GET"])
def api_save_config():
    cfg = get_current_config()
    # we will persist only editable keys, not runtime
    save_obj = {}
    for k in EDITABLE_KEYS.keys():
        save_obj[k] = cfg.get(k)
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(save_obj, f, indent=2)
        return jsonify({"saved": CONFIG_FILE})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/config/load", methods=["GET"])
def api_load_config():
    if not os.path.exists(CONFIG_FILE):
        return jsonify({"error": "no config.json found"}), 404
    try:
        with open(CONFIG_FILE, "r") as f:
            obj = json.load(f)
        res = apply_config_changes(obj)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(get_runtime_status())

@app.route("/api/action/trigger_peak", methods=["POST"])
def api_trigger_peak():
    body = request.get_json(force=True) if request.data else {}
    arm = int(body.get("arm", 1))
    if not (1 <= arm <= getattr(controller_module, "NUM_ARMS", 5)):
        return jsonify({"error": "arm out of range"}), 400
    # trigger peak on controller
    try:
        controller.trigger_peak(arm)
        return jsonify({"ok": True, "arm": arm})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/action/reset_show", methods=["POST"])
def api_reset_show():
    try:
        controller._reset_show_cycle()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/action/set_color", methods=["POST"])
def api_set_color():
    body = request.get_json(force=True)
    arm_v = body.get("arm_value")
    center_hex = body.get("center_hex")
    try:
        if arm_v is not None:
            controller.set_show_color(arm_value=int(arm_v))
        if center_hex is not None:
            controller.set_show_color(center_hex=str(center_hex))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Simple SPA for UI (single HTML) ---
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Glow Controller — Web UI</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body { font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; margin: 18px; background:#0f1720; color:#e6eef8; }
    .card { background:#0b1220; border-radius:10px; padding:14px; box-shadow: 0 6px 20px rgba(0,0,0,0.6); margin-bottom:12px; }
    input, select { padding:8px; border-radius:6px; border:1px solid #223; background:#081018; color:#e6eef8; width:100%; box-sizing:border-box; }
    label { font-size:13px; color:#bcd; display:block; margin-bottom:6px; }
    .grid { display:grid; grid-template-columns: 1fr 320px; gap:12px; }
    .btn { background:#1f6feb; color:white; border:none; padding:10px 12px; border-radius:8px; cursor:pointer; }
    .btn-ghost { background:transparent; border:1px solid #234; color:#cfe; }
    .muted { color:#9db; font-size:13px; }
    .flex { display:flex; gap:8px; align-items:center; }
    textarea { width:100%; min-height:120px; background:#06101a; color:#dff; border-radius:8px; padding:10px; border:1px solid #123; }
    pre { background:#02060a; padding:12px; border-radius:8px; overflow:auto; max-height:300px; }
  </style>
</head>
<body>
  <h2>Glow — Mac Mini Controller (Web UI)</h2>
  <div class="grid">
    <div>
      <div class="card">
        <h3>Live Status</h3>
        <div id="status">loading…</div>
        <div style="margin-top:8px;" class="flex">
          <button class="btn" onclick="refreshStatus()">Refresh</button>
          <button class="btn-ghost" onclick="doReset()">Reset Show</button>
          <button class="btn-ghost" onclick="document.getElementById('manualPeakArm').value=1">Manual Peak (choose arm below)</button>
          <select id="manualPeakArm" style="width:120px;">
            <option value="1">ARM1</option><option value="2">ARM2</option><option value="3">ARM3</option><option value="4">ARM4</option><option value="5">ARM5</option>
          </select>
          <button class="btn" onclick="triggerPeak()">Trigger Peak</button>
        </div>
      </div>

      <div class="card">
        <h3>Runtime Controls</h3>
        <div style="display:flex;gap:8px">
          <input id="colorArm" placeholder="show_color_arm (0-255)">
          <input id="colorCenter" placeholder="show_color_center (#RRGGBB)">
        </div>
        <div style="margin-top:8px" class="flex">
          <button class="btn" onclick="setColor()">Apply Color</button>
          <button class="btn-ghost" onclick="loadConfig()">Load saved config</button>
          <button class="btn-ghost" onclick="saveConfig()">Save editable config to disk</button>
        </div>
        <div style="margin-top:10px;">
          <label class="muted">Climax timeout (s)</label>
          <input id="climaxTimeout" type="number" min="5">
          <div style="margin-top:6px" class="flex">
            <button class="btn" onclick="setClimaxTimeout()">Set</button>
            <button class="btn-ghost" onclick="setKeepalive(30)">Set Keepalive 30s</button>
            <button class="btn-ghost" onclick="setKeepalive(60)">Set Keepalive 60s</button>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>Editable Config</h3>
        <div id="configFields">loading…</div>
        <div style="margin-top:8px;" class="flex">
          <button class="btn" onclick="applyConfig()">Apply changes</button>
          <button class="btn-ghost" onclick="applyConfig(true)">Apply + Save</button>
        </div>
        <p class="muted">Tip: changes attempt to update module globals and controller attributes. Some constants are used at startup.</p>
      </div>
    </div>

    <div>
      <div class="card">
        <h3>Pending confirms & logs</h3>
        <pre id="pending">loading…</pre>
      </div>

      <div class="card">
        <h3>Health / Arms snapshot</h3>
        <pre id="health">loading…</pre>
      </div>

      <div class="card">
        <h3>Raw config JSON</h3>
        <textarea id="rawConfig"></textarea>
      </div>
    </div>
  </div>

<script>
async function fetchJson(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const t = await r.text();
    throw new Error('HTTP ' + r.status + ': ' + t);
  }
  return r.json();
}

async function refreshStatus() {
  try {
    const s = await fetchJson('/api/status');
    document.getElementById('status').innerText = 'stars_collected: ' + s.stars_collected + '\\nclimax_state: ' + s.climax_state + '\\nkeepalive_interval: ' + s.keepalive_interval + '\\npending: ' + s.pending_count;
    document.getElementById('pending').innerText = JSON.stringify(s.pending_confirms, null, 2);
    document.getElementById('health').innerText = JSON.stringify({health: s.health, arms: s.arms}, null, 2);
    // populate some small fields
    document.getElementById('colorArm').value = s.health ? '' : '';
    document.getElementById('climaxTimeout').value = s.climax_timeout_seconds || '';
  } catch (e) {
    document.getElementById('status').innerText = 'ERROR: ' + e.message;
  }
}

async function loadConfig() {
  try {
    const cfg = await fetchJson('/api/config');
    // build the fields
    const fieldsDiv = document.getElementById('configFields');
    fieldsDiv.innerHTML = '';
    window._currentConfig = cfg;
    const editableKeys = Object.keys(cfg).filter(k => k !== 'runtime');
    editableKeys.forEach(k => {
      const v = cfg[k];
      const id = 'f_' + k;
      const wrapper = document.createElement('div');
      wrapper.style.marginBottom = '8px';
      wrapper.innerHTML = `<label>${k}</label><input id="${id}" value="${v===null?'':v}">`;
      fieldsDiv.appendChild(wrapper);
    });
    document.getElementById('rawConfig').value = JSON.stringify(cfg, null, 2);
  } catch (e) {
    alert('Load config failed: ' + e.message);
  }
}

async function applyConfig(save=false) {
  // gather fields from configFields
  const nodes = document.querySelectorAll('#configFields input');
  const payload = {};
  nodes.forEach(n => {
    const k = n.id.slice(2);
    if (n.value !== '') payload[k] = n.value;
  });
  try {
    const res = await fetchJson('/api/config' + (save ? '?save=1' : ''), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    alert('Apply result: ' + JSON.stringify(res));
  } catch (e) {
    alert('Apply failed: ' + e.message);
  }
}

async function saveConfig() {
  try {
    const res = await fetchJson('/api/config/save');
    alert(JSON.stringify(res));
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
}

async function triggerPeak() {
  const arm = document.getElementById('manualPeakArm').value;
  try {
    const res = await fetchJson('/api/action/trigger_peak', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({arm: parseInt(arm)})
    });
    alert('Triggered: ' + JSON.stringify(res));
  } catch (e) {
    alert('Trigger failed: ' + e.message);
  }
}

async function doReset() {
  if (!confirm('Reset show state?')) return;
  try {
    const res = await fetchJson('/api/action/reset_show', {method: 'POST'});
    alert(JSON.stringify(res));
  } catch (e) {
    alert('Reset failed: ' + e.message);
  }
}

async function setColor() {
  const av = document.getElementById('colorArm').value;
  const ch = document.getElementById('colorCenter').value;
  try {
    const res = await fetchJson('/api/action/set_color', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({arm_value: av || null, center_hex: ch || null})
    });
    alert(JSON.stringify(res));
  } catch (e) {
    alert('Set color failed: ' + e.message);
  }
}

async function setClimaxTimeout() {
  const v = parseFloat(document.getElementById('climaxTimeout').value);
  if (!v) return alert('invalid value');
  try {
    const res = await fetchJson('/api/config?save=0', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({CLIMAX_TIMEOUT_SECONDS: v})
    });
    alert(JSON.stringify(res));
  } catch (e) {
    alert('Set failed: ' + e.message);
  }
}

async function setKeepalive(sec) {
  try {
    const res = await fetchJson('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({DEFAULT_KEEPALIVE_INTERVAL: sec})
    });
    alert(JSON.stringify(res));
  } catch (e) {
    alert('Set failed: ' + e.message);
  }
}

window.addEventListener('load', () => {
  loadConfig();
  refreshStatus();
  // refresh status periodically
  setInterval(refreshStatus, 3000);
});
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

if __name__ == "__main__":
    print("Starting Web UI on http://127.0.0.1:5000/ (Ctrl+C to quit)")
    # Flask default server is fine for local use
    app.run(host="127.0.0.1", port=5000, debug=False)
