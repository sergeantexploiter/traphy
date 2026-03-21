# pub.py
import paho.mqtt.client as mqtt
import logging
import time

import config

logging.basicConfig(level=logging.INFO)

USERNAME = config.MQTT_USERNAME
PASSWORD = config.MQTT_PASSWORD

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info("Connected successfully")
    else:
        logging.error(f"Connect failed: {rc}")

client = mqtt.Client()
client.username_pw_set(USERNAME, PASSWORD)
client.on_connect = on_connect

try:
    client.connect('localhost', 1883, 60)
except Exception as e:
    logging.error(f"Connect failed: {e}")
    exit(1)

client.loop_start()
time.sleep(1)  # Wait for connect

try:
    info = client.publish('test/topic', 'Hello from pub')
    info.wait_for_publish()
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise ValueError(f"Publish failed: {info.rc}")
except Exception as e:
    logging.error(f"Publish error: {e}")

client.disconnect()
client.loop_stop()