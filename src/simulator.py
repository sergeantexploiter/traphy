# simulator.py – MQTT traffic simulator for coordinator testing.
"""
Publishes synthetic vehicle and pedestrian counts over MQTT in the same format
the coordinator expects: key:value:unix_timestamp. Used to test the coordinator
without real detection hardware. Each key must match the coordinator's data keys.
"""

import argparse
import time
import random
import config
import paho.mqtt.publish as publish


# -----------------------------------------------------------------------------
# Simulated data keys and value generators
# -----------------------------------------------------------------------------
# Each entry is (key, callable). gen() is called once per batch and must return int.
# Keys must match coordinator's data dict. Weights favour non-zero values so demand appears often.
SIM_KEYS = [
    # Vehicle counts: low weight on 0, favour 1–7 so demand appears often.
    ("north_right_vehicle_count", lambda: random.choices([0, 1, 2, 3, 5, 7, 10], weights=[1, 4, 4, 3, 3, 2, 1])[0]),
    ("north_left_vehicle_count",  lambda: random.choices([0, 1, 2, 3, 4, 6, 9], weights=[1, 4, 4, 3, 2, 2, 1])[0]),
    ("south_left_vehicle_count",  lambda: random.choices([0, 1, 2, 4, 6, 8, 12], weights=[1, 4, 4, 3, 2, 2, 1])[0]),
    ("south_right_vehicle_count", lambda: random.choices([0, 1, 3, 5, 8, 10, 14], weights=[1, 4, 4, 3, 2, 2, 1])[0]),
    # Pedestrian: 65% chance of 1, else 0.
    ("pedestrian_narrow_passage_count", lambda: 1 if random.random() < 0.65 else 0),
    # Narrow centre: 80% chance of generating; when so, favour 1 and 2 (blockage) over 0.
    ("narrow_centre_vehicle_count", lambda: random.choices([0, 1, 1, 2, 2], weights=[2, 5, 4, 3, 2])[0] if random.random() < 0.8 else 0),
]


def send_one(topic, key, value, hostname, port, auth=None):
    """Publish a single key:value:timestamp message to the given MQTT topic."""
    ts = int(time.time())
    payload = f"{key}{config.MQTT_PAYLOAD_SEP}{value}{config.MQTT_PAYLOAD_SEP}{ts}"
    kwargs = {"hostname": hostname, "port": port}
    if auth:
        kwargs["auth"] = auth
    publish.single(topic, payload, **kwargs)
    print(f"→ {payload}")


def send_batch(topic, hostname, port, auth=None):
    """Publish one value for each simulated key (one batch)."""
    for key, gen in SIM_KEYS:
        send_one(topic, key, gen(), hostname, port, auth)


def main():
    """Parse CLI, optionally set random seed, then loop sending batches at random intervals."""
    ap = argparse.ArgumentParser(description="MQTT traffic simulator (vehicle/pedestrian counts)")
    ap.add_argument("--broker", default=config.MQTT_BROKER, help="MQTT broker host")
    ap.add_argument("--port", type=int, default=config.MQTT_PORT, help="MQTT broker port")
    ap.add_argument("--topic", default=config.MQTT_TOPIC, help="MQTT topic")
    ap.add_argument("--interval-min", type=float, default=1.6, help="Min seconds between sends")
    ap.add_argument("--interval-max", type=float, default=4.0, help="Max seconds between sends")
    ap.add_argument("--seed", type=int, default=None, help="Random seed for reproducible runs")
    ap.add_argument("--once", action="store_true", help="Send one batch and exit")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    auth = None
    if getattr(config, "MQTT_USERNAME", None) and getattr(config, "MQTT_PASSWORD", None):
        auth = {"username": config.MQTT_USERNAME, "password": config.MQTT_PASSWORD}

    print("MQTT traffic simulator running. Ctrl+C to stop.")
    print(f"Broker: {args.broker}:{args.port}  Topic: {args.topic}")
    if args.once:
        print("Mode: single batch then exit")

    try:
        while True:
            send_batch(args.topic, args.broker, args.port, auth)
            if args.once:
                break
            time.sleep(random.uniform(args.interval_min, args.interval_max))
    except KeyboardInterrupt:
        print("\nSimulator stopped.")


if __name__ == "__main__":
    main()