"""Minimal CLI example: stream this PC's audio to one AirPlay speaker.

Usage:
    python examples/stream_cli.py 192.168.1.50 [--seconds 30] [--volume 30]

Runs until Ctrl+C (or --seconds). Uses the same live capture source as the
tray app.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pyatv
import pyatv.protocols.raop as raop_pkg
from airplay_tray import LivePCMSource


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("host", help="IP address of the AirPlay speaker")
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds")
    p.add_argument("--volume", type=float, default=30, help="volume 0-100")
    args = p.parse_args()

    loop = asyncio.get_running_loop()
    confs = await pyatv.scan(loop, hosts=[args.host], timeout=5)
    if not confs:
        print(f"No AirPlay device found at {args.host}")
        return

    holder = {}

    async def open_src(file, sr, ch, ss):
        holder["src"] = LivePCMSource(sr, ch, ss)
        return holder["src"]

    raop_pkg.open_source = open_src

    atv = await pyatv.connect(confs[0], loop)
    try:
        await atv.audio.set_volume(args.volume)
    except Exception:
        pass
    task = asyncio.ensure_future(atv.stream.stream_file("live"))
    print(f"Streaming to {confs[0].name} - Ctrl+C to stop")
    try:
        if args.seconds > 0:
            await asyncio.sleep(args.seconds)
        else:
            await task
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if "src" in holder:
            await holder["src"].close()
        if not task.done():
            try:
                await asyncio.wait_for(task, 6)
            except BaseException:
                task.cancel()
        pending = atv.close()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
