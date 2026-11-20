"""Emergency halt: connect and send FTMS PAUSE (08 02), which this firmware honours
(STOP 08 01 returns OPERATION_FAILED while running). Confirms speed reaches 0."""
import asyncio, sys
from bleak import BleakClient, BleakScanner

CP = "00002ad9-0000-1000-8000-00805f9b34fb"
TD = "00002acd-0000-1000-8000-00805f9b34fb"

async def main(name="KS-AP-RQ3"):
    dev = await BleakScanner.find_device_by_name(name, timeout=15)
    if not dev:
        raise SystemExit("Device not found")
    speeds = []
    async with BleakClient(dev, timeout=20) as c:
        await c.start_notify(TD, lambda _s, d: speeds.append(int.from_bytes(d[2:4], "little") / 100))
        await asyncio.sleep(0.3)
        await c.start_notify(CP, lambda _s, d: print("CP <-", bytes(d).hex(" ")))
        await asyncio.sleep(0.3)
        await c.write_gatt_char(CP, b"\x00", response=True)  # REQUEST_CONTROL (required per connection)
        await asyncio.sleep(0.5)
        await c.write_gatt_char(CP, b"\x08\x02", response=True)
        print("sent PAUSE 08 02")
        for _ in range(16):
            await asyncio.sleep(0.5)
            if speeds and speeds[-1] == 0 and len(speeds) > 2:
                break
        print("speeds seen:", speeds)

asyncio.run(main(*sys.argv[1:]))
