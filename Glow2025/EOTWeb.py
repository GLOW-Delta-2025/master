# EOTWeb.py
"""
Web UI for Mac Mini + Audio Controller
Run: python3 EOTWeb.py
Open: http://127.0.0.1:5000/
Features:
 - View & edit controller config
 - Apply changes at runtime
 - Save / Load config to disk
 - Live status: health, arms, stars_collected
 - Audio processor control (start/stop, channels, thresholds)
"""
import json
import threading
import time
import os
from typing import Dict, Any

from flask import Flask, jsonify, request, render_template


try:
    import EOTMain as controller_module
    MacMiniController = controller_module.MacMiniController
except Exception as e:
    raise RuntimeError(
        "Failed to import EOTMain.py. Make sure it's in the same folder. Import error: " + str(e)
    )

controller = None
controller_thread = None

def ensure_controller_running():
    global controller, controller_thread
    if controller is None:
        controller = MacMiniController()
        controller_thread = threading.Thread(target=controller.run, daemon=True)
        controller_thread.start()
    return controller

ensure_controller_running()

# Editable keys for live config
EDITABLE_KEYS = {
    "SERIAL_PORT": ("SERIAL_PORT", None),
    "SERIAL_BAUD": ("SERIAL_BAUD", None),
    "NUM_ARMS": ("NUM_ARMS", None),
    "MAX_STARS_FOR_CLIMAX": ("MAX_STARS_FOR_CLIMAX", lambda v: setattr(controller, "stars_collected", min(controller.stars_collected, int(v)))),
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

from BlueprintAudio import audio_bp
app.register_blueprint(audio_bp, url_prefix="/audio")


# --- Controller config helpers ---
def get_current_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    mod = controller_module

    # Fetch editable keys from either controller_module or controller
    for k, (mod_name, _) in EDITABLE_KEYS.items():
        if hasattr(mod, mod_name):
            cfg[k] = getattr(mod, mod_name)
        elif hasattr(controller, mod_name):
            cfg[k] = getattr(controller, mod_name)
        else:
            cfg[k] = None

    # Build runtime health info
    runtime_health: Dict[str, Dict[str, Any]] = {}
    if hasattr(controller, "health"):
        for dev, info in controller.health.items():
            runtime_health[dev.value] = {
                "online": bool(info.get("online", False)),
                "failures": int(info.get("failures", 0)),
                "last_seen": float(info.get("last_seen", 0))
            }

    # Safely handle runtime fields
    stars_collected = getattr(controller, "stars_collected", 0)

    cs = getattr(controller, "climax_state", None)
    if cs is None:
        climax_state = None
    elif hasattr(cs, "name"):
        climax_state = cs.name
    else:
        climax_state = cs  # fallback to raw value

    pending = getattr(controller, "pending_confirms", 0)
    # Ensure we count correctly if it's a dict/list or just an int
    if isinstance(pending, (list, dict, set)):
        pending_confirms = len(pending)
    else:
        pending_confirms = int(pending)

    version = getattr(mod, "VERSION", None)

    cfg["runtime"] = {
        "stars_collected": stars_collected,
        "climax_state": climax_state,
        "keepalive_interval": getattr(controller, "keepalive_interval", None),
        "pending_confirms": pending_confirms,
        "version": version,
        "health": runtime_health
    }

    return cfg



def apply_config_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    mod = controller_module
    result = {"applied": {}, "errors": {}}
    for k, v in changes.items():
        if k not in EDITABLE_KEYS:
            result["errors"][k] = "Not editable via web UI."
            continue
        mod_name, apply_fn = EDITABLE_KEYS[k]
        try:
            if isinstance(v, str):
                if v.lower() in ("true", "false"):
                    parsed = v.lower() == "true"
                else:
                    try:
                        parsed = float(v) if "." in v else int(v)
                    except Exception:
                        parsed = v
            else:
                parsed = v
            if hasattr(mod, mod_name):
                setattr(mod, mod_name, parsed)
            elif hasattr(controller, mod_name):
                setattr(controller, mod_name, parsed)
            else:
                setattr(mod, mod_name, parsed)
            if apply_fn:
                try:
                    apply_fn(parsed)
                except Exception as e:
                    result["errors"][k] = f"apply_fn error: {e}"
            result["applied"][k] = parsed
        except Exception as e:
            result["errors"][k] = str(e)
    return result

# --- Audio processor helpers ---
def get_audio_status() -> Dict[str, Any]:
    ap = getattr(controller, "audio_lib", None)
    if ap is None:
        return {"running": False, "channels": [], "names": {}, "avg_db": {}, "noise_db": {}, "spikes": {}}
    return {
        "running": getattr(ap, "running", False),
        "channels": getattr(ap.processor, "channels", []),
        "names": getattr(ap.processor, "channel_names", {}),
        "avg_db": getattr(ap.processor, "avg_db", {}),
        "noise_db": getattr(ap.processor, "noise_db", {}),
        "spikes": getattr(ap.processor, "spikes", {}),
    }

# --- REST API ---
@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(get_current_config())

@app.route("/api/config", methods=["POST"])
def api_post_config():
    payload = request.get_json(force=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected JSON object"}), 400
    res = apply_config_changes(payload)
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
    save_obj = {k: cfg.get(k) for k in EDITABLE_KEYS.keys()}
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(save_obj, f, indent=2)
        return jsonify({"saved": CONFIG_FILE})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/config/load", methods=["GET"])
def api_load_config():
    if not os.path.exists(CONFIG_FILE):
        return jsonify({"error": "no config file found"}), 404
    try:
        with open(CONFIG_FILE, "r") as f:
            obj = json.load(f)
        res = apply_config_changes(obj)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/status", methods=["GET"])
def api_status():
    status = getattr(controller, "get_runtime_status", lambda: {})()
    return jsonify(status if status else {"error": "no status method"})

@app.route("/api/action/trigger_peak", methods=["POST"])
def api_trigger_peak():
    body = request.get_json(force=True) or {}
    arm = int(body.get("arm", 1))
    if not (1 <= arm <= getattr(controller_module, "NUM_ARMS", 5)):
        return jsonify({"error": "arm out of range"}), 400
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


@app.route("/")
def index():
    return render_template('index.html')

if __name__ == "__main__":
    print("Starting EOT Web UI on http://0.0.0.0:5000/")
    app.run(host="0.0.0.0", port=5000, debug=False)
