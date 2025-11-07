# BlueprintConfig.py
from flask import Blueprint, jsonify, request, render_template
import json, os
from typing import Dict, Any

# import shared controller + helpers
from EOTWeb import controller, controller_module, EDITABLE_KEYS, CONFIG_FILE, apply_config_changes, get_current_config


config_bp = Blueprint("config_bp", __name__)

@config_bp.route("/")
def config_ui():
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
    
# BlueprintConfig.py
from flask import Blueprint, jsonify, request
import json, os
from typing import Dict, Any

# Import shared symbols from main app
from EOTWeb import controller, controller_module, EDITABLE_KEYS, CONFIG_FILE, apply_config_changes, get_current_config

config_bp = Blueprint("config_bp", __name__)

# --- CONFIG MANAGEMENT ---

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


# --- COLOR CONTROL ---

@config_bp.route("/api/action/set_color", methods=["POST"])
def api_set_color():
    """
    Set color for arms and/or center light.
    Accepts JSON like:
    { "arm_value": 255, "center_hex": "#FF00FF" }
    """
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

