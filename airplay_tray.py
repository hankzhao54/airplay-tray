"""AirPlay Tray - stream this PC's audio to one or more AirPlay speakers.

System-tray app: right-click the icon and click speakers to toggle streaming to
them (you can enable several at once). Streams the PC's default-output audio via
pyatv, including to AirPlay 2 devices that require pairing. Note: multiple
speakers run as independent streams, so they may drift slightly out of sync
(fine for different rooms).

Logs to %TEMP%\\airplay_tray.log. Build a standalone exe with build_exe.ps1.
"""
import asyncio
import atexit
import concurrent.futures
import ctypes
import json
import logging
import os
import sys
import threading
import time
import webbrowser

import numpy as np
import soundcard as sc
import pyatv
import pyatv.protocols.raop as raop_pkg
from pyatv.const import Protocol
from pyatv.protocols.raop.protocols import StreamContext
from pyatv.support.rtsp import RtspSession
from pyatv.protocols.raop.audio_source import AudioSource
from pyatv.support.metadata import EMPTY_METADATA

from PIL import Image, ImageDraw
import pystray

APP_NAME = "AirPlay Tray"
APP_VERSION = "0.4.0"
REPO_URL = "https://github.com/bleidzen/airplay-tray"
DEFAULT_VOLUME = 30
# Receiver-side playback buffer. pyatv hardcodes ~1.5s; AirPlay 2 receivers
# advertise latencyMin=11025 frames (0.25s), so lower values usually work.
LATENCY_PRESETS = [(100, "Experimental (0.1s)"),
                   (250, "Ultra low (0.25s)"), (500, "Low (0.5s)"),
                   (1000, "Normal (1s)"), (1500, "Safe (1.5s, pyatv default)")]
DEFAULT_LATENCY_MS = 500
_latency_ms = DEFAULT_LATENCY_MS
CONFIG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                      "AirPlayTray", "config.json")
LOGPATH = os.path.join(os.environ.get("TEMP", "."), "airplay_tray.log")

logging.basicConfig(filename=LOGPATH, level=logging.INFO, filemode="w",
                    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
log = logging.getLogger("tray")
sys.excepthook = lambda *a: log.error("uncaught exception", exc_info=a)
if hasattr(threading, "excepthook"):
    threading.excepthook = lambda a: log.error(
        "thread exception", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


# ----------------------------- latency patch --------------------------------
def _latency_frames(sample_rate):
    # floor: two RTP packets (352 frames each)
    return max(704, int(sample_rate * _latency_ms / 1000))


_orig_ctx_init = StreamContext.__init__
_orig_ctx_reset = StreamContext.reset


def _ctx_init(self, *a, **kw):
    _orig_ctx_init(self, *a, **kw)
    self.latency = _latency_frames(self.sample_rate)


def _ctx_reset(self, *a, **kw):
    _orig_ctx_reset(self, *a, **kw)
    self.latency = _latency_frames(self.sample_rate)


StreamContext.__init__ = _ctx_init
StreamContext.reset = _ctx_reset

# AirPlay 2 SETUP tells the receiver latencyMin=11025 (0.25s); lower it when the
# user picks something smaller so the speaker is allowed to buffer less.
_orig_rtsp_setup = RtspSession.setup


async def _rtsp_setup(self, headers=None, body=None):
    if isinstance(body, dict):
        for st in body.get("streams", []) or []:
            if isinstance(st, dict) and "latencyMin" in st:
                st["latencyMin"] = min(st["latencyMin"],
                                       _latency_frames(st.get("sr", 44100)))
    return await _orig_rtsp_setup(self, headers=headers, body=body)


RtspSession.setup = _rtsp_setup


def set_latency_ms(ms):
    global _latency_ms
    _latency_ms = int(ms)


# ----------------------------- audio source --------------------------------
class LivePCMSource(AudioSource):
    """Feeds pyatv raw PCM captured from the default output (WASAPI loopback).

    A momentary capture underrun pauses briefly instead of ending the stream.
    """

    def __init__(self, sample_rate, channels, sample_size):
        self._sr = int(sample_rate)
        self._ch = int(channels)
        self._ss = int(sample_size)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stopped = False
        self._loop = asyncio.get_running_loop()
        self._ev = asyncio.Event()
        threading.Thread(target=self._capture, daemon=True, name="capture").start()

    def _capture(self):
        try:
            spk = sc.default_speaker()
            try:
                mic = sc.get_microphone(str(spk.id), include_loopback=True)
            except Exception:
                mic = next(m for m in sc.all_microphones(include_loopback=True)
                           if getattr(m, "isloopback", False))
            log.info("capturing loopback of %s @ %sHz x%s", spk.name, self._sr, self._ch)
            with mic.recorder(samplerate=self._sr, channels=self._ch) as rec:
                # ~10ms chunks keep capture->send latency small
                chunk = max(256, self._sr // 100)
                while not self._stopped:
                    data = rec.record(numframes=chunk)
                    # float32 [-1,1] -> big-endian s16 (what pyatv puts on the wire)
                    pcm = np.clip(data * 32767.0, -32768, 32767).astype(">i2").tobytes()
                    with self._lock:
                        self._buf.extend(pcm)
                        # bound capture-side backlog (clock drift / stalls) so it
                        # never eats a big share of the AirPlay buffer
                        secs = min(0.12, max(0.04, _latency_ms / 2000))
                        cap = int(self._sr * self._ch * self._ss * secs)
                        if len(self._buf) > cap:
                            del self._buf[:len(self._buf) - cap]
                    self._loop.call_soon_threadsafe(self._ev.set)
        except Exception:
            log.exception("capture failed")
            self._stopped = True
            self._loop.call_soon_threadsafe(self._ev.set)

    async def readframes(self, nframes):
        need = nframes * self._ss * self._ch
        while True:
            with self._lock:
                have = len(self._buf)
            if have >= need or self._stopped:
                break
            self._ev.clear()
            with self._lock:
                if len(self._buf) >= need:
                    break
            try:
                await asyncio.wait_for(self._ev.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        with self._lock:
            n = min(need, len(self._buf))
            data = bytes(self._buf[:n])
            del self._buf[:n]
        return data

    async def close(self):
        self._stopped = True
        self._loop.call_soon_threadsafe(self._ev.set)

    async def get_metadata(self):
        return EMPTY_METADATA

    @property
    def sample_rate(self):
        return self._sr

    @property
    def channels(self):
        return self._ch

    @property
    def sample_size(self):
        return self._ss

    @property
    def duration(self):
        return 0


# ----------------------------- local mute ----------------------------------
class LocalMuter:
    """Mutes the PC's default output while streaming, restores it afterwards.

    WASAPI loopback captures before the endpoint mute, so the AirPlay speakers
    keep playing while headphones/PC speakers go quiet. All COM calls run on
    one dedicated thread.
    """

    def __init__(self):
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="com", initializer=self._com_init)
        self._ev = None          # endpoint we muted
        self._prev = None        # its mute state before we touched it

    @staticmethod
    def _com_init():
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            log.exception("CoInitialize failed")

    @staticmethod
    def _endpoint():
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        dev = AudioUtilities.GetSpeakers()
        ev = getattr(dev, "EndpointVolume", None)
        if ev is None:   # older pycaw returns a raw IMMDevice
            iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
            ev = iface.QueryInterface(IAudioEndpointVolume)
        return ev

    def _mute(self):
        if self._ev is not None:
            return
        try:
            ev = self._endpoint()
            self._prev = bool(ev.GetMute())
            if not self._prev:
                ev.SetMute(1, None)
            self._ev = ev
            log.info("local output muted (was muted: %s)", self._prev)
        except Exception:
            log.exception("local mute failed")

    def _restore(self):
        if self._ev is None:
            return
        try:
            if not self._prev:
                self._ev.SetMute(0, None)
            log.info("local output restored")
        except Exception:
            log.exception("local unmute failed")
        self._ev = None
        self._prev = None

    def _force_unmute(self):
        try:
            self._endpoint().SetMute(0, None)
            log.info("unmuted output left muted by a previous run")
        except Exception:
            log.exception("recovery unmute failed")

    def mute(self):
        return self._pool.submit(self._mute)

    def restore(self):
        return self._pool.submit(self._restore)

    def force_unmute(self):
        return self._pool.submit(self._force_unmute)

    @property
    def muted(self):
        return self._ev is not None


# ----------------------------- controller ----------------------------------
class Session:
    def __init__(self, ident, name):
        self.ident = ident
        self.name = name
        self.atv = None
        self.task = None
        self.state = "Connecting"   # Connecting | Streaming
        self.started = time.monotonic()
        self.retries = 0


class Streamer:
    """Owns an asyncio loop in a background thread and drives pyatv sessions."""

    def __init__(self, on_change, on_error=None):
        self.on_change = on_change
        self.on_error = on_error or (lambda msg: None)
        self.loop = asyncio.new_event_loop()
        self.loop.set_exception_handler(
            lambda loop, ctx: log.error("loop error: %s", ctx.get("message"),
                                        exc_info=ctx.get("exception")))
        threading.Thread(target=self._run, daemon=True, name="asyncio").start()
        self.devices = []               # [(name, ident, address)]
        self.sessions = {}              # ident -> Session
        self.scanning = False
        self._confs = {}                # ident -> pyatv conf (for fast connect)
        self._sources_by_task = {}      # task -> LivePCMSource
        self._retry_handles = {}        # ident -> TimerHandle for pending retry
        self._resumed = False           # auto-resume ran (one-shot per launch)
        cfg = load_config()
        self.volumes = {str(k): int(v) for k, v in cfg.get("volumes", {}).items()}
        self.last = [tuple(x) for x in cfg.get("last", []) if len(x) == 2]
        self.resume = bool(cfg.get("resume", False))
        self.latency_ms = int(cfg.get("latency_ms", DEFAULT_LATENCY_MS))
        set_latency_ms(self.latency_ms)
        self.mute_local = bool(cfg.get("mute_local", True))
        self.muter = LocalMuter()
        self._muted_flag = False
        if cfg.get("muted_by_us"):
            # last run ended (crash?) while it had the PC muted - undo that
            self.muter.force_unmute()
            self._save_config()
        raop_pkg.open_source = self._open_source

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _notify(self):
        self._sync_mute()
        try:
            self.on_change()
        except Exception:
            log.exception("on_change failed")

    def _sync_mute(self):
        want = self.mute_local and bool(self.sessions)
        if want and not self._muted_flag:
            self._muted_flag = True
            self.muter.mute()
            self._save_config()
        elif not want and self._muted_flag:
            self._muted_flag = False
            self.muter.restore()
            self._save_config()

    def toggle_mute_local(self):
        self.mute_local = not self.mute_local
        self._save_config()
        self._notify()

    def shutdown_mute(self):
        """Blocking restore used at quit."""
        self._muted_flag = False
        try:
            self.muter.restore().result(timeout=3)
        except Exception:
            log.exception("restore on quit failed")
        self._save_config()

    def _error(self, msg):
        log.error(msg)
        try:
            self.on_error(msg)
        except Exception:
            log.exception("on_error failed")

    def _save_config(self):
        save_config({"volumes": self.volumes,
                     "last": [list(x) for x in self.last],
                     "resume": self.resume,
                     "latency_ms": self.latency_ms,
                     "mute_local": self.mute_local,
                     "muted_by_us": self._muted_flag})

    async def _open_source(self, file, sr, ch, ss):
        src = LivePCMSource(sr, ch, ss)
        t = asyncio.current_task()
        if t is not None:
            self._sources_by_task[t] = src
        return src

    # ---- scan ----
    async def _scan(self):
        confs = await pyatv.scan(self.loop, timeout=4)
        out = []
        for c in confs:
            if c.get_service(Protocol.RAOP) or c.get_service(Protocol.AirPlay):
                ident = str(c.identifier)
                self._confs[ident] = c
                out.append((c.name, ident, str(c.address)))
        return out

    def scan(self):
        log.info("scan requested")
        self.scanning = True
        self._notify()

        def done(fut):
            self.scanning = False
            try:
                self.devices = fut.result()
                log.info("scan found %d: %s", len(self.devices), [d[0] for d in self.devices])
            except Exception:
                log.exception("scan failed")
                self.devices = []
            first = not self._resumed
            self._resumed = True
            self._notify()
            if first and self.resume:
                found = {d[1] for d in self.devices}
                for ident, name in self.last:
                    if ident in found and ident not in self.sessions:
                        log.info("auto-resuming %s", name)
                        self._submit(self._start_session(ident, name))
        self._submit(self._scan()).add_done_callback(done)

    # ---- state helpers (called from tray thread) ----
    def is_active(self, ident):
        return ident in self.sessions

    def active_names(self):
        return [s.name for s in list(self.sessions.values()) if s.state == "Streaming"]

    def any_connecting(self):
        return any(s.state == "Connecting" for s in list(self.sessions.values()))

    def get_volume(self, ident):
        return self.volumes.get(ident, DEFAULT_VOLUME)

    def set_volume(self, ident, vol):
        self.volumes[ident] = int(vol)
        self._save_config()
        self._notify()
        sess = self.sessions.get(ident)
        if sess and sess.atv:
            async def apply():
                try:
                    await asyncio.wait_for(sess.atv.audio.set_volume(vol), 5)
                except Exception:
                    log.warning("set_volume(%s) failed", vol, exc_info=True)
            self._submit(apply())

    def set_latency(self, ms):
        """Change receiver buffer; restarts active streams so it takes effect."""
        if int(ms) == self.latency_ms:
            return
        log.info("latency -> %dms", ms)
        self.latency_ms = int(ms)
        set_latency_ms(ms)
        self._save_config()
        self._notify()
        active = [(s.ident, s.name) for s in list(self.sessions.values())]
        if not active:
            return

        async def restart():
            for ident, _ in active:
                await self._stop_session(ident, remember=False)
            self._notify()
            await asyncio.sleep(1.0)   # speakers dislike instant re-SETUP
            for ident, name in active:
                await self._start_session(ident, name)
        self._submit(restart())

    def toggle_resume(self):
        self.resume = not self.resume
        self._save_config()
        self._notify()

    # ---- session lifecycle ----
    def toggle(self, ident, name):
        self._submit(self._toggle(ident, name))

    async def _toggle(self, ident, name):
        if ident in self.sessions:
            await self._stop_session(ident, remember=True)
        else:
            await self._start_session(ident, name)
        self._notify()

    def _aborted(self, ident, sess):
        """True if the user toggled this speaker off while we were connecting."""
        return self.sessions.get(ident) is not sess

    def _remember_last(self):
        self.last = [(s.ident, s.name) for s in list(self.sessions.values())]
        self._save_config()

    async def _close_atv(self, atv):
        try:
            pending = atv.close()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        except Exception:
            log.exception("atv close error")

    async def _start_session(self, ident, name, retries=0):
        if ident in self.sessions:
            return
        log.info("start session %s (retries=%d)", name, retries)
        sess = Session(ident, name)
        sess.retries = retries
        self.sessions[ident] = sess
        self._notify()
        try:
            # Connect using the cached scan result when we have one (instant);
            # fall back to a fresh identifier scan, and retry once on failure.
            conf = self._confs.get(ident)
            atv = None
            for _attempt in (1, 2):
                if conf is None:
                    found = await pyatv.scan(self.loop, timeout=4, identifier=ident)
                    if self._aborted(ident, sess):
                        return
                    conf = found[0] if found else None
                    if conf is not None:
                        self._confs[ident] = conf
                if conf is None:
                    break
                try:
                    atv = await asyncio.wait_for(pyatv.connect(conf, self.loop), 12)
                    break
                except Exception:
                    log.warning("connect to %s failed", name, exc_info=True)
                    conf = None       # force a fresh scan on the retry
                    if self._aborted(ident, sess):
                        return
            if self._aborted(ident, sess):
                if atv:
                    await self._close_atv(atv)
                return
            if atv is None:
                self.sessions.pop(ident, None)
                self._notify()
                self._error(f"Couldn't connect to {name}. Check that {APP_NAME} is "
                            "allowed in Windows Firewall and the speaker is on.")
                return
            sess.atv = atv
            try:
                await asyncio.wait_for(atv.audio.set_volume(self.get_volume(ident)), 5)
            except Exception:
                pass
            if self._aborted(ident, sess):
                await self._close_atv(atv)
                return
            sess.task = asyncio.ensure_future(atv.stream.stream_file("live"))
            sess.task.add_done_callback(lambda f, i=ident: self._sess_done(i, f))
            sess.state = "Streaming"
            sess.started = time.monotonic()
            log.info("streaming to %s (latency %dms)", name, self.latency_ms)
            self._remember_last()
            self._notify()
        except Exception:
            log.exception("start session %s failed", name)
            if self.sessions.get(ident) is sess:
                self.sessions.pop(ident, None)
            self._notify()
            self._error(f"Couldn't start streaming to {name} (see log).")

    def _sess_done(self, ident, fut):
        # Always release the audio source for this task, even if the session was
        # already removed (covers cancellation before/while the source registers).
        src = self._sources_by_task.pop(fut, None)
        if src:
            self._submit(src.close())
        sess = self.sessions.get(ident)
        if sess and sess.task is fut:
            exc = fut.exception() if not fut.cancelled() else None
            self.sessions.pop(ident, None)
            self._notify()
            if exc:
                log.error("session %s ended with error", sess.name, exc_info=exc)
                # pyatv connects lazily, so a firewall block surfaces as the
                # stream dying right after start - word the message accordingly.
                # Speakers also refuse back-to-back sessions sometimes, so give
                # one silent retry before bothering the user.
                if time.monotonic() - sess.started < 10:
                    if sess.retries < 1:
                        log.info("early failure on %s - retrying in 2s", sess.name)

                        def fire(ident=ident, name=sess.name, n=sess.retries + 1):
                            self._retry_handles.pop(ident, None)
                            self.loop.create_task(self._start_session(ident, name, n))

                        old = self._retry_handles.pop(ident, None)
                        if old:
                            old.cancel()
                        self._retry_handles[ident] = self.loop.call_later(2.0, fire)
                    else:
                        self._error(
                            f"Couldn't stream to {sess.name}. Check that "
                            f"{APP_NAME} is allowed in Windows Firewall (Private "
                            "networks) and the speaker is on.")
                else:
                    self._error(f"Lost connection to {sess.name}.")

    async def _stop_session(self, ident, remember=True):
        handle = self._retry_handles.pop(ident, None)
        if handle:
            handle.cancel()
        sess = self.sessions.pop(ident, None)
        if not sess:
            return
        log.info("stop session %s", sess.name)
        try:
            src = self._sources_by_task.pop(sess.task, None)
            if src:
                # Normal path: end the capture, let the stream drain out cleanly.
                # NB: the stream task may finish by raising CancelledError (a
                # BaseException) during pyatv teardown - must not let it escape.
                await src.close()
                if sess.task and not sess.task.done():
                    try:
                        await asyncio.wait_for(sess.task, timeout=6)
                    except BaseException:
                        if not sess.task.done():
                            sess.task.cancel()
            elif sess.task and not sess.task.done():
                # The stream task hasn't registered its audio source yet (still
                # setting up) - cancel it outright; _sess_done releases any
                # source that squeaked in.
                sess.task.cancel()
                try:
                    await asyncio.wait_for(sess.task, timeout=6)
                except BaseException:
                    pass
            if sess.atv:
                await self._close_atv(sess.atv)
        except Exception:
            log.exception("stop session error")
        if remember:
            self._remember_last()

    def stop_all(self, remember=True):
        log.info("stop all (remember=%s)", remember)

        async def go():
            for handle in self._retry_handles.values():
                handle.cancel()
            self._retry_handles.clear()
            for ident in list(self.sessions):
                await self._stop_session(ident, remember=False)
            if remember:
                self._remember_last()
            self._notify()
        return self._submit(go())


# ----------------------------- config --------------------------------------
def load_config():
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(d):
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        log.exception("save_config failed")


# ----------------------------- icon & status -------------------------------
def make_icon(active=False, size=64):
    """Beamed eighth-notes glyph; blue when idle, green while streaming."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = (40, 200, 90) if active else (60, 160, 230)
    d.rectangle([0.28 * s, 0.14 * s, 0.80 * s, 0.27 * s], fill=c)   # beam
    d.rectangle([0.28 * s, 0.14 * s, 0.37 * s, 0.72 * s], fill=c)   # left stem
    d.rectangle([0.71 * s, 0.14 * s, 0.80 * s, 0.72 * s], fill=c)   # right stem
    d.ellipse([0.10 * s, 0.62 * s, 0.41 * s, 0.84 * s], fill=c)     # left head
    d.ellipse([0.53 * s, 0.62 * s, 0.84 * s, 0.84 * s], fill=c)     # right head
    return img


def status_text(streamer):
    names = streamer.active_names()
    if names:
        return "Streaming -> " + ", ".join(names)
    if streamer.any_connecting():
        return "Connecting..."
    return "Stopped (click a speaker)"


# ----------------------------- tray UI -------------------------------------
def _toggle_action(streamer, ident, name):
    def cb(icon, item):
        streamer.toggle(ident, name)
    return cb


def _checked_action(streamer, ident):
    def cb(item):
        return streamer.is_active(ident)
    return cb


def _vol_action(streamer, ident, vol):
    def cb(icon, item):
        streamer.set_volume(ident, vol)
    return cb


def _vol_checked(streamer, ident, vol):
    def cb(item):
        return streamer.get_volume(ident) == vol
    return cb


def _volume_menu(streamer, ident):
    def gen():
        for v in range(10, 101, 10):
            yield pystray.MenuItem(f"{v}%", _vol_action(streamer, ident, v),
                                   checked=_vol_checked(streamer, ident, v),
                                   radio=True)
    return pystray.Menu(gen)


def _volume_root(streamer):
    def gen():
        for name, ident, addr in list(streamer.devices):
            yield pystray.MenuItem(name, _volume_menu(streamer, ident))
    return pystray.Menu(gen)


def _latency_menu(streamer):
    def gen():
        for ms, label in LATENCY_PRESETS:
            yield pystray.MenuItem(
                label, (lambda m: lambda icon, item: streamer.set_latency(m))(ms),
                checked=(lambda m: lambda item: streamer.latency_ms == m)(ms),
                radio=True)
    return pystray.Menu(gen)


def build_menu(streamer, on_rescan, on_quit):
    def items():
        yield pystray.MenuItem(status_text(streamer), None, enabled=False)
        yield pystray.Menu.SEPARATOR
        devs = list(streamer.devices)
        if devs:
            for name, ident, addr in devs:
                yield pystray.MenuItem(
                    name, _toggle_action(streamer, ident, name),
                    checked=_checked_action(streamer, ident))
        elif streamer.scanning:
            yield pystray.MenuItem("(scanning...)", None, enabled=False)
        else:
            yield pystray.MenuItem("(no speakers found - Rescan)", None, enabled=False)
        yield pystray.Menu.SEPARATOR
        if devs:
            yield pystray.MenuItem("Volume", _volume_root(streamer))
        yield pystray.MenuItem("Latency", _latency_menu(streamer))
        yield pystray.MenuItem(
            "Mute this PC while streaming",
            lambda icon, item: streamer.toggle_mute_local(),
            checked=lambda item: streamer.mute_local)
        yield pystray.MenuItem(
            "Stop all", lambda icon, item: streamer.stop_all(),
            enabled=lambda item: bool(streamer.sessions))
        yield pystray.MenuItem("Rescan speakers", on_rescan)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(
            "Start last speakers on launch",
            lambda icon, item: streamer.toggle_resume(),
            checked=lambda item: streamer.resume)
        yield pystray.MenuItem(
            "Open log", lambda icon, item: os.startfile(LOGPATH))
        yield pystray.MenuItem(
            f"About {APP_NAME} v{APP_VERSION}",
            lambda icon, item: webbrowser.open(REPO_URL))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Quit", on_quit)
    return pystray.Menu(items)


def already_running():
    """Single-instance guard via a named mutex (Windows only)."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW(None, False, "Local\\AirPlayTray_SingleInstance")
        return kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def main():
    if already_running():
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "AirPlay Tray is already running.\n"
                "Look for the speaker icon in the system tray.", APP_NAME, 0x40)
        except Exception:
            pass
        return
    log.info("starting %s v%s", APP_NAME, APP_VERSION)
    icon_holder = {}
    icon_state = {"active": None}

    def on_change():
        ic = icon_holder.get("icon")
        if not ic:
            return
        active = bool(streamer.active_names())
        if active != icon_state["active"]:
            icon_state["active"] = active
            try:
                ic.icon = make_icon(active)
            except Exception:
                log.exception("icon update failed")
        try:
            ic.update_menu()
        except Exception:
            log.exception("update_menu failed")

    def on_error(msg):
        ic = icon_holder.get("icon")
        if ic:
            try:
                ic.notify(msg, APP_NAME)
            except Exception:
                log.exception("notify failed")

    streamer = Streamer(on_change=on_change, on_error=on_error)
    atexit.register(streamer.shutdown_mute)

    def on_rescan(icon, item):
        streamer.scan()

    def on_quit(icon, item):
        log.info("quit")
        try:
            streamer.stop_all(remember=False).result(timeout=8)
        except BaseException:
            log.exception("teardown on quit incomplete")
        streamer.shutdown_mute()
        icon.stop()

    icon = pystray.Icon(APP_NAME, make_icon(False), APP_NAME,
                        menu=build_menu(streamer, on_rescan, on_quit))
    icon_holder["icon"] = icon

    def on_ready(icon):
        icon.visible = True
        try:
            icon.update_menu()   # build the menu handle so right-click works
        except Exception:
            log.exception("initial update_menu failed")
        log.info("icon ready; scanning")
        streamer.scan()

    icon.run(setup=on_ready)


# ----------------------------- selftest ------------------------------------
def run_selftest():
    args = [a for a in sys.argv[1:] if a != "--selftest"]
    ident = args[0] if args else None
    logpath = os.path.join(os.environ.get("TEMP", "."), "airplay_selftest.log")
    try:
        open(logpath, "w").close()
    except Exception:
        pass

    def out(*a):
        msg = " ".join(str(x) for x in a)
        try:
            with open(logpath, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass
        if sys.__stdout__:
            sys.__stdout__.write(msg + "\n")

    async def go():
        loop = asyncio.get_running_loop()
        confs = (await pyatv.scan(loop, timeout=4, identifier=ident) if ident
                 else await pyatv.scan(loop, timeout=4))
        confs = [c for c in confs
                 if c.get_service(Protocol.RAOP) or c.get_service(Protocol.AirPlay)]
        if not confs:
            out("SELFTEST FAIL: no devices")
            return
        conf = confs[0]
        out(f"SELFTEST target: {conf.name} @ {conf.address}")
        holder = {}

        async def open_src(file, sr, ch, ss):
            s = LivePCMSource(sr, ch, ss)
            holder["src"] = s
            return s

        raop_pkg.open_source = open_src
        atv = await pyatv.connect(conf, loop)
        task = asyncio.ensure_future(atv.stream.stream_file("live"))
        await asyncio.sleep(8)
        out(f"SELFTEST streaming_alive={not task.done()}")
        if "src" in holder:
            await holder["src"].close()
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                task.cancel()
        pending = atv.close()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        out("SELFTEST DONE")

    asyncio.run(go())


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        main()
