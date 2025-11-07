# BlueprintConfig.py
"""
Blueprint for configuration management and color control.
Handles:
 - /config UI page
 - /api/config (GET/POST)
 - /api/config/load and /save
 - /api/action/set_color
"""

from flask import Blueprint, jsonify, request, render_template
import json, os
from typing import Dict, Any

from EOTcontroller import controller  # import the same shared controller
import EOTMain as controller_module

config_bp = Blueprint("config_bp", __name__, template_folder="templates")

CONFIG_FILE = "controller_config.json"

# ---------------------------------------------------------------------
# Editable keys (moved here from EOTWeb)
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def get_current_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    mod = controller

    for k, (mod_name, _) in EDITABLE_KEYS.items():
        if hasattr(mod, mod_name):
            cfg[k] = getattr(mod, mod_name)
        elif hasattr(controller, mod_name):
            cfg[k] = getattr(controller, mod_name)
        else:
            cfg[k] = None

    # Runtime health info
    runtime_health: Dict[str, Dict[str, Any]] = {}
    if hasattr(controller, "health"):
        for dev, info in controller.health.items():
            runtime_health[dev.value] = {
                "online": bool(info.get("online", False)),
                "failures": int(info.get("failures", 0)),
                "last_seen": float(info.get("last_seen", 0))
            }

    stars_collected = getattr(controller, "stars_collected", 0)
    cs = getattr(controller, "climax_state", None)
    if cs is None:
        climax_state = None
    elif hasattr(cs, "name"):
        climax_state = cs.name
    else:
        climax_state = cs

    pending = getattr(controller, "pending_confirms", 0)
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
    mod = controller
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

# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@config_bp.route("/")
def config_ui():
    """Render the configuration page."""
    return render_template("config.html")


@config_bp.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(get_current_config())


@config_bp.route("/api/config", methods=["POST"])
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


@config_bp.route("/api/config/save", methods=["GET"])
def api_save_config():
    cfg = get_current_config()
    save_obj = {k: cfg.get(k) for k in EDITABLE_KEYS.keys()}
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(save_obj, f, indent=2)
        return jsonify({"saved": CONFIG_FILE})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@config_bp.route("/api/config/load", methods=["GET"])
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


@config_bp.route("/api/action/set_color", methods=["POST"])
def api_set_color():
    """Change LED colors for arms and center."""
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
