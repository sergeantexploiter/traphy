# sub.py
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
        client.subscribe('test/topic')
    else:
        logging.error(f"Connect failed: {rc}")

def on_disconnect(client, userdata, rc):
    logging.warning(f"Disconnected: {rc}. Reconnecting...")
    while True:
        try:
            client.reconnect()
            break
        except:
            time.sleep(5)

def on_message(client, userdata, msg):
    try:
        print(f"Received: {msg.payload.decode()} on {msg.topic}")
    except Exception as e:
        logging.error(f"Message error: {e}")

client = mqtt.Client()
client.username_pw_set(USERNAME, PASSWORD)
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message

try:
    client.connect('localhost', 1883, 60)
except Exception as e:
    logging.error(f"Initial connect failed: {e}")
    exit(1)

client.loop_forever()