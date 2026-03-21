# relay_server.py
import RPi.GPIO as GPIO
from flask import Flask, jsonify, request
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature
import base64
import config
import logging
import atexit
import signal
import sys

app = Flask(__name__)

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

RELAY_PINS = config.RELAY_PINS

# In-memory state for status (on/off)
_relay_state = {rid: "off" for rid in RELAY_PINS}

# Setup GPIO (inverted: LOW = off, HIGH = on)
GPIO.setmode(GPIO.BCM)
for pin in RELAY_PINS.values():
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)  # all off at startup

# Cleanup handler (may run from signal and atexit; only run once)
_gpio_cleaned = False

def cleanup(sig=None, frame=None):
    global _gpio_cleaned
    if _gpio_cleaned:
        return
    _gpio_cleaned = True
    logging.info("Turning off all relays")
    for rid in RELAY_PINS:
        _set_relay(rid, 'off')
    logging.info("Cleaning up GPIO")
    GPIO.cleanup()
    sys.exit(0)  # Exit gracefully

atexit.register(cleanup)

# Signal handlers
signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# Load public key
with open(config.PUBLIC_KEY_PATH, "rb") as key_file:
    PUBLIC_KEY = serialization.load_pem_public_key(key_file.read())

# Validate RELAY_MAPPINGS at startup (local check for pins)
def validate_mappings():
    invalid = [mid for mid, (srv, rid) in config.RELAY_MAPPINGS.items() if rid not in RELAY_PINS]
    if invalid:
        logging.error(f"Invalid relay_ids in mappings: {invalid}")
        raise ValueError("Invalid relay mappings")
validate_mappings()

def _verify_signature(message: bytes):
    sig_header = request.headers.get('X-Signature')
    if not sig_header:
        raise ValueError("Missing signature")
    signature = base64.b64decode(sig_header)
    PUBLIC_KEY.verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )

def _set_relay(relay_id: int, action: str) -> bool:
    if relay_id not in RELAY_PINS or action not in ('on', 'off'):
        return False
    pin = RELAY_PINS[relay_id]
    # Inverted: GPIO HIGH = relay on, GPIO LOW = relay off (hardware is active-low)
    GPIO.output(pin, GPIO.HIGH if action == 'on' else GPIO.LOW)
    _relay_state[relay_id] = action
    logging.info(f"Set relay {relay_id} to {action}")
    return True

@app.route('/status', methods=['GET'])
def get_status():
    return jsonify({"status": "ok", "relays": dict(_relay_state)})

@app.route('/relay', methods=['POST'])
def control_relay():
    """One batch: relay IDs in the list are set to action; any relay not in the list is turned off."""
    data = request.get_json(force=True, silent=True) or {}
    relays = data.get('relays', [])
    action = data.get('action')
    if not isinstance(relays, list) or action not in ('on', 'off'):
        logging.warning("Invalid body")
        return jsonify({"error": "Invalid body"}), 400
    relay_ids = sorted(set(int(r) for r in relays if str(r).isdigit()))
    message = f"relays={','.join(map(str, relay_ids))}&action={action}".encode()
    try:
        _verify_signature(message)
    except InvalidSignature:
        logging.warning("Invalid signature")
        return jsonify({"error": "Invalid signature"}), 401
    except Exception as e:
        logging.error(f"Verify failed: {str(e)}")
        return jsonify({"error": f"Verify failed: {str(e)}"}), 500
    # Any relay not in the batch is turned off; then the batch is set to action (on/off)
    for rid in RELAY_PINS:
        if rid not in relay_ids:
            _set_relay(rid, 'off')
    applied = [rid for rid in relay_ids if _set_relay(rid, action)]
    invalid = list(set(relay_ids) - set(applied))
    return jsonify({"status": "ok", "action": action, "applied": applied, "invalid": invalid})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, ssl_context=(config.SSL_CERT_PATH, config.SSL_KEY_PATH))