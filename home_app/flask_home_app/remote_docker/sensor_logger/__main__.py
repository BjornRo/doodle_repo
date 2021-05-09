from datetime import datetime, timedelta
from json import dumps as jsondumps
from glob import glob
from configparser import ConfigParser
from pathlib import Path
from bmemcached import Client as mClient
from time import sleep

# from jsonpickle import encode as jpencode
# from zlib import compress
import tarfile
from io import BytesIO
from ssl import SSLContext, PROTOCOL_TLSv1_2

import asyncio
from asyncio_mqtt import Client
from aiosqlite import connect as dbconnect
from aiofiles import open as async_open
from aiohttp import web

# Ugly imports, premature optimization perhaps. Whatever to make pizw fasterish.

DEV_NAME = "remote_sh"
DB_FILE = f"{DEV_NAME}.db"
DB_FILEPATH = "/db/" + DB_FILE


def main():
    cfg = ConfigParser()
    cfg.read(Path(__file__).parent.absolute() / "config.ini")

    # SSL Context
    sslpath = f'/etc/letsencrypt/live/{cfg["CERT"]["url"]}/'

    # Defined read only global variables
    # Find the device file to read from.
    file_addr = glob("/sys/bus/w1/devices/28*")[0] + "/w1_slave"
    # To stop subscribing to non-existing devices.
    sub_denylist = ("pizw/temp",)

    while 1:
        # Datastructure is in the form of:
        #  devicename/measurements: for each measurement type: value.
        # New value is a flag to know if value has been updated since last SQL-query. -> Each :00, :30
        tmpdata = {
            "pizw/temp": {"Temperature": -99},
            "hydrofor/temphumidpress": {
                "Temperature": -99,
                "Humidity": -99,
                "Airpressure": -99,
            },
        }
        new_values = {key: False for key in tmpdata}  # For DB-query
        last_update = {key: None for key in tmpdata}  # For main node to know when sample was taken.

        # asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(mqtt_agent(sub_denylist, tmpdata, new_values, last_update))
            loop.create_task(read_temp(file_addr, tmpdata, new_values, "pizw/temp", last_update))
            loop.create_task(querydb(tmpdata, new_values))
            loop.create_task(memcache_as(cfg, tmpdata, last_update))
            loop.create_task(low_lvl_http((tmpdata, last_update), cfg["GETDATA"]["token"], sslpath))
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            finally:
                loop.close()
                sleep(20)
                asyncio.set_event_loop(asyncio.new_event_loop())


# Simple server to get data with HTTP. Trial to eventually replace memcachier.
async def low_lvl_http(tmpdata_last_update, token, sslpath=None):
    query = """SELECT t.time, htemp, humid, press, ptemp
FROM Timestamp t
LEFT OUTER JOIN
(SELECT time, temperature AS htemp
FROM Temperature
WHERE measurer = 'hydrofor') a ON t.time = a.time
LEFT OUTER JOIN
(SELECT time, humidity As humid
FROM Humidity
WHERE measurer = 'hydrofor') b ON t.time = b.time
LEFT OUTER JOIN
(SELECT time, airpressure AS press
FROM Airpressure
WHERE measurer = 'hydrofor') c ON t.time = c.time
LEFT OUTER JOIN
(SELECT time, temperature AS ptemp
FROM Temperature
WHERE measurer = 'pizw') d ON t.time = d.time"""

    last_request = [datetime.now()] * 2
    last_data = ["null"] * 2
    columns = ("time", "htemp", "humid", "press", "ptemp")

    # Adds 30 min to "cache" the data.
    def update_data(method_idx: int, request_time: list, delta: int = 30) -> bool:
        time_now = datetime.now()
        if boolean := request_time[method_idx] < time_now:
            request_time[method_idx] = time_now + timedelta(minutes=delta)
        return boolean

    async def handler(request: web.Request):
        try:
            rel_url = str(request.rel_url)[1:].split("/")
            if "status" == rel_url[0] and "get" == rel_url[1]:
                return web.json_response(tmpdata_last_update)
            # Can't decide on query vs sending the file. Just have both ready for usage.
            if token == rel_url[0]:
                if "query" == rel_url[1]:
                    if update_data(0, last_request):
                        async with dbconnect(DB_FILEPATH) as db:
                            async with db.execute(query) as c:
                                last_data[0] = jsondumps((columns, await c.fetchall()))
                    return web.Response(text=last_data[0])
                if "file" == rel_url[1]:
                    if update_data(1, last_request):
                        async with async_open(DB_FILEPATH, "rb") as f:
                            source = BytesIO(await f.read())
                        tardb = BytesIO()
                        with tarfile.open(fileobj=tardb, mode="w:gz") as tar:
                            info = tarfile.TarInfo(DB_FILE)
                            info.size = source.seek(0, 2)
                            source.seek(0)
                            tar.addfile(info, source)
                        last_data[1] = tardb.getvalue()
                    return web.Response(
                        body=last_data[1],
                        content_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename={DEV_NAME}.tar.gz"},
                    )
        except:
            pass
        return web.Response(status=500)

    sslpath_tuple = (sslpath + "fullchain.pem", sslpath + "privkey.pem")
    ssl = SSLContext(PROTOCOL_TLSv1_2)
    ssl.load_cert_chain(*sslpath_tuple)

    runner = web.ServerRunner(web.Server(handler))
    await runner.setup()
    site = web.TCPSite(runner, None, 42660, ssl_context=ssl)
    await site.start()
    # Reload ssl every day.
    while 1:
        await asyncio.sleep(86400)
        ssl.load_cert_chain(*sslpath_tuple)


async def memcache_as(cfg, tmpdata, last_update):
    loop = asyncio.get_event_loop()
    m1 = mClient((cfg["DATA"]["server"],), cfg["DATA"]["user"], cfg["DATA"]["pass"])
    m2 = mClient((cfg["DATA2"]["server"],), cfg["DATA2"]["user"], cfg["DATA2"]["pass"])

    def memcache():
        # Get the data, don't care about race conditions.
        data = jsondumps((tmpdata, last_update, datetime.now().isoformat()))
        m1.set(DEV_NAME, data)
        m2.set(DEV_NAME, data)

    while 1:
        await asyncio.sleep(10)
        try:
            await loop.run_in_executor(None, memcache)
        except:
            pass


async def mqtt_agent(sub_denylist, tmpdata, new_values, last_update):
    # Since mqtt_agent is async, thus this is sync, no race conditions.
    #  -> Either MQTT or SQL, but not both.
    def on_message(message):
        # Payload is in form of bytes.
        try:
            msg = message.payload
            # Check if string has ( and ) or [ and ]
            if (msg[0] == 40 and msg[-1] == 41) or (msg[0] == 91 and msg[-1] == 93):
                listlike = tuple(map(int, msg[1:-1].split(b",")))
            elif msg.isdigit():  # A number.
                listlike = (int(msg),)
            else:  # dict
                # listlike = json.loads(msg) #can't handle json...
                return
            # Handle the topic depending on what it is about.
            topic = message.topic[7:]  # Topic is decoded with utf8
            if len(listlike) != len(tmpdata[topic]):
                return

            for key, value in zip(tmpdata[topic], listlike):
                # If a device sends bad data break and don't set flag as newer value.
                if not _test_value(key, value):
                    break
                tmpdata[topic][key] = value / 100
            else:
                new_values[topic] = True
                last_update[topic] = datetime.now().isoformat()
        except:  # Unsupported datastructures or invalid values
            return

    while 1:
        try:
            async with Client("192.168.1.200") as client:
                for topic in tmpdata:
                    if topic not in sub_denylist:
                        await client.subscribe("landet/" + topic)
                async with client.unfiltered_messages() as messages:
                    async for message in messages:
                        on_message(message)
        except:
            pass
        await asyncio.sleep(5)


async def querydb(tmpdata: dict, new_values: dict):
    while 1:
        # Old algo ((1800 - dt.minute * 60 - 1) % 1800) - dt.second + 1

        # Get time to sleep. Expensive algorithm, but queries are few.
        dt = datetime.now()
        # This will always get next 30minutes. If time is 00:00, then 00:30...
        nt = dt.replace(second=0, microsecond=0) + timedelta(minutes=(30 - dt.minute - 1) % 30 + 1)
        await asyncio.sleep((nt - dt).total_seconds())
        # If timer gone too fast and there are seconds left, wait the remaining time, else continue.
        if (remain := (nt - datetime.now()).total_seconds()) > 0:
            await asyncio.sleep(remain)
        if not any(new_values.values()):
            continue
        try:
            # Copy values because we don't know how long time the queries will take.
            # Async allows for mutex since we explicit tells it when it's ok to give control to the event loop.
            tmplist = []
            for key, value in new_values.items():
                if value:
                    new_values[key] = False
                    tmplist.append((key, tmpdata[key].copy()))
            # Convert nt to a string. Overwrite the old variable since it won't be used until next loop.
            nt = nt.isoformat("T", "minutes")
            async with dbconnect(DB_FILEPATH) as db:
                await db.execute(f"INSERT INTO Timestamp VALUES ('{nt}')")
                for measurer, data in tmplist:
                    mkey = measurer.partition("/")[0]
                    for tb, val in data.items():
                        await db.execute(f"INSERT INTO {tb} VALUES ('{mkey}', '{nt}', {val})")
                await db.commit()
        except:
            pass


async def read_temp(file_addr: str, tmpdata: dict, new_values: dict, topic: str, last_update: dict):
    while 1:
        found = False
        try:
            async with async_open(file_addr, "r") as f:
                async for line in f:
                    line = line.strip()
                    if not found and line[-3:] == "YES":
                        found = True
                        continue
                    elif found and (eq_pos := line.find("t=")) != -1:
                        if (tmp_val := line[eq_pos + 2 :]).isdigit():
                            conv_val = round(int(tmp_val) / 1000, 1)  # 28785 -> 28.785 -> 28.8
                            if _test_value("Temperature", conv_val, 100):
                                tmpdata[topic]["Temperature"] = conv_val
                                new_values[topic] = True
                                last_update[topic] = datetime.now().isoformat()
                    break
        except:
            pass
        await asyncio.sleep(4)


# Simplified to try catch since we want to compare numbers and not another datatype.
# Less computation for a try/except block than isinstance...
def _test_value(key, value, magnitude=1) -> bool:
    try:
        value *= magnitude
        if key == "Temperature":
            return -5000 <= value <= 6000
        elif key == "Humidity":
            return 0 <= value <= 10000
        elif key == "Airpressure":
            return 90000 <= value <= 115000
    except:
        pass
    return False


if __name__ == "__main__":
    main()