import threading
from flask import Flask, render_template, jsonify, request

# --- Shared controller / core logic ---
from EOTcontroller import controller  # Import the initialized controller

# --- Blueprints ---
from BlueprintAudio import audio_bp
from BlueprintConfig import config_bp

# --- Create Flask app ---
app = Flask(__name__)

# --- Register blueprints ---
app.register_blueprint(audio_bp, url_prefix="/audio")
app.register_blueprint(config_bp, url_prefix="/config")


# ---------------------------------------------------------------------
# Index route (Live Status & Logs)
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------
# Live status & manual action API routes (index page)
# ---------------------------------------------------------------------
@app.route("/api/status", methods=["GET"])
def api_status():
    """Return live runtime status for index page"""
    status = getattr(controller, "get_runtime_status", lambda: {})()
    return jsonify(status if status else {"error": "no status method"})


@app.route("/api/action/trigger_peak", methods=["POST"])
def api_trigger_peak():
    body = request.get_json(force=True) or {}
    try:
        arm = int(body.get("arm", 1))
        num_arms = int(getattr(controller, "NUM_ARMS", 5))
    except ValueError:
        return jsonify({"error": "invalid arm number"}), 400

    if not (1 <= arm <= num_arms):
        return jsonify({"error": "arm out of range"}), 400

    try:
        controller.trigger_peak(arm)
        return jsonify({"ok": True, "arm": arm})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/action/reset_show", methods=["POST"])
def api_reset_show():
    """Reset the show cycle manually"""
    try:
        controller._reset_show_cycle()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------
# Run the server
# ---------------------------------------------------------------------
if __name__ == "__main__":
    print("Starting EOT Web UI on http://0.0.0.0:5000/")
    app.run(host="0.0.0.0", port=5000, debug=False)
