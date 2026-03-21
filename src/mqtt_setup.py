# mqtt_setup.py
# Script to run MQTT broker, publisher, or subscriber based on mode.
# Installation: Run `pip3 install paho-mqtt hbmqtt` manually first.
# Usage: python3 mqtt_setup.py broker  # or pub, sub

import sys
import asyncio
import logging
from hbmqtt.broker import Broker
import paho.mqtt.client as mqtt
import config
logging.basicConfig(level=logging.INFO)

MQTT_TOPIC = config.MQTT_TOPIC
MQTT_HOST = config.MQTT_BROKER
MQTT_PORT = config.MQTT_PORT

async def run_broker():
    config = {
        'listeners': {'default': {'type': 'tcp', 'bind': f'0.0.0.0:{MQTT_PORT}'}},
        'auth': {'allow-anonymous': True}
    }
    broker = Broker(config)
    await broker.start()

def on_connect(client, userdata, flags, rc):
    logging.info(f"Connected with result code {rc}")

def publisher():
    client = mqtt.Client()
    client.on_connect = on_connect
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    client.publish(MQTT_TOPIC, "Test count: 5")
    client.loop_stop()

def on_message(client, userdata, msg):
    logging.info(f"Received: {msg.payload.decode()} on {msg.topic}")

def subscriber():
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.subscribe(MQTT_TOPIC)
    client.loop_forever()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python mqtt_setup.py [broker|pub|sub]")
        sys.exit(1)
    mode = sys.argv[1]
    if mode == 'broker':
        asyncio.run(run_broker())
    elif mode == 'pub':
        publisher()
    elif mode == 'sub':
        subscriber()
    else:
        print("Invalid mode")