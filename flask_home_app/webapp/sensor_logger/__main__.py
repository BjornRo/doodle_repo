from ast import literal_eval
import paho.mqtt.client as mqtt
from datetime import datetime
import sqlite3
from threading import Thread, Lock
import schedule
import time
from pymemcache.client.base import PooledClient
import json


lock = Lock()

# Datastructure is in the form of:
#  devicename/measurements: for each measurement type: value.
# New value is a flag to know if value has been updated since last SQL-query. -> Each :00, :30
def main():
    main_node_data = {
        "home": {
            "bikeroom/temp": {"Temperature": -99},
            "balcony/temphumid": {"Temperature": -99, "Humidity": -99},
            "kitchen/temphumidpress": {
                "Temperature": -99,
                "Humidity": -99,
                "Airpressure": -99,
            },
        },
        "remote_sh": {
            "pizw/temp": {"Temperature": -99},
            "hydrofor/temphumidpress": {
                "Temperature": -99,
                "Humidity": -99,
                "Airpressure": -99,
            },
        },
    }
    # Associated dict to see if the values has been updated. This is to let remote nodes
    # just send data and then you can decide at the main node.
    main_node_new_values = {
        sub_node: {device: False for device in sub_node_data}
        for sub_node, sub_node_data in main_node_data.items()
    }

    memcache_local = PooledClient("memcached:11211", serde=JSerde(), max_pool_size=3)

    # Set initial values.
    memcache_local.set("weather_data_home", main_node_data["home"])
    memcache_local.set("weather_data_remote_sh", main_node_data["remote_sh"])

    Thread(
        target=mqtt_agent,
        args=(
            main_node_data["home"],
            main_node_new_values["home"],
            memcache_local,
        ),
        daemon=True,
    ).start()
    Thread(
        target=remote_fetcher,
        args=(
            main_node_data["remote_sh"],
            main_node_new_values["remote_sh"],
            memcache_local,
            "remote_sh",
        ),
        daemon=True,
    ).start()
    schedule_setup(main_node_data, main_node_new_values)

    # Poll tmpdata until all Nones are gone.
    while 1:
        time.sleep(1)
        for sub_node_values in main_node_data["home"].values():
            if -99 in sub_node_values.values():
                break
        else:
            break

    while 1:
        schedule.run_pending()
        time.sleep(10)


def remote_fetcher(sub_node_data, sub_node_new_values, memcache, rem_key):
    from bmemcached import Client
    import configparser
    import pathlib

    def test_values_and_compare(value1, value2):
        # Get the latest value from two sources.. woohoo
        # Test every possiblity...Try catch to reduce if statements.
        try:
            if value1 is None:
                value = json.loads(value2)
            else:
                if value2 is None:
                    value = json.loads(value1)
                else:
                    value1 = json.loads(value1)
                    value2 = json.loads(value2)
                    try:
                        t1 = datetime.fromisoformat(value1.pop(-1))
                        try:
                            t2 = datetime.fromisoformat(value2.pop(-1))
                            return (*value1, t1) if t1 >= t2 else (*value2, t2)
                        except:
                            return (*value1, t1)
                    except:
                        value = value2
                return (*value[:2], datetime.fromisoformat(value[2]))
        except:
            pass
        return None

    # This could be a great use of asyncio... Maybe when I understand it for a later project.
    cfg = configparser.ConfigParser()
    cfg.read(pathlib.Path(__file__).parent.absolute() / "config.ini")
    memcachier1 = Client((cfg["DATA"]["server"],), cfg["DATA"]["user"], cfg["DATA"]["pass"])
    memcachier2 = Client((cfg["DATA2"]["server"],), cfg["DATA2"]["user"], cfg["DATA2"]["pass"])

    count_error = 0
    last_update_remote = {key: datetime.now() for key in sub_node_data}
    last_update_memcachier = datetime.now()  # Just as init
    while 1:
        time.sleep(15)
        value = test_values_and_compare(memcachier1.get(rem_key), memcachier2.get(rem_key))
        if value is None:
            count_error += 1
            if count_error >= 20:
                # Set to invalid values after n attempts. Reset counter.
                count_error = 0
                with lock:
                    for device, device_data in sub_node_data.items():
                        for key in device_data:
                            sub_node_data[device][key] = -99
                            sub_node_new_values[device] = False
            continue

        # At this point valid value or exception thrown.
        memcache.set("remote_sh_rawdata", (*value[:2], value[2].isoformat()))
        if value[2] >= last_update_memcachier:
            last_update_memcachier = value.pop(-1)
            count_error = 0

            new_sub_node_data, data_time = value
            for device, device_data in new_sub_node_data.items():
                tmpdict = {}
                # Test if new updatetime is newer than last. Otherwise continue.
                try:
                    updatetime = datetime.fromisoformat(data_time[device])
                except:
                    continue
                if updatetime >= last_update_remote[device]:
                    for key, value in device_data.items():
                        if key in sub_node_data[device] and _test_value(key, value * 100):
                            tmpdict[key] = value
                        else:
                            break
                    else:
                        memcache.set(f"weather_data_{rem_key}", sub_node_data)
                        last_update_remote[device] = updatetime
                        with lock:
                            sub_node_data[device].update(tmpdict)
                            sub_node_new_values[device] = True


# mqtt function does all the heavy lifting sorting out bad data.
def schedule_setup(main_node_data: dict, main_node_new_values: dict):
    def querydb():
        # Check if there exist any values that should be queried. To reduce as much time with lock.
        update_node = []
        for sub_node, new_values in main_node_new_values.items():
            if any(new_values.values()):
                update_node.append(sub_node)

        if not update_node:
            return

        # Copy data and set values to false.
        new_data = {}
        with lock:
            for sub_node in update_node:
                new_data[sub_node] = []
                for device, new_value in main_node_new_values[sub_node].items():
                    if new_value:
                        main_node_new_values[sub_node][device] = False
                        new_data[sub_node].append((device, main_node_data[sub_node][device].copy()))
        time_now = datetime.now().isoformat("T", "minutes")
        cursor = db.cursor()
        cursor.execute(f"INSERT INTO Timestamp VALUES ('{time_now}')")
        for sub_node_data in new_data.values():
            for device, device_data in sub_node_data:
                mkey = device.split("/")[0]
                for table, value in device_data.items():
                    cursor.execute(f"INSERT INTO {table} VALUES ('{mkey}', '{time_now}', {value})")
        db.commit()
        cursor.close()

    db = sqlite3.connect("/db/main_db.db")
    schedule.every().hour.at(":30").do(querydb)
    schedule.every().hour.at(":00").do(querydb)


def mqtt_agent(h_tmpdata: dict, h_new_values: dict, memcache, status_path="balcony/relay/status"):
    def on_connect(client, *_):
        for topic in list(h_tmpdata.keys()) + [status_path]:
            client.subscribe("home/" + topic)

    def on_message(client, userdata, msg):
        # Get values into a listlike form.
        try:
            listlike = literal_eval(msg.payload.decode("utf-8"))
            if isinstance(listlike, dict):
                listlike = tuple(listlike.values())
            elif not (isinstance(listlike, tuple) or isinstance(listlike, list)):
                listlike = (listlike,)
        except:
            return

        # Handle the topic depending on what it is about.
        topic = msg.topic.replace("home/", "")
        if status_path == topic:
            if not set(listlike).difference(set((0, 1))) and len(listlike) == 4:
                memcache.set("relay_status", listlike)
            return

        if len(listlike) != len(h_tmpdata[topic]):
            return

        tmpdict = {}
        for key, value in zip(h_tmpdata[topic].keys(), listlike):
            # If a device sends bad data -> break and discard, else update
            if not _test_value(key, value):
                break
            tmpdict[key] = value / 100
        else:
            memcache.set("weather_data_home", h_tmpdata)
            with lock:
                h_tmpdata[topic].update(tmpdict)
                h_new_values[topic] = True

    # Setup and connect mqtt client. Return client object.
    client = mqtt.Client("br_logger")
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            if client.connect("www.home", 1883, 60) == 0:
                break
        except:
            pass
        time.sleep(5)
    client.loop_forever()


def _test_value(key, value) -> bool:
    try:
        if key == "Temperature":
            return -5000 <= value <= 6000
        elif key == "Humidity":
            return 0 <= value <= 10000
        elif key == "Airpressure":
            return 90000 <= value <= 115000
    except:
        pass
    return False


# setup memcache
class JSerde(object):
    def serialize(self, key, value):
        return json.dumps(value), 2


if __name__ == "__main__":
    main()
