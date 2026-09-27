# AirPlay Tray

Stream your Windows PC's audio to AirPlay speakers, straight from the system
tray — **including AirPlay 2 speakers that require pairing**, which the classic
Windows tools can't connect to.

Play Spotify, YouTube, games — whatever comes out of your PC — on one or
several AirPlay speakers at once.

## Why this exists

A lot of modern AirPlay 2 speakers (Xiaomi Sound Pro and friends) refuse
old-style AirPlay senders: the legacy handshake gets `403 Forbidden` at SETUP
because the device demands AirPlay 2 pairing first. That's why tools like
TuneBlade show the speaker but never connect, and Airfoil for Windows was
discontinued years ago.

AirPlay Tray uses [pyatv](https://pyatv.dev), which speaks modern AirPlay 2 and
performs the pairing — so those speakers just work.

## Features

- Lives in the system tray; **click a speaker to start, click again to stop**
- Auto-discovers AirPlay / AirPlay 2 receivers on your network (Bonjour not required)
- Stream to **multiple speakers at once**
- **Per-speaker volume** control from the menu
- **Adjustable latency** — down to ~0.1s experimental (v0.2 was ~2s)
- **Mutes your PC/headphones while streaming** (restored when you stop)
- Remembers volumes, and can optionally **resume your last speakers on launch**
- Auto-retries flaky connections once before complaining, with clear
  notifications when something is actually wrong
- Single portable `.exe` — nothing to install, no Python needed

## Download

Grab `AirPlayTray.zip` from the
[latest release](https://github.com/bleidzen/airplay-tray/releases/latest),
unzip, and run `AirPlayTray.exe`.

Two things on first run (the exe is not code-signed):

1. **SmartScreen**: "Windows protected your PC" → *More info* → *Run anyway*.
2. **Windows Firewall** will ask for permission the first time you start a
   stream — tick **Private networks** and *Allow access*. AirPlay needs the
   speaker to talk back to your PC; without this, connections fail.

## Usage

1. Run `AirPlayTray.exe` — a speaker icon appears in the tray
   (bottom-right; check under the `^` overflow arrow).
2. **Right-click** the icon and click a speaker — the icon turns green and
   your PC's audio plays there.
3. Click more speakers to add them, use **Volume** for per-speaker levels,
   **Stop all** to go quiet, and **Start last speakers on launch** if you want
   it to reconnect automatically next time.

Good to know:

- It streams your **default output device** — whatever Windows plays. If you
  switch output device, stop and start the speaker again.
- **Latency** menu sets how much the speaker buffers: *Low (0.5s)* by default,
  *Ultra low (0.25s)* if your Wi-Fi is solid, *Experimental (0.1s)* if your
  speaker accepts it, *Safe (1.5s)* if you hear
  dropouts. AirPlay can't do zero latency, so for video use your player's
  audio-delay setting to line things up.
- Multiple speakers run as independent streams, so they can drift slightly
  apart — great across rooms, less ideal side by side. (For sample-accurate
  sync, group/stereo-pair the speakers in their vendor app so they appear as
  one AirPlay target.)
- Speakers that require an AirPlay **password** aren't supported yet.

## Troubleshooting

- **Speaker connects then drops immediately** → almost always the firewall.
  Remove and re-allow the app, or check
  *Windows Security → Firewall → Allow an app*.
- **No speakers found** → *Rescan speakers*. Make sure the PC and speakers are
  on the same network/subnet, and that your VPN routes local traffic directly
  (most do by default).
- **Anything else** → *Open log* in the tray menu shows what happened
  (`%TEMP%\airplay_tray.log`). Please attach it to a GitHub issue.

## Run / build from source

```powershell
py -3.12 -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python airplay_tray.py            # run from source

venv\Scripts\pip install pyinstaller           # to build the exe
powershell -ExecutionPolicy Bypass -File build_exe.ps1   # -> dist\AirPlayTray.exe
```

There's also a minimal CLI example in [`examples/stream_cli.py`](examples/stream_cli.py),
and a headless end-to-end check: `AirPlayTray.exe --selftest` (writes
`%TEMP%\airplay_selftest.log`).

## How it works

Windows audio is captured with a WASAPI **loopback** recorder (via
[soundcard](https://github.com/bastibe/SoundCard)) — no virtual audio cable
needed and you keep hearing sound locally. The raw PCM is fed to
[pyatv](https://pyatv.dev) through a custom `AudioSource`, so a momentary
capture hiccup just pauses the stream instead of ending it. pyatv handles
discovery (zeroconf), AirPlay 2 pairing, and the RAOP/RTSP streaming.

## Credits & license

MIT — see [LICENSE](LICENSE). Built on
[pyatv](https://github.com/postlund/pyatv) (MIT),
[soundcard](https://github.com/bastibe/SoundCard) (BSD-3),
[pystray](https://github.com/moses-palmer/pystray) (LGPL-3.0),
[Pillow](https://python-pillow.org) and [NumPy](https://numpy.org).
Windows 10/11 only.
