#!/usr/bin/env python3
"""
Test script to send signed relay commands to the relay server.
Uses the same private key and signing as the coordinator.

Usage:
  python test_relay.py --relays 1 2 --action on
  python test_relay.py --relays 1 2 3 4 5 6 7 8 --action off --url https://localhost:8080
  python test_relay.py --relays 5 --action on --url https://pi1.local:8080
"""

import argparse
import base64
import sys

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

import config

try:
    import requests
except ImportError:
    print("Install requests: pip install requests", file=sys.stderr)
    sys.exit(1)


def load_private_key():
    with open(config.PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_message(private_key, message: bytes) -> str:
    """Sign message with PSS-SHA256; return base64 signature for X-Signature header."""
    sig = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def send_relay(url: str, relay_ids: list, action: str, verify_ssl: bool = False) -> None:
    relay_ids = sorted(set(int(r) for r in relay_ids))
    if action not in ("on", "off"):
        print(f"Invalid action: {action}", file=sys.stderr)
        sys.exit(1)
    message = f"relays={','.join(map(str, relay_ids))}&action={action}".encode()
    key = load_private_key()
    signature = sign_message(key, message)
    payload = {"relays": relay_ids, "action": action}
    headers = {"Content-Type": "application/json", "X-Signature": signature}
    print(f"POST {url}/relay  message={message.decode()!r}")
    try:
        r = requests.post(
            f"{url.rstrip('/')}/relay",
            json=payload,
            headers=headers,
            verify=verify_ssl,
            timeout=getattr(config, "RELAY_TIMEOUT", 5),
        )
        print(f"Status: {r.status_code}")
        print(r.text)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Error: {e}", file=sys.stderr)
        if hasattr(e, "response") and e.response is not None:
            print(e.response.text, file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Send signed relay command to relay server")
    ap.add_argument("--relays", type=int, nargs="+", required=True, help="Relay IDs (e.g. 1 2 5)")
    ap.add_argument("--action", choices=("on", "off"), required=True, help="on or off")
    ap.add_argument("--url", default="https://localhost:8080", help="Relay server base URL (default: https://localhost:8080)")
    ap.add_argument("--verify-ssl", action="store_true", help="Verify SSL cert (default: False for self-signed)")
    args = ap.parse_args()
    send_relay(args.url, args.relays, args.action, verify_ssl=args.verify_ssl)


if __name__ == "__main__":
    main()
