"""
Meezinger - karaoke-tekst bij alles wat je Windows-pc afspeelt.

Werking:
  1. Luistert (WASAPI loopback) mee met het standaard audio-uitvoerapparaat.
  2. Herkent het nummer via Shazam (shazamio) en krijgt de positie in het nummer terug.
  3. Haalt gesynchroniseerde songteksten (LRC) op van LRCLIB.
  4. Toont de tekst regel voor regel, gelijk met de muziek, en blijft periodiek
     her-herkennen om drift, doorspoelen en nieuwe nummers op te vangen.

Sneltoetsen:  <- / ->  sync -/+ 0,25 s   |   0  sync resetten
              R  opnieuw herkennen      |   T  altijd bovenop aan/uit
              D  audio-apparaat kiezen  |   O  ondertitelmodus aan/uit
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import threading
import time
import wave
import warnings
from dataclasses import dataclass, field

import numpy as np
import requests

# ----------------------------------------------------------------------------
# Instellingen
# ----------------------------------------------------------------------------
SAMPLE_RATE = 48000          # Windows-standaard; WASAPI converteert indien nodig
SAMPLE_SECONDS = 12          # Shazam-clients sturen een venster van 12 s
BUFFER_SECONDS = 20
SILENCE_RMS = 0.001          # onder deze RMS-waarde beschouwen we audio als stil
SILENCE_PAUSE_AFTER = 1.5    # s stilte voordat de tekstklok pauzeert
RETRY_SEARCHING = 4.0        # s tussen pogingen zolang er geen nummer herkend is
RETRY_LOCKED = 15.0          # s tussen controles als er al een nummer loopt
LYRICS_LEAD = 0.25           # toon regels iets vóór de tijd, zodat je kan inzetten
USER_AGENT = "Meezinger/1.0 (desktop karaoke app)"


# ----------------------------------------------------------------------------
# Audio-opname (WASAPI loopback van een of meer uitvoerapparaten)
# ----------------------------------------------------------------------------
AUTO = "auto"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".meezinger.json")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(**updates) -> None:
    data = load_config()
    data.update(updates)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _com_init() -> None:
    """COM initialiseren in een extra thread (pas NADAT soundcard geïmporteerd is)."""
    if sys.platform == "win32":
        try:  # soundcard gebruikt COM; elke thread moet het initialiseren
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass


def _import_soundcard():
    import soundcard as sc
    warnings.filterwarnings("ignore", category=getattr(
        sys.modules["soundcard"], "SoundcardRuntimeWarning", RuntimeWarning))
    return sc


class DeviceRecorder(threading.Thread):
    """Neemt één uitvoerapparaat op via loopback en houdt een ringbuffer bij."""

    def __init__(self, dev_id: str, name: str) -> None:
        super().__init__(daemon=True, name=f"audio:{name}")
        self.dev_id, self.name = dev_id, name
        self._buf = np.zeros(SAMPLE_RATE * BUFFER_SECONDS, dtype=np.float32)
        self._filled = 0
        self._end_time = time.monotonic()   # tijdstip van de laatste sample
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.error: str | None = None
        self.level = 0.0                    # RMS van het laatste blok (0..1)
        self.last_sound_time = 0.0

    def run(self) -> None:
        _com_init()
        sc = _import_soundcard()
        block = SAMPLE_RATE // 10  # 100 ms per blok
        while not self.stop_event.is_set():
            try:
                mic = sc.get_microphone(self.dev_id, include_loopback=True)
                with mic.recorder(samplerate=SAMPLE_RATE) as rec:
                    self.error = None
                    while not self.stop_event.is_set():
                        data = rec.record(numframes=block)
                        self._push(data.mean(axis=1) if data.ndim == 2 else data)
            except Exception as exc:  # apparaat weg, driverfout, ...
                self.error = f"{exc.__class__.__name__}: {exc}"
                self.level = 0.0
                self.stop_event.wait(2)

    def _push(self, mono: np.ndarray) -> None:
        mono = mono.astype(np.float32, copy=False)
        n = len(mono)
        if not n:
            return
        now = time.monotonic()
        self.level = float(np.sqrt(np.mean(mono ** 2)))
        if self.level > SILENCE_RMS:
            self.last_sound_time = now
        with self._lock:
            if n >= len(self._buf):
                self._buf[:] = mono[-len(self._buf):]
            else:
                self._buf[:-n] = self._buf[n:]
                self._buf[-n:] = mono
            self._filled = min(len(self._buf), self._filled + n)
            self._end_time = now

    def snapshot(self, seconds: float) -> tuple[np.ndarray, float] | None:
        n = int(seconds * SAMPLE_RATE)
        with self._lock:
            if self._filled < n:
                return None
            return self._buf[-n:].copy(), self._end_time - seconds

    def has_sound(self) -> bool:
        return time.monotonic() - self.last_sound_time <= SILENCE_PAUSE_AFTER


class AudioManager(threading.Thread):
    """Beheert de recorders.

    Modus "auto": luistert naar ALLE actieve uitvoerapparaten tegelijk en gebruikt het
    apparaat waar geluid uit komt (handig als de browser op een ander apparaat speelt
    dan het Windows-standaardapparaat). Anders: alleen het gekozen apparaat.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="audio-manager")
        self.mode = self._load_mode()
        self.devices: list[tuple[str, str]] = []   # (id, naam)
        self.default_id: str | None = None
        self.recorders: dict[str, DeviceRecorder] = {}
        self._active: DeviceRecorder | None = None
        self._lock = threading.Lock()
        self.error: str | None = None
        self._wake = threading.Event()

    # -- instellingen ------------------------------------------------------
    @staticmethod
    def _load_mode() -> str:
        return str(load_config().get("device", AUTO))

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        save_config(device=mode)
        self._wake.set()

    # -- thread ------------------------------------------------------------
    def run(self) -> None:
        # Let op: GEEN _com_init() vóór de import. soundcard initialiseert COM zelf bij het
        # importeren en ziet "al geïnitialiseerd" (S_FALSE) ten onrechte als fout.
        try:
            sc = _import_soundcard()
        except Exception as exc:
            self.error = f"Audiobibliotheek kan niet laden: {exc}"
            return
        while True:
            try:
                speakers = sc.all_speakers()
                self.devices = [(s.id, s.name) for s in speakers]
                try:
                    self.default_id = sc.default_speaker().id
                except Exception:
                    self.default_id = None
                self.error = None if self.devices else "Geen audio-uitvoerapparaten gevonden"
            except Exception as exc:
                self.error = f"Apparaten opvragen mislukt: {exc}"
            self._sync_recorders()
            self._wake.wait(3)
            self._wake.clear()

    def _sync_recorders(self) -> None:
        ids = {d for d, _ in self.devices}
        if self.mode == AUTO:
            wanted = ids
        elif self.mode in ids:
            wanted = {self.mode}
        else:  # gekozen apparaat niet (meer) aanwezig: terugvallen op standaard
            wanted = {self.default_id} if self.default_id in ids else set()
        names = dict(self.devices)
        with self._lock:
            for dev_id in list(self.recorders):
                if dev_id not in wanted:
                    self.recorders.pop(dev_id).stop_event.set()
            for dev_id in wanted:
                rec = self.recorders.get(dev_id)
                if rec is None or not rec.is_alive():
                    rec = DeviceRecorder(dev_id, names.get(dev_id, dev_id))
                    self.recorders[dev_id] = rec
                    rec.start()
            if self._active and self._active.dev_id not in self.recorders:
                self._active = None

    # -- voor herkenner en GUI ----------------------------------------------
    def active(self) -> DeviceRecorder | None:
        """Het apparaat waarvan we de audio gebruiken (met wat 'plakkerigheid')."""
        with self._lock:
            recs = list(self.recorders.values())
            cur = self._active
            if cur is not None and cur.has_sound():
                return cur
            sounding = [r for r in recs if r.has_sound()]
            if sounding:
                self._active = max(sounding, key=lambda r: r.level)
            elif cur is None and recs:
                self._active = next((r for r in recs if r.dev_id == self.default_id), recs[0])
            return self._active

    @property
    def device_name(self) -> str:
        rec = self.active()
        return rec.name if rec else "geen apparaat"

    @property
    def level(self) -> float:
        rec = self.active()
        return rec.level if rec else 0.0

    def current_error(self) -> str | None:
        if self.error:
            return self.error
        with self._lock:
            recs = list(self.recorders.values())
        if recs and all(r.error for r in recs):
            return f"Audiofout ({recs[0].name}): {recs[0].error}"
        return None

    def snapshot(self, seconds: float):
        rec = self.active()
        return rec.snapshot(seconds) if rec else None

    def is_silent(self) -> bool:
        rec = self.active()
        return rec is None or not rec.has_sound()


def to_wav_bytes(samples: np.ndarray) -> bytes:
    peak = float(np.max(np.abs(samples))) or 1.0
    pcm = (samples / max(peak, 1e-3) * 0.9 * 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


# ----------------------------------------------------------------------------
# Songteksten (LRCLIB)
# ----------------------------------------------------------------------------
_TS = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Zet LRC-tekst om in een gesorteerde lijst (tijd_in_seconden, regel)."""
    offset = 0.0
    m = re.search(r"\[offset:\s*([+-]?\d+)\]", text, re.I)
    if m:
        offset = int(m.group(1)) / 1000.0
    lines: list[tuple[float, str]] = []
    for raw in text.splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        lyric = _TS.sub("", raw)
        lyric = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", lyric).strip()  # woord-timings
        for s in stamps:
            secs = int(s.group(1)) * 60 + float(s.group(2).replace(":", "."))
            lines.append((max(0.0, secs - offset), lyric))
    lines.sort(key=lambda x: x[0])
    return lines


def _clean_title(title: str) -> str:
    title = re.sub(r"\s*[\(\[](feat\.?|ft\.?|with|prod\.?)[^\)\]]*[\)\]]", "", title, flags=re.I)
    title = re.sub(r"\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo).*$", "", title, flags=re.I)
    return title.strip()


def _clean_artist(artist: str) -> str:
    return re.split(r"\s*(?:,|&| feat\.? | ft\.? | x | and )\s*", artist, maxsplit=1, flags=re.I)[0].strip()


@dataclass
class Lyrics:
    synced: list[tuple[float, str]] = field(default_factory=list)
    plain: str = ""
    source: str = ""


_lyrics_cache: dict[tuple[str, str], Lyrics] = {}


def fetch_lyrics(title: str, artist: str) -> Lyrics:
    key = (title.lower(), artist.lower())
    if key in _lyrics_cache:
        return _lyrics_cache[key]

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    queries = [
        {"track_name": _clean_title(title), "artist_name": _clean_artist(artist)},
        {"q": f"{_clean_artist(artist)} {_clean_title(title)}"},
        {"track_name": _clean_title(title)},
    ]
    want = _clean_title(title).lower()
    best_plain = ""
    for params in queries:
        try:
            r = session.get("https://lrclib.net/api/search", params=params, timeout=10)
            r.raise_for_status()
            results = r.json()
        except Exception:
            continue
        # liefst: gesynchroniseerd + titel die exact overeenkomt
        results.sort(key=lambda x: (not x.get("syncedLyrics"),
                                    (x.get("trackName") or "").lower() != want))
        for item in results:
            if item.get("syncedLyrics"):
                lyr = Lyrics(synced=parse_lrc(item["syncedLyrics"]),
                             plain=item.get("plainLyrics") or "",
                             source="LRCLIB (gesynchroniseerd)")
                if lyr.synced:
                    _lyrics_cache[key] = lyr
                    return lyr
            if not best_plain and item.get("plainLyrics"):
                best_plain = item["plainLyrics"]
    lyr = Lyrics(plain=best_plain, source="LRCLIB (niet gesynchroniseerd)" if best_plain else "")
    _lyrics_cache[key] = lyr
    return lyr


# ----------------------------------------------------------------------------
# Klok die de positie in het nummer bijhoudt
# ----------------------------------------------------------------------------
class SongClock:
    def __init__(self) -> None:
        self._pos = 0.0
        self._t = time.monotonic()
        self.running = False
        self._lock = threading.Lock()

    def position(self) -> float:
        with self._lock:
            return self._pos + (time.monotonic() - self._t if self.running else 0.0)

    def set(self, pos_at: float, at_time: float) -> None:
        with self._lock:
            now = time.monotonic()
            self._pos = pos_at + (now - at_time)
            self._t = now
            self.running = True

    def pause(self) -> None:
        with self._lock:
            if self.running:
                self._pos += time.monotonic() - self._t
                self.running = False

    def resume(self) -> None:
        with self._lock:
            if not self.running:
                self._t = time.monotonic()
                self.running = True


# ----------------------------------------------------------------------------
# Herkenner: Shazam + teksten ophalen, draait in een eigen thread met asyncio
# ----------------------------------------------------------------------------
@dataclass
class Track:
    key: str
    title: str
    artist: str
    cover: str = ""


class Engine:
    def __init__(self, capture: AudioManager) -> None:
        self.capture = capture
        self.clock = SongClock()
        self.track: Track | None = None
        self.lyrics: Lyrics | None = None
        self.status = "Opstarten…"
        self.user_offset = 0.0
        self._force = threading.Event()
        self._lock = threading.Lock()
        self._misses = 0

    # -- publieke API voor de GUI ------------------------------------------
    def request_recognition(self) -> None:
        self._force.set()

    def current_state(self):
        with self._lock:
            return self.track, self.lyrics, self.status

    def song_position(self) -> float:
        return self.clock.position() + self.user_offset + LYRICS_LEAD

    # -- werkthread --------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True,
                         name="recognizer").start()

    async def _main(self) -> None:
        from shazamio import Shazam
        try:
            from shazamio_core import SearchParams
            options = SearchParams(segment_duration_seconds=SAMPLE_SECONDS)
        except Exception:
            options = None
        shazam = Shazam()
        last_attempt = 0.0
        backoff = 0.0

        while True:
            await asyncio.sleep(0.25)
            err = self.capture.current_error()
            if err:
                self._set_status("⚠  " + err)
                continue

            # pauzeren bij stilte, hervatten zodra er weer geluid is
            if self.capture.is_silent():
                if self.clock.running:
                    self.clock.pause()
                where = ("alle uitvoerapparaten" if self.capture.mode == AUTO
                         else self.capture.device_name)
                self._set_status(f"🔇  Geen geluid op {where}")
                continue
            if not self.clock.running and self.track:
                self.clock.resume()
                self._force.set()   # na een pauze snel opnieuw synchroniseren

            interval = (RETRY_LOCKED if self.track else RETRY_SEARCHING) + backoff
            if not self._force.is_set() and time.monotonic() - last_attempt < interval:
                continue
            self._force.clear()

            snap = self.capture.snapshot(SAMPLE_SECONDS)
            if snap is None:
                self._set_status("🎧  Audio verzamelen…")
                continue
            samples, start_time = snap
            # niet herkennen als het fragment grotendeels stil is
            frames = samples[: len(samples) // SAMPLE_RATE * SAMPLE_RATE].reshape(-1, SAMPLE_RATE)
            if np.mean(np.sqrt(np.mean(frames ** 2, axis=1)) > SILENCE_RMS) < 0.6:
                continue

            last_attempt = time.monotonic()
            if not self.track:
                self._set_status("🎧  Luisteren en herkennen…")
            try:
                result = await shazam.recognize(to_wav_bytes(samples), options=options)
                backoff = 0.0
            except Exception as exc:
                backoff = min(60.0, backoff * 2 + 5)
                self._set_status(f"Herkenningsfout ({exc.__class__.__name__}), opnieuw over {int(backoff)} s")
                continue

            await self._handle_result(result, start_time)

    async def _handle_result(self, result: dict, start_time: float) -> None:
        matches = result.get("matches") or []
        track = result.get("track")
        if not matches or not track:
            self._misses += 1
            if self.track and self._misses >= 3:
                with self._lock:
                    self.track, self.lyrics = None, None
                self.clock.pause()
                self._set_status("🎧  Nummer niet (meer) herkend, luisteren…")
            elif not self.track:
                self._set_status("🎧  Nog niet herkend, blijf luisteren…")
            return
        self._misses = 0

        offset = float(matches[0].get("offset", 0.0))  # positie in nummer bij begin fragment
        key = str(track.get("key") or f"{track.get('title')}|{track.get('subtitle')}")

        if self.track and self.track.key == key:
            # zelfde nummer: alleen klok bijstellen als de afwijking merkbaar is
            predicted = self.clock.position() - (time.monotonic() - start_time)
            if abs(predicted - offset) > 0.35:
                self.clock.set(offset, start_time)
            return

        new = Track(key=key, title=track.get("title", "?"), artist=track.get("subtitle", "?"),
                    cover=(track.get("images") or {}).get("coverart", ""))
        self.clock.set(offset, start_time)
        with self._lock:
            self.track, self.lyrics = new, None
        self._set_status("📝  Songtekst ophalen…")
        lyrics = await asyncio.to_thread(fetch_lyrics, new.title, new.artist)
        with self._lock:
            if self.track is new:
                self.lyrics = lyrics
        if lyrics.synced:
            self._set_status(f"🎤  {lyrics.source}")
        elif lyrics.plain:
            self._set_status("Alleen ongesynchroniseerde tekst gevonden")
        else:
            self._set_status("Geen songtekst gevonden voor dit nummer")

    def _set_status(self, text: str) -> None:
        with self._lock:
            self.status = text


# ----------------------------------------------------------------------------
# Welke regels tonen we nu?
# ----------------------------------------------------------------------------
def lyric_window(engine: "Engine", track, lyrics, n_prev: int, n_next: int):
    """Geeft (teksten, voortgang_huidige_regel) voor n_prev + 1 + n_next posities.

    Positie n_prev is de regel die nu gezongen wordt.
    """
    texts = [""] * (n_prev + 1 + n_next)
    frac = 0.0
    if lyrics and lyrics.synced:
        pos = engine.song_position()
        lines = lyrics.synced
        idx = -1
        for i, (t, _) in enumerate(lines):  # lineair is snel genoeg (~100 regels)
            if t <= pos:
                idx = i
            else:
                break
        for slot in range(len(texts)):
            j = idx - n_prev + slot
            if 0 <= j < len(lines):
                texts[slot] = lines[j][1] or "♪"
        if idx == -1:
            texts[n_prev] = "♪ ♪ ♪"
            if lines and n_next:
                texts[n_prev + 1] = lines[0][1]
        if 0 <= idx < len(lines) - 1:
            t0, t1 = lines[idx][0], lines[idx + 1][0]
            frac = min(1.0, max(0.0, (pos - t0) / max(0.1, t1 - t0)))
        if not engine.clock.running:
            texts[n_prev] = "⏸  " + texts[n_prev]
    elif track and lyrics is None:
        texts[n_prev] = "Songtekst ophalen…"
    elif track and lyrics.plain:
        texts[n_prev] = f"♪  {track.title}  ♪"
        if n_next:
            texts[n_prev + 1] = "(alleen tekst zonder tijdcodes, zie hoofdvenster)"
    elif track:
        texts[n_prev] = f"♪  {track.title} – {track.artist}  ♪"
    else:
        texts[n_prev] = "🎧"
        if n_next:
            texts[n_prev + 1] = "Ik luister mee met je audio-uitvoer"
    return texts, frac


# ----------------------------------------------------------------------------
# GUI (tkinter)
# ----------------------------------------------------------------------------
BG, FG, DIM, ACCENT, BAR = "#121218", "#f5f5f7", "#5d5d6e", "#ffcf5a", "#24242e"


# ----------------------------------------------------------------------------
# Ondertitelmodus: alleen de tekst, op een transparante achtergrond
# ----------------------------------------------------------------------------
KEY = "#010203"   # deze kleur maakt Windows volledig transparant (en klik-door)
SUB_CUR, SUB_NEXT, SUB_OUTLINE = ACCENT, "#e8e8ee", "#000000"


class SubtitleOverlay:
    """Randloos venster dat altijd bovenop kan staan en alleen de tekst toont.

    - Transparante delen laten klikken door naar het venster eronder.
    - Beweeg de muis over de tekst voor de werkbalk (verslepen, bovenop, grootte, sluiten).
    - Rechtsklik op de tekst voor alle opties.
    """

    TOOLBAR_H = 30

    def __init__(self, root, engine: "Engine", on_close) -> None:
        import tkinter as tk
        from tkinter import font as tkfont
        self.tk, self.root, self.engine, self.on_close = tk, root, engine, on_close
        cfg = load_config().get("overlay", {})
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.width = int(cfg.get("width", min(1100, int(sw * 0.6))))
        self.x = int(cfg.get("x", (sw - self.width) // 2))
        self.bottom = int(cfg.get("bottom", sh - 90))   # onderkant blijft op zijn plek
        self.size = int(cfg.get("size", 28))
        self.n_lines = int(cfg.get("lines", 3))
        self.topmost = bool(cfg.get("topmost", True))
        self.visible = False
        self._drawn_key = None
        self._hide_job = None
        self._drag = None
        self._bar_y = 0
        self._height = 0

        self.f_cur = tkfont.Font(family="Segoe UI", weight="bold")
        self.f_next = tkfont.Font(family="Segoe UI", weight="bold")
        self.f_bar = tkfont.Font(family="Segoe UI", size=10)
        self._apply_size()

        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.title("Meezinger ondertitels")
        win.overrideredirect(True)
        win.configure(bg=KEY)
        try:
            win.attributes("-transparentcolor", KEY)   # Windows
        except tk.TclError:
            pass  # andere platformen: zwarte achtergrond
        self.canvas = tk.Canvas(win, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        # werkbalk (verschijnt bij hover)
        bar = self.bar = tk.Frame(win, bg="#202028")
        def b(text, cmd, tip_cursor="hand2"):
            w = tk.Label(bar, text=text, font=self.f_bar, fg=FG, bg="#202028", padx=7, pady=3,
                         cursor=tip_cursor)
            w.bind("<Button-1>", lambda e: cmd())
            w.bind("<Enter>", lambda e: w.configure(bg="#3a3a48"))
            w.bind("<Leave>", lambda e: w.configure(bg="#202028"))
            w.pack(side="left")
            return w
        grip = b("⠿ verslepen", lambda: None, "fleur")
        for wdg in (grip,):
            wdg.bind("<ButtonPress-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)
            wdg.bind("<ButtonRelease-1>", self._drag_end)
        self.btn_top = b("📌", self.toggle_topmost)
        b("A−", lambda: self.change_size(-2))
        b("A+", lambda: self.change_size(+2))
        b("⇔−", lambda: self.change_width(-100))
        b("⇔+", lambda: self.change_width(+100))
        self.btn_lines = b("", self.toggle_lines)
        b("⟳", engine.request_recognition)
        b("≡", lambda: self._menu_at_widget(bar))
        b("✕ terug", self.hide)

        # tekst zelf: slepen + rechtsklik + hover
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", lambda e: self.hide())
        self.canvas.bind("<Button-3>", self._menu)
        win.bind("<Enter>", lambda e: self._show_bar())
        win.bind("<Leave>", lambda e: self._schedule_hide_bar())
        win.bind("<Escape>", lambda e: self.hide())

    # -- tonen/verbergen ---------------------------------------------------
    def show(self) -> None:
        self.visible = True
        self._drawn_key = None
        self._height = 0
        self.update()                 # eerst tekenen en positioneren, dan pas tonen
        self.win.deiconify()
        self.win.attributes("-topmost", self.topmost)
        self.win.lift()
        self._show_bar()
        self._schedule_hide_bar(4000)

    def hide(self) -> None:
        self.visible = False
        self.win.withdraw()
        self._save()
        self.on_close()

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    # -- opties ------------------------------------------------------------
    def toggle_topmost(self) -> None:
        self.topmost = not self.topmost
        self.win.attributes("-topmost", self.topmost)
        self._save()

    def toggle_lines(self) -> None:
        self.n_lines = 2 if self.n_lines == 3 else 3
        self._redraw_soon()

    def change_size(self, d: int) -> None:
        self.size = max(14, min(72, self.size + d))
        self._apply_size()
        self._redraw_soon()

    def change_width(self, d: int) -> None:
        sw = self.root.winfo_screenwidth()
        new = max(400, min(sw, self.width + d))
        self.x -= (new - self.width) // 2   # midden blijft op zijn plek
        self.width = new
        self._redraw_soon()

    def _apply_size(self) -> None:
        self.f_cur.configure(size=self.size)
        self.f_next.configure(size=max(11, int(self.size * 0.72)))

    def _redraw_soon(self) -> None:
        self._drawn_key = None
        self._save()
        self.update()

    def _save(self) -> None:
        save_config(overlay={"width": self.width, "x": self.x, "bottom": self.bottom,
                             "size": self.size, "lines": self.n_lines, "topmost": self.topmost})

    def _menu(self, event) -> None:
        tk = self.tk
        m = tk.Menu(self.win, tearoff=0, bg="#2a2a35", fg=FG, activebackground=ACCENT,
                    activeforeground=BG, selectcolor=ACCENT, font=self.f_bar)
        top = tk.BooleanVar(value=self.topmost)
        three = tk.BooleanVar(value=self.n_lines == 3)
        m.add_checkbutton(label="Altijd bovenop", variable=top, command=self.toggle_topmost)
        m.add_checkbutton(label="3 regels tonen (anders 2)", variable=three, command=self.toggle_lines)
        m.add_separator()
        m.add_command(label="Tekst groter", command=lambda: self.change_size(+2))
        m.add_command(label="Tekst kleiner", command=lambda: self.change_size(-2))
        m.add_command(label="Breder", command=lambda: self.change_width(+100))
        m.add_command(label="Smaller", command=lambda: self.change_width(-100))
        m.add_separator()
        m.add_command(label="Tekst eerder (+0,25 s)", command=lambda: self._shift(+0.25))
        m.add_command(label="Tekst later (−0,25 s)", command=lambda: self._shift(-0.25))
        m.add_command(label="Opnieuw herkennen", command=self.engine.request_recognition)
        m.add_separator()
        m.add_command(label="Terug naar hoofdvenster", command=self.hide)
        m.tk_popup(event.x_root, event.y_root)

    def _menu_at_widget(self, widget) -> None:
        class E:  # nep-event met schermcoördinaten
            x_root = widget.winfo_rootx() + widget.winfo_width() - 40
            y_root = widget.winfo_rooty() + widget.winfo_height()
        self._menu(E)

    def _shift(self, d: float) -> None:
        self.engine.user_offset += d

    # -- werkbalk tonen bij hover -------------------------------------------
    def _show_bar(self) -> None:
        if self._hide_job:
            self.win.after_cancel(self._hide_job)
            self._hide_job = None
        self.bar.place(x=0, y=0)
        self.bar.lift()

    def _schedule_hide_bar(self, ms: int = 1500) -> None:
        if self._hide_job:
            self.win.after_cancel(self._hide_job)
        self._hide_job = self.win.after(ms, self._hide_bar)

    def _hide_bar(self) -> None:
        self._hide_job = None
        if self._drag is None:
            self.bar.place_forget()

    # -- verslepen ---------------------------------------------------------
    def _drag_start(self, e) -> None:
        self._drag = (e.x_root, e.y_root, self.x, self.bottom)

    def _drag_move(self, e) -> None:
        if not self._drag:
            return
        x0, y0, wx, wb = self._drag
        self.x, self.bottom = wx + e.x_root - x0, wb + e.y_root - y0
        self._place(self._height)

    def _drag_end(self, e) -> None:
        self._drag = None
        self._save()

    def _place(self, height: int) -> None:
        self._height = height
        self.canvas.configure(width=self.width, height=height)
        self.win.geometry(f"{self.width}x{height}+{self.x}+{self.bottom - height}")

    # -- tekenen -----------------------------------------------------------
    def _outlined(self, x, y, text, font, fill, width) -> int:
        """Tekst met zwarte rand, zodat hij op elke achtergrond leesbaar is."""
        c = self.canvas
        r = max(2, font.cget("size") // 12)
        for dx in (-r, 0, r):
            for dy in (-r, 0, r):
                if dx or dy:
                    c.create_text(x + dx, y + dy, text=text, font=font, fill=SUB_OUTLINE,
                                  width=width, justify="center", anchor="n")
        return c.create_text(x, y, text=text, font=font, fill=fill, width=width,
                             justify="center", anchor="n")

    def update(self) -> None:
        if not self.visible:
            return
        track, lyrics, _ = self.engine.current_state()
        texts, frac = lyric_window(self.engine, track, lyrics, 0, self.n_lines - 1)
        lines_label = f"{self.n_lines} regels"
        if self.btn_lines.cget("text") != lines_label:
            self.btn_lines.configure(text=lines_label)
        self.btn_top.configure(fg=ACCENT if self.topmost else DIM)

        key = (tuple(texts), self.width, self.size)
        c = self.canvas
        if key != self._drawn_key:
            self._drawn_key = key
            c.delete("all")
            wrap = self.width - 40
            y = self.TOOLBAR_H + 4
            cx = self.width // 2
            item = self._outlined(cx, y, texts[0], self.f_cur, SUB_CUR, wrap)
            y = c.bbox(item)[3] + 3
            self._bar_y = y
            y += 10
            for t in texts[1:]:
                if not t:
                    continue
                item = self._outlined(cx, y, t, self.f_next, SUB_NEXT, wrap)
                y = c.bbox(item)[3] + 4
            height = y + 8
            if height != self._height or self.width != int(self.canvas.cget("width")):
                self._place(height)
        # voortgangsbalkje onder de huidige regel
        c.delete("progress")
        if frac > 0:
            bw = min(self.width - 80, max(200, self.f_cur.measure(texts[0])))
            x0 = (self.width - bw) // 2
            c.create_rectangle(x0 - 1, self._bar_y - 1, x0 + bw * frac + 1, self._bar_y + 5,
                               fill=SUB_OUTLINE, width=0, tags="progress")
            c.create_rectangle(x0, self._bar_y, x0 + bw * frac, self._bar_y + 4,
                               fill=SUB_CUR, width=0, tags="progress")


def run_gui(engine: Engine) -> None:
    import tkinter as tk
    from tkinter import font as tkfont

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    root.title("Meezinger")
    root.geometry("900x560")
    root.minsize(420, 300)
    root.configure(bg=BG)

    f_title = tkfont.Font(family="Segoe UI", size=14, weight="bold")
    f_artist = tkfont.Font(family="Segoe UI", size=11)
    f_cur = tkfont.Font(family="Segoe UI", size=30, weight="bold")
    f_near = tkfont.Font(family="Segoe UI", size=18)
    f_small = tkfont.Font(family="Segoe UI", size=10)

    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=24, pady=(16, 0))
    audio_box = tk.Frame(header, bg=BG)
    audio_box.pack(side="right", anchor="ne")
    btn_device = tk.Label(audio_box, text="🔊 …", font=f_small, fg=FG, bg="#33333f",
                          padx=10, pady=3, cursor="hand2", anchor="e")
    btn_device.pack(anchor="e")
    btn_sub = tk.Label(header, text="🎬 Ondertitelmodus", font=f_small, fg=BG, bg=ACCENT,
                       padx=10, pady=3, cursor="hand2")
    btn_sub.pack(side="right", anchor="ne", padx=(0, 10))
    btn_sub.bind("<Button-1>", lambda e: toggle_overlay())
    meter = tk.Canvas(audio_box, width=220, height=8, bg=BAR, highlightthickness=0)
    meter.pack(anchor="e", pady=(6, 0))
    lbl_hears = tk.Label(audio_box, text="", font=f_small, fg=DIM, bg=BG, anchor="e")
    lbl_hears.pack(anchor="e")
    lbl_title = tk.Label(header, text="Meezinger", font=f_title, fg=FG, bg=BG, anchor="w")
    lbl_title.pack(fill="x")
    lbl_artist = tk.Label(header, text="Speel muziek af op je pc…", font=f_artist, fg=DIM, bg=BG, anchor="w")
    lbl_artist.pack(fill="x")

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=24)
    lines_frame = tk.Frame(body, bg=BG)   # pack(expand) zonder fill = verticaal gecentreerd
    lines_frame.pack(fill="x", expand=True)
    N_PREV, N_NEXT = 2, 3
    line_labels = []
    for i in range(N_PREV + 1 + N_NEXT):
        is_cur = i == N_PREV
        lbl = tk.Label(lines_frame, text="", bg=BG, fg=ACCENT if is_cur else DIM,
                       font=f_cur if is_cur else f_near, justify="center")
        lbl.pack(fill="x", pady=(10 if is_cur else 3))
        line_labels.append(lbl)
        if is_cur:
            progress = tk.Canvas(lines_frame, height=4, bg=BG, highlightthickness=0)
            progress.pack(fill="x", padx=120)
    plain_text = tk.Text(body, bg=BG, fg=FG, font=f_near, wrap="word", relief="flat",
                         highlightthickness=0, padx=10)

    footer = tk.Frame(root, bg=BAR)
    footer.pack(fill="x", side="bottom")
    lbl_status = tk.Label(footer, text="", font=f_small, fg=DIM, bg=BAR, anchor="w")
    lbl_status.pack(side="left", padx=12, pady=8, fill="x", expand=True)

    def btn(text, cmd):
        b = tk.Label(footer, text=text, font=f_small, fg=FG, bg="#33333f", padx=10, pady=3, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.pack(side="left", padx=3, pady=6)
        return b

    topmost = tk.BooleanVar(value=False)

    def shift(d):
        engine.user_offset = 0.0 if d is None else engine.user_offset + d

    def toggle_top():
        topmost.set(not topmost.get())
        root.attributes("-topmost", topmost.get())

    btn("−", lambda: shift(-0.25))
    lbl_offset = tk.Label(footer, text="", font=f_small, fg=FG, bg=BAR, width=11)
    lbl_offset.pack(side="left")
    btn("+", lambda: shift(+0.25))
    btn("Reset", lambda: shift(None))
    btn("⟳ Herken", engine.request_recognition)
    btn_top = btn("📌 Bovenop", toggle_top)
    tk.Label(footer, text="", bg=BAR, width=1).pack(side="left")

    root.bind("<Left>", lambda e: shift(-0.25))
    root.bind("<Right>", lambda e: shift(+0.25))
    root.bind("0", lambda e: shift(None))
    root.bind("r", lambda e: engine.request_recognition())
    root.bind("t", lambda e: toggle_top())
    root.bind("o", lambda e: toggle_overlay())

    def overlay_closed():
        root.deiconify()
        root.lift()

    overlay = SubtitleOverlay(root, engine, on_close=overlay_closed)

    def toggle_overlay():
        if overlay.visible:
            overlay.hide()
        else:
            overlay.show()
            root.iconify()   # hoofdvenster blijft in de taakbalk

    def on_resize(event):
        if event.widget is not root:
            return
        w = max(event.width, 420)
        f_cur.configure(size=max(16, min(44, w // 28)))
        f_near.configure(size=max(11, min(26, w // 48)))
        for lbl in line_labels:
            lbl.configure(wraplength=w - 80)
    root.bind("<Configure>", on_resize)


    capture = engine.capture
    device_choice = tk.StringVar(value=capture.mode)
    meter_peak = {"v": 0.0}

    def short(name: str, n: int = 34) -> str:
        return name if len(name) <= n else name[: n - 1] + "…"

    def open_device_menu(event=None):
        menu = tk.Menu(root, tearoff=0, bg="#2a2a35", fg=FG, activebackground=ACCENT,
                       activeforeground=BG, selectcolor=ACCENT, font=f_small)
        device_choice.set(capture.mode)
        menu.add_radiobutton(label="Automatisch (luister naar alle apparaten)", value=AUTO,
                             variable=device_choice, command=lambda: capture.set_mode(AUTO))
        menu.add_separator()
        recs = dict(capture.recorders)
        for dev_id, name in capture.devices:
            tags = []
            if dev_id == capture.default_id:
                tags.append("standaard")
            rec = recs.get(dev_id)
            if rec is not None and rec.has_sound():
                tags.append("speelt nu")
            label = name + (f"   ({', '.join(tags)})" if tags else "")
            menu.add_radiobutton(label=label, value=dev_id, variable=device_choice,
                                 command=lambda d=dev_id: capture.set_mode(d))
        if not capture.devices:
            menu.add_command(label="Geen apparaten gevonden", state="disabled")
        menu.tk_popup(btn_device.winfo_rootx(), btn_device.winfo_rooty() + btn_device.winfo_height())

    btn_device.bind("<Button-1>", open_device_menu)
    root.bind("d", open_device_menu)

    def render_audio():
        rec = capture.active()
        mode_txt = "Automatisch" if capture.mode == AUTO else "Vast"
        name = rec.name if rec else "geen apparaat"
        btn_device.configure(text=f"🔊 {mode_txt}: {short(name)}  ▾")
        # niveaumeter in dB (-60 .. 0 dBFS)
        level = rec.level if rec else 0.0
        db = 20 * np.log10(max(level, 1e-6))
        frac = min(1.0, max(0.0, (db + 60) / 60))
        meter_peak["v"] = max(frac, meter_peak["v"] - 0.02)
        w = int(meter.cget("width"))
        meter.delete("all")
        color = "#4cd97b" if level > SILENCE_RMS else DIM
        meter.create_rectangle(0, 0, w * frac, 8, fill=color, width=0)
        meter.create_rectangle(w * meter_peak["v"] - 2, 0, w * meter_peak["v"], 8, fill=FG, width=0)
        if capture.current_error():
            lbl_hears.configure(text="⚠ audiofout, zie onderbalk", fg="#ff6b6b")
        elif rec and rec.has_sound():
            lbl_hears.configure(text=f"hoort geluid  ({db:.0f} dB)", fg="#4cd97b")
        else:
            lbl_hears.configure(text="geen geluid", fg=DIM)

    state = {"key": None, "mode": None}

    def show_mode(mode):
        if state["mode"] == mode:
            return
        state["mode"] = mode
        if mode == "plain":
            lines_frame.pack_forget()
            plain_text.pack(fill="both", expand=True, pady=10)
        else:
            plain_text.pack_forget()
            lines_frame.pack(fill="x", expand=True)

    def tick():
        track, lyrics, status = engine.current_state()
        lbl_status.configure(text=status)
        render_audio()
        sign = "+" if engine.user_offset >= 0 else "−"
        lbl_offset.configure(text=f"sync {sign}{abs(engine.user_offset):.2f}s")
        btn_top.configure(bg=ACCENT if topmost.get() else "#33333f", fg=BG if topmost.get() else FG)

        if track:
            lbl_title.configure(text=track.title)
            lbl_artist.configure(text=track.artist)
        else:
            lbl_title.configure(text="Meezinger")
            lbl_artist.configure(text="Speel muziek af op je pc…")

        key = (track.key if track else None, id(lyrics))
        if lyrics and not lyrics.synced and lyrics.plain:
            show_mode("plain")
            if state["key"] != key:
                plain_text.configure(state="normal")
                plain_text.delete("1.0", "end")
                plain_text.insert("1.0", lyrics.plain)
                plain_text.configure(state="disabled")
        else:
            show_mode("synced")
            render_synced(track, lyrics)
        state["key"] = key
        overlay.update()
        root.after(50, tick)

    def render_synced(track, lyrics):
        texts, frac = lyric_window(engine, track, lyrics, N_PREV, N_NEXT)
        if track and lyrics is not None and not lyrics.synced:
            texts[N_PREV] = "♪  Geen songtekst gevonden  ♪"
        for lbl, txt in zip(line_labels, texts):
            if lbl.cget("text") != txt:
                lbl.configure(text=txt)
        progress.delete("all")
        w = progress.winfo_width()
        if frac > 0:
            progress.create_rectangle(0, 0, w * frac, 4, fill=ACCENT, width=0)

    tick()
    root.mainloop()


def main() -> None:
    capture = AudioManager()
    capture.start()
    engine = Engine(capture)
    engine.start()
    run_gui(engine)


if __name__ == "__main__":
    main()
