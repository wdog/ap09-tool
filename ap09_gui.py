#!/usr/bin/env python3
"""
ap09-gui: GTK4 / libadwaita front end for ap09.py (Ammoon AP-09 nano looper).

All pedal I/O runs on one background worker thread (the pedal only handles one
client at a time); the UI is updated through GLib.idle_add.

Copyright (c) 2026 wdog <wdog666@gmail.com>
SPDX-License-Identifier: MIT  (see LICENSE; keep this notice in copies and derivatives)
"""

import os
import queue
import sys
import threading
import traceback
import wave

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk, Pango  # noqa: E402

import numpy as np  # noqa: E402
import usb.core  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ap09  # noqa: E402

APP_ID = "io.github.ap09.Looper"
CACHE_DIR = ap09.cache_dir()
AUDIO_FILTER_MIME = ["audio/*"]

CSS = b"""
.hero { padding: 18px; }
.hero-title { font-size: 1.6em; font-weight: 800; }
.big-time { font-size: 2.4em; font-weight: 800; font-feature-settings: "tnum"; }
.state-playing { color: @success_color; }
.state-saved { color: @accent_color; }
.state-damaged { color: @warning_color; }
.state-dup { color: alpha(@window_fg_color, 0.5); }
.state-empty { color: @error_color; }
.pill-badge { border-radius: 99px; padding: 2px 10px; font-weight: 700; font-size: 0.85em; }
.badge-playing { background: alpha(@success_color, 0.18); color: @success_color; }
.badge-saved { background: alpha(@accent_color, 0.18); color: @accent_color; }
.badge-damaged { background: alpha(@warning_color, 0.2); color: @warning_color; }
.badge-memory { background: alpha(@accent_color, 0.18); color: @accent_color; }
.badge-deleted { background: alpha(@window_fg_color, 0.1); color: alpha(@window_fg_color, 0.6); }
.dropzone { border: 2px dashed alpha(@accent_color, 0.6); border-radius: 18px; padding: 22px; }
.dropzone.hover { background: alpha(@accent_color, 0.12); border-style: solid; }
.player { padding: 6px 12px; }
.progress-card { padding: 14px 18px; margin: 10px 12px 0 12px; }
.progress-card progressbar trough, .progress-card progressbar progress { min-height: 12px; border-radius: 6px; }
.progress-pct { font-size: 2em; font-weight: 800; font-feature-settings: "tnum"; }
"""


# ---------------------------------------------------------------- helpers

fmt_secs = ap09.fmt_secs
loop_secs = ap09.loop_secs
cache_path = ap09.cache_path


def loop_key(loop) -> str:
    return os.path.basename(ap09.cache_path(loop))


def pcm_to_float(pcm: bytes) -> np.ndarray:
    """24-bit LE signed mono PCM -> float32 in [-1, 1]."""
    n = len(pcm) // 3
    b = np.frombuffer(pcm[:n * 3], dtype=np.uint8).reshape(n, 3).astype(np.int32)
    v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
    v = np.where(v & 0x800000, v - 0x1000000, v)
    return v.astype(np.float32) / 8388608.0


def peaks(pcm: bytes, buckets: int = 600) -> np.ndarray:
    x = pcm_to_float(pcm)
    if len(x) == 0:
        return np.zeros(buckets, dtype=np.float32)
    buckets = min(buckets, len(x))
    x = x[:len(x) - len(x) % buckets].reshape(buckets, -1)
    p = np.abs(x).max(axis=1)
    top = p.max()
    return p / top if top > 0 else p  # normalised so quiet loops are still visible


def read_wav_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())


# ---------------------------------------------------------------- widgets

class Waveform(Gtk.DrawingArea):
    """Mirrored peak waveform with an optional playhead."""

    def __init__(self, height=90):
        super().__init__()
        self.peaks = None
        self.position = None  # 0..1 playhead
        self.placeholder = ""
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_peaks(self, p, placeholder=""):
        self.peaks = p
        self.placeholder = placeholder
        self.queue_draw()

    def set_position(self, pos):
        self.position = pos
        self.queue_draw()

    def _draw(self, area, cr, w, h):
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        fg = self.get_color()
        mid = h / 2
        if self.peaks is None or len(self.peaks) == 0:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            cr.set_line_width(1)
            cr.move_to(0, mid)
            cr.line_to(w, mid)
            cr.stroke()
            if self.placeholder:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
                cr.select_font_face("Sans")
                cr.set_font_size(12)
                ext = cr.text_extents(self.placeholder)
                cr.move_to((w - ext.width) / 2, mid - 8)
                cr.show_text(self.placeholder)
            return
        n = len(self.peaks)
        bar = w / n
        played = int(self.position * n) if self.position is not None else -1
        for i, p in enumerate(self.peaks):
            a = 1.0 if (played < 0 or i <= played) else 0.35
            cr.set_source_rgba(accent.red, accent.green, accent.blue, a)
            y = max(1.0, p * (mid - 2))
            cr.rectangle(i * bar, mid - y, max(1.0, bar - 0.6), 2 * y)
        cr.fill()
        if self.position is not None:
            x = self.position * w
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
            cr.set_line_width(2)
            cr.move_to(x, 0)
            cr.line_to(x, h)
            cr.stroke()


def badge(text: str, kind: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("pill-badge")
    lbl.add_css_class(f"badge-{kind}")
    lbl.set_valign(Gtk.Align.CENTER)
    return lbl


def icon_button(icon: str, tooltip: str, cb, *args) -> Gtk.Button:
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    b.add_css_class("circular")
    b.connect("clicked", lambda *_: cb(*args))
    return b


# ---------------------------------------------------------------- worker

class Cancelled(Exception):
    pass


class Worker:
    """Serialises every pedal job on one thread. Jobs get a fresh Looper each time.
    cancel() stops the running job at its next progress report (between two USB
    commands), which is always a safe point."""

    def __init__(self):
        self.q = queue.Queue()
        self.busy = False
        self.cancel_flag = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self.cancel_flag.set()

    def submit(self, fn, on_done=None, on_error=None, on_progress=None):
        self.q.put((fn, on_done, on_error, on_progress))

    def _run(self):
        while True:
            fn, on_done, on_error, on_progress = self.q.get()
            self.busy = True
            self.cancel_flag.clear()
            lp = None
            try:
                lp = ap09.Looper()

                def progress(frac, text, on_progress=on_progress):
                    if self.cancel_flag.is_set():
                        raise Cancelled()
                    if on_progress:
                        GLib.idle_add(on_progress, frac, text)

                result = fn(lp, progress)
                if on_done:
                    GLib.idle_add(on_done, result)
            except Cancelled:
                if on_error:
                    GLib.idle_add(on_error, "__cancelled__")
            except Exception as e:  # noqa: BLE001 - everything goes to the UI
                traceback.print_exc()
                msg = str(e)
                if isinstance(e, usb.core.USBError) and e.errno == 16:
                    msg = "pedal busy: another program is using it"
                elif isinstance(e, usb.core.USBError) and e.errno == 13:
                    msg = "permission denied: install the udev rule (see README) or run with sudo"
                if on_error:
                    GLib.idle_add(on_error, msg)
            finally:
                if lp:
                    lp.close()
                self.busy = False


# ---------------------------------------------------------------- player

class Player:
    """Tiny GStreamer playbin wrapper that reports position to a callback."""

    def __init__(self, on_position, on_state):
        Gst.init(None)
        self.bin = Gst.ElementFactory.make("playbin", "player")
        self.on_position = on_position
        self.on_state = on_state
        self.playing = False
        self.uri = None
        bus = self.bin.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", lambda *_: self.stop())
        bus.connect("message::error", lambda *_: self.stop())
        GLib.timeout_add(50, self._tick)

    def play(self, path):
        uri = Gst.filename_to_uri(path)
        if self.uri != uri:
            self.bin.set_state(Gst.State.NULL)
            self.bin.set_property("uri", uri)
            self.uri = uri
        self.bin.set_state(Gst.State.PLAYING)
        self.playing = True
        self.on_state(True)

    def pause(self):
        self.bin.set_state(Gst.State.PAUSED)
        self.playing = False
        self.on_state(False)

    def stop(self):
        self.bin.set_state(Gst.State.NULL)
        self.playing = False
        self.uri = None
        self.on_state(False)
        self.on_position(None)

    def seek(self, frac):
        ok, dur = self.bin.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            self.bin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(dur * frac))

    def _tick(self):
        if self.playing:
            ok1, pos = self.bin.query_position(Gst.Format.TIME)
            ok2, dur = self.bin.query_duration(Gst.Format.TIME)
            if ok1 and ok2 and dur > 0:
                self.on_position(pos / dur)
        return True


# ---------------------------------------------------------------- window

class LooperWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="AP-09 Looper")
        self.set_default_size(760, 820)
        self.worker = Worker()
        self.records = []
        self.connected = None
        self.space = None
        self.playing_key = None
        self.waveforms = {}      # loop key -> list of Waveform widgets to update
        self.play_buttons = {}   # loop key -> list of buttons
        self.player = Player(self._on_play_position, self._on_play_state)

        # header
        self.title = Adw.WindowTitle(title="AP-09 Looper", subtitle="looking for the pedal…")
        header = Adw.HeaderBar(title_widget=self.title)
        self.upload_btn = Gtk.Button(label="Upload", icon_name="document-send-symbolic",
                                     tooltip_text="Put an audio file on the pedal")
        self.upload_btn.set_child(self._label_icon("document-send-symbolic", "Upload"))
        self.upload_btn.add_css_class("suggested-action")
        self.upload_btn.connect("clicked", lambda *_: self.choose_upload())
        header.pack_start(self.upload_btn)
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh (F5)")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_end(self._build_menu())
        header.pack_end(self.refresh_btn)

        # progress card under the header
        self.progress_rev = Gtk.Revealer(child=self._build_progress_card(),
                                         transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)

        # pages
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_disconnected(), "disconnected")
        self.stack.add_named(self._build_main(), "main")
        self.stack.add_named(Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True), "loading")

        # player bar at the bottom
        self.player_bar = self._build_player_bar()

        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_top_bar(self.progress_rev)
        view.add_bottom_bar(self.player_bar)
        self.set_content(view)

        # drag & drop anywhere in the window
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        drop.connect("enter", lambda *_: (self.dropzone.add_css_class("hover"), Gdk.DragAction.COPY)[1])
        drop.connect("leave", lambda *_: self.dropzone.remove_css_class("hover"))
        self.add_controller(drop)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        os.makedirs(CACHE_DIR, exist_ok=True)
        self.stack.set_visible_child_name("loading")
        GLib.timeout_add_seconds(2, self._poll_connection)
        self._poll_connection()

    # ---------------------------------------------------------- building

    @staticmethod
    def _label_icon(icon, text):
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image(icon_name=icon))
        box.append(Gtk.Label(label=text))
        return box

    def _build_menu(self):
        menu = Gio.Menu()
        menu.append("Scan free space", "win.space")
        menu.append("Clear the pedal", "win.clear")
        menu.append("Download all loops…", "win.download-all")
        menu.append("Open cache folder", "win.cache")
        menu.append("About", "win.about")
        for name, cb in (("space", self.scan_space), ("clear", self.confirm_clear),
                         ("download-all", self.download_all), ("about", self.show_about),
                         ("cache", lambda: Gio.AppInfo.launch_default_for_uri(Gst.filename_to_uri(CACHE_DIR), None))):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda _a, _p, f=cb: f())
            self.add_action(act)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu")

    def _build_progress_card(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        card.add_css_class("progress-card")
        top = Gtk.Box(spacing=12)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.progress_title = Gtk.Label(xalign=0)
        self.progress_title.add_css_class("heading")
        self.progress_detail = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.progress_detail.add_css_class("dim-label")
        texts.append(self.progress_title)
        texts.append(self.progress_detail)
        self.progress_pct = Gtk.Label()
        self.progress_pct.add_css_class("progress-pct")
        self.cancel_btn = Gtk.Button(label="Cancel", valign=Gtk.Align.CENTER,
                                     tooltip_text="Stop safely after the current step")
        self.cancel_btn.add_css_class("pill")
        self.cancel_btn.connect("clicked", lambda *_: self._cancel())
        top.append(texts)
        top.append(self.progress_pct)
        top.append(self.cancel_btn)
        card.append(top)
        self.progress = Gtk.ProgressBar()
        card.append(self.progress)
        return card

    def _cancel(self):
        self.cancel_btn.set_sensitive(False)
        self.cancel_btn.set_label("Stopping…")
        self.worker.cancel()

    def _build_disconnected(self):
        page = Adw.StatusPage(icon_name="audio-input-microphone-symbolic",
                              title="Plug in the looper",
                              description="Connect the Ammoon AP-09 with its USB cable.\n"
                                          "It is detected automatically.")
        self.disc_detail = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.disc_detail.add_css_class("dim-label")
        page.set_child(self.disc_detail)
        return page

    def _build_main(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=18, margin_bottom=24, margin_start=12, margin_end=12)

        # hero card: what the pedal plays
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hero.add_css_class("card")
        hero.add_css_class("hero")
        top = Gtk.Box(spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        cap = Gtk.Label(label="ON THE PEDAL", xalign=0)
        cap.add_css_class("caption-heading")
        cap.add_css_class("dim-label")
        self.hero_time = Gtk.Label(xalign=0)
        self.hero_time.add_css_class("big-time")
        self.hero_sub = Gtk.Label(xalign=0)
        self.hero_sub.add_css_class("dim-label")
        left.append(cap)
        left.append(self.hero_time)
        left.append(self.hero_sub)
        top.append(left)
        self.hero_buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        top.append(self.hero_buttons)
        hero.append(top)
        self.hero_wave = Waveform(height=110)
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_hero_wave_click)
        self.hero_wave.add_controller(click)
        hero.append(self.hero_wave)
        box.append(hero)

        # drop zone
        self.dropzone = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.dropzone.add_css_class("dropzone")
        dz_icon = Gtk.Image(icon_name="folder-music-symbolic", pixel_size=36)
        dz_icon.add_css_class("dim-label")
        dz_title = Gtk.Label(label="Drop an audio file here to put it on the pedal")
        dz_title.add_css_class("heading")
        dz_sub = Gtk.Label(label="wav · mp3 · flac · ogg … converted to mono 24-bit 46875 Hz")
        dz_sub.add_css_class("dim-label")
        dz_sub.add_css_class("caption")
        for w in (dz_icon, dz_title, dz_sub):
            self.dropzone.append(w)
        dz_click = Gtk.GestureClick()
        dz_click.connect("released", lambda *_: self.choose_upload())
        self.dropzone.add_controller(dz_click)
        self.dropzone.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        box.append(self.dropzone)

        # memory
        mem = Adw.PreferencesGroup(title="Memory")
        self.space_row = Adw.ActionRow(title="Free space", subtitle="not scanned yet")
        self.space_row.add_prefix(Gtk.Image(icon_name="drive-harddisk-symbolic"))
        self.space_level = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=120)
        self.space_level.set_visible(False)
        self.space_row.add_suffix(self.space_level)
        scan = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER, tooltip_text="Full scan, about 4 minutes")
        scan.connect("clicked", lambda *_: self.scan_space())
        self.space_row.add_suffix(scan)
        mem.add(self.space_row)
        self.device_row = Adw.ExpanderRow(title="Device", subtitle="–")
        self.device_row.add_prefix(Gtk.Image(icon_name="audio-card-symbolic"))
        self.device_rows = {}
        for key in ("model", "usb", "format", "info", "index"):
            row = Adw.ActionRow(title=key, subtitle="–", subtitle_selectable=True)
            row.add_css_class("property")
            self.device_row.add_row(row)
            self.device_rows[key] = row
        mem.add(self.device_row)
        box.append(mem)

        # history
        self.history = Adw.PreferencesGroup(
            title="Loops",
            description="▶ playing · ● in memory · ⚠ damaged · ✖ deleted. "
                        "Numbers are the same as in the CLI (ap09.py list).")
        self.show_deleted = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Show deleted loops (list --all)")
        self.show_deleted.connect("notify::active", lambda *_: self._fill_history())
        sd = Gtk.Box(spacing=6)
        sd_label = Gtk.Label(label="deleted")
        sd_label.add_css_class("dim-label")
        sd.append(sd_label)
        sd.append(self.show_deleted)
        self.history.set_header_suffix(sd)
        self.history_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.history_list.add_css_class("boxed-list")
        self.history.add(self.history_list)
        box.append(self.history)

        clamp = Adw.Clamp(maximum_size=760, child=box)
        return Gtk.ScrolledWindow(child=clamp, vexpand=True)

    def _build_player_bar(self):
        bar = Gtk.Box(spacing=10)
        bar.add_css_class("player")
        self.pl_btn = Gtk.Button(icon_name="media-playback-pause-symbolic", valign=Gtk.Align.CENTER)
        self.pl_btn.add_css_class("circular")
        self.pl_btn.connect("clicked", lambda *_: self._toggle_bar_play())
        self.pl_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.pl_title.add_css_class("heading")
        self.pl_wave = Waveform(height=36)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda g, n, x, y: self.player.seek(x / max(1, self.pl_wave.get_width())))
        self.pl_wave.add_controller(click)
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Stop")
        stop.add_css_class("flat")
        stop.connect("clicked", lambda *_: self.player.stop())
        bar.append(self.pl_btn)
        bar.append(self.pl_title)
        bar.append(self.pl_wave)
        bar.append(stop)
        self.player_rev = Gtk.Revealer(child=bar, transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        return self.player_rev

    # ---------------------------------------------------------- connection / refresh

    def _poll_connection(self):
        if self.worker.busy:
            return True
        present = usb.core.find(idVendor=ap09.VID, idProduct=ap09.PID) is not None
        if present != self.connected:
            self.connected = present
            if present:
                self.title.set_subtitle("connected")
                self.refresh()
            else:
                self.title.set_subtitle("not connected")
                self.stack.set_visible_child_name("disconnected")
                self.upload_btn.set_sensitive(False)
        return True

    def refresh(self):
        def job(lp, progress):
            return ap09.read_device_info(lp), ap09.all_records(lp, include_deleted=True)

        self.refresh_btn.set_sensitive(False)
        self.worker.submit(job, self._on_refreshed, self._on_refresh_error)

    def _on_refresh_error(self, msg):
        self.refresh_btn.set_sensitive(True)
        self.stack.set_visible_child_name("disconnected")
        self.disc_detail.set_label(msg)
        self.title.set_subtitle("error")
        self.upload_btn.set_sensitive(False)

    def _on_refreshed(self, result):
        d, recs = result
        self.refresh_btn.set_sensitive(True)
        self.upload_btn.set_sensitive(True)
        self.all_loops = recs
        self.records = [r for r in recs if not r["deleted"]]
        self.stack.set_visible_child_name("main")
        name = d["model_name"] if d["model"] == 0x2715 else f"unknown model 0x{d['model']:04x}"
        self.title.set_subtitle(name)
        self.device_row.set_subtitle(f"{name} · USB {d['usb']} · bus {d['bus']} addr {d['address']}")
        cur = next((r for r in recs if r["current"]), None)
        values = {"model": f"0x{d['model']:04x} {d['model_name']}",
                  "usb": f"{d['usb']} · bus {d['bus']} addr {d['address']}",
                  "format": d["format"], "info": d["info"],
                  "index": f"record @ +{cur['offset']:#x}" if cur else "no loop"}
        for k, v in values.items():
            self.device_rows[k].set_subtitle(v)
        self.waveforms.clear()
        self.play_buttons.clear()
        self._fill_hero()
        self._fill_history()

    @staticmethod
    def _state(r):
        key, label = ap09.loop_state(r)
        icon = ap09.STATES[key][0]
        return key, f"{icon} {label}"

    def _fill_hero(self):
        child = self.hero_buttons.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.hero_buttons.remove(child)
            child = nxt
        cur = next((r for r in self.records if r["current"]), None)
        self.hero_loop = cur
        if cur is None:
            self.hero_time.set_label("No loop")
            self.hero_time.remove_css_class("state-playing")
            self.hero_time.add_css_class("state-empty")
            self.hero_sub.set_label("Record one on the pedal, upload a file, or restore one below")
            self.hero_wave.set_peaks(None, "")
            return
        self.hero_time.remove_css_class("state-empty")
        self.hero_time.add_css_class("state-playing")
        self.hero_time.set_label(fmt_secs(loop_secs(cur)))
        self.hero_sub.set_label(f"▶ playing #{cur['num']} · {len(cur['blocks'])} blocks · {cur['length']:,} bytes")
        self._register_wave(cur, self.hero_wave)
        play = self._play_button(cur)
        play.add_css_class("suggested-action")
        self.hero_buttons.append(play)
        self.hero_buttons.append(icon_button("document-save-symbolic", "Save as WAV…", self.download, cur))
        self.hero_buttons.append(icon_button("edit-clear-all-symbolic", "Clear the pedal", self.confirm_clear))

    def _fill_history(self):
        while (row := self.history_list.get_row_at_index(0)) is not None:
            self.history_list.remove(row)
        loops = getattr(self, "all_loops", self.records)
        shown = [r for r in loops if not r["current"] and (self.show_deleted.get_active() or not r["deleted"])]
        self.history.set_visible(bool([r for r in loops if not r["current"]]))
        for r in reversed(shown):  # newest first
            kind, label = self._state(r)
            row = Adw.ActionRow(title=f"#{r['num']}", subtitle=f"{fmt_secs(loop_secs(r))} · {len(r['blocks'])} blocks")
            row.add_prefix(badge(label, kind))
            wave = Waveform(height=34)
            wave.set_size_request(150, -1)
            wave.set_hexpand(False)
            self._register_wave(r, wave)
            row.add_suffix(wave)
            row.add_suffix(self._play_button(r))
            row.add_suffix(icon_button("document-save-symbolic", "Save as WAV…", self.download, r))
            restore = Gtk.Button(label="Put on pedal", valign=Gtk.Align.CENTER,
                                 tooltip_text=f"Make #{r['num']} the loop the pedal plays (select {r['num']})")
            restore.add_css_class("pill")
            restore.connect("clicked", lambda *_, r=r: self.confirm_select(r["num"], r))
            row.add_suffix(restore)
            if not r["deleted"]:
                row.add_suffix(icon_button("user-trash-symbolic", f"Delete #{r['num']} (delete {r['num']})",
                                           self.confirm_delete, r["num"], r))
            self.history_list.append(row)

    def _register_wave(self, loop, wave):
        key = loop_key(loop)
        self.waveforms.setdefault(key, []).append(wave)
        path = cache_path(loop)
        if os.path.exists(path):
            wave.set_peaks(peaks(read_wav_pcm(path)))
        else:
            wave.set_peaks(None, "press play to load")

    def _play_button(self, loop):
        key = loop_key(loop)
        b = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                       tooltip_text="Listen (loads the audio from the pedal the first time)")
        b.add_css_class("circular")
        b.connect("clicked", lambda *_: self.toggle_play(loop))
        self.play_buttons.setdefault(key, []).append(b)
        if self.playing_key == key and self.player.playing:
            b.set_icon_name("media-playback-pause-symbolic")
        return b

    # ---------------------------------------------------------- audio fetch / play

    def fetch(self, loop, then):
        """Make sure the loop's audio is in the cache, then call then(path)."""
        path = cache_path(loop)
        if os.path.exists(path):
            then(path)
            return

        def job(lp, progress):
            return ap09.fetch_loop_wav(lp, loop, progress=progress)

        def done(p):
            self._end_progress()
            wf = peaks(read_wav_pcm(p))
            for w in self.waveforms.get(loop_key(loop), []):
                w.set_peaks(wf)
            then(p)

        self._start_progress(f"Loading {fmt_secs(loop_secs(loop))} from the pedal…")
        self.worker.submit(job, done, self._job_error, self._on_progress)

    def toggle_play(self, loop):
        key = loop_key(loop)
        if self.playing_key == key and self.player.playing:
            self.player.pause()
            return
        if self.playing_key == key and self.player.uri:
            self.player.play(cache_path(loop))
            return

        def start(path):
            self.player.stop()
            self.playing_key = key
            self.pl_title.set_label(f"Loop · {fmt_secs(loop_secs(loop))}")
            self.pl_wave.set_peaks(peaks(read_wav_pcm(path)))
            self.player_rev.set_reveal_child(True)
            self.player.play(path)

        self.fetch(loop, start)

    def _toggle_bar_play(self):
        if self.player.playing:
            self.player.pause()
        elif self.playing_key:
            loop = next((r for r in getattr(self, "all_loops", self.records) if loop_key(r) == self.playing_key), None)
            if loop:
                self.player.play(cache_path(loop))

    def _on_play_state(self, playing):
        self.pl_btn.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        for key, buttons in self.play_buttons.items():
            for b in buttons:
                on = playing and key == self.playing_key
                b.set_icon_name("media-playback-pause-symbolic" if on else "media-playback-start-symbolic")
        if not playing and self.player.uri is None:
            self.player_rev.set_reveal_child(False)

    def _on_play_position(self, pos):
        self.pl_wave.set_position(pos)
        for key, waves in self.waveforms.items():
            for w in waves:
                w.set_position(pos if key == self.playing_key and pos is not None else None)

    def _on_hero_wave_click(self, gesture, n, x, y):
        if self.hero_loop is None:
            return
        if self.playing_key == loop_key(self.hero_loop) and self.player.uri:
            self.player.seek(x / max(1, self.hero_wave.get_width()))
        else:
            self.toggle_play(self.hero_loop)

    # ---------------------------------------------------------- download

    def download(self, loop):
        suffix = "-playing" if loop["current"] else ""
        dialog = Gtk.FileDialog(title="Save loop as WAV", initial_name=f"ap09-loop{loop['num']:02d}{suffix}.wav")

        def chosen(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            dest = f.get_path()

            def copy(path):
                import shutil
                shutil.copyfile(path, dest)
                self.toast(f"Saved {os.path.basename(dest)}")

            self.fetch(loop, copy)

        dialog.save(self, None, chosen)

    def download_all(self):
        dialog = Gtk.FileDialog(title="Choose a folder for all loops")

        def chosen(d, res):
            try:
                folder = d.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            todo = [(r["num"], r) for r in self.records]

            def step(k=0):
                if k >= len(todo):
                    self.toast(f"Saved {len(todo)} loops to {folder}")
                    return
                i, r = todo[k]

                def copy(path):
                    import shutil
                    suffix = "-playing" if r["current"] else ""
                    shutil.copyfile(path, os.path.join(folder, f"ap09-loop{i:02d}{suffix}.wav"))
                    step(k + 1)

                self.fetch(r, copy)

            step()

        dialog.select_folder(self, None, chosen)

    # ---------------------------------------------------------- upload

    def choose_upload(self):
        if not self.connected:
            self.toast("Connect the pedal first")
            return
        filt = Gtk.FileFilter(name="Audio files")
        for m in AUDIO_FILTER_MIME:
            filt.add_mime_type(m)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog = Gtk.FileDialog(title="Choose audio to put on the pedal", filters=filters)

        def chosen(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self.prepare_upload(f.get_path())

        dialog.open(self, None, chosen)

    def _on_drop(self, target, value, x, y):
        self.dropzone.remove_css_class("hover")
        files = value.get_files()
        if files:
            self.prepare_upload(files[0].get_path())
        return True

    def prepare_upload(self, path):
        if not path or not os.path.isfile(path):
            self.toast("Not a local file")
            return
        self._start_progress(f"Reading {os.path.basename(path)}…", pulse=True)

        def convert():
            try:
                pcm = ap09.load_audio(path)
                GLib.idle_add(self._confirm_upload, path, pcm)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._job_error, str(e))

        threading.Thread(target=convert, daemon=True).start()

    def _confirm_upload(self, path, pcm):
        self._end_progress()
        secs = len(pcm) / ap09.SAMPLE_WIDTH / ap09.SAMPLE_RATE
        blocks = -(-len(pcm) // (ap09.PAGE_AUDIO * ap09.PAGES_PER_BLOCK))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        wave = Waveform(height=80)
        wave.set_size_request(420, -1)
        wave.set_peaks(peaks(pcm))
        body.append(wave)
        space = ""
        if self.space:
            free = self.space[0]
            space = f"\nFree memory: {free} blocks" + ("  ⚠ not enough!" if free < blocks else "")
        dlg = Adw.AlertDialog(
            heading="Put this on the pedal?",
            body=f"{os.path.basename(path)}\n{fmt_secs(secs)} · {blocks} memory blocks{space}\n\n"
                 "It replaces the loop the pedal plays. The current loop stays in memory "
                 "and can be put back later.")
        dlg.set_extra_child(body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("upload", "Upload")
        dlg.set_response_appearance("upload", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("upload")

        def resp(d, r):
            if r == "upload":
                self.do_upload(path, pcm)

        dlg.connect("response", resp)
        dlg.present(self)

    def do_upload(self, path, pcm):
        def job(lp, progress):
            model = int.from_bytes(lp.read(ap09.AREA_MCU, 0x2180, 16)[4:8], "little")
            if model != 0x2715:
                raise ap09.DeviceError(f"model id 0x{model:04x} is not a NANO LOOPER; refusing to write")
            return ap09.upload_loop(lp, pcm, progress=progress)

        def done(blocks):
            self._end_progress()
            # seed the cache so the new loop can be previewed without reading it back
            padded = pcm[:len(pcm) - len(pcm) % ap09.SAMPLE_WIDTH]
            padded += bytes(-len(padded) % ap09.TAIL_UNIT)
            ap09.seed_cache({"length": len(padded), "blocks": blocks}, padded)
            self.done_dialog("Uploaded", f"{os.path.basename(path)} is on the pedal.\n\n"
                             "Unplug and replug the pedal so it loads the new loop.")
            self.refresh()

        self._start_progress(f"Uploading {os.path.basename(path)}…")
        self.worker.submit(job, done, self._job_error, self._on_progress)

    # ---------------------------------------------------------- select / clear / space

    def confirm_select(self, i, r):
        if r["current"]:
            self.toast(f"#{i} is already playing")
            return
        warn = ""
        if r["overwritten"]:
            warn = f"\n\n⚠ {r['overwritten']} of its {len(r['blocks'])} blocks were reused: it will sound damaged."
        dlg = Adw.AlertDialog(heading=f"Put #{i} on the pedal?",
                              body=f"{fmt_secs(loop_secs(r))}. It replaces the current loop "
                                   f"(which stays in memory).{warn}")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Put on pedal")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda d, resp: resp == "ok" and self._run_simple(
            lambda lp, p: ap09.select_loop(lp, r), f"#{i} is now playing. Replug the pedal to load it."))
        dlg.present(self)

    def confirm_delete(self, i, r):
        dlg = Adw.AlertDialog(heading=f"Delete #{i}?",
                              body=f"{fmt_secs(loop_secs(r))}. It disappears from the list. The audio "
                                   "cannot be wiped over USB and is overwritten when the memory is needed.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda d, resp: resp == "delete" and self._run_simple(
            lambda lp, p: ap09.delete_loop(lp, r), f"#{i} deleted"))
        dlg.present(self)

    def confirm_clear(self):
        if not any(r["current"] for r in self.records):
            self.toast("The pedal already has no loop")
            return
        dlg = Adw.AlertDialog(heading="Clear the pedal?",
                              body="The pedal will have no loop and the loop disappears from the list. "
                                   "Its audio stays in memory until it is needed again.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("clear", "Clear")
        dlg.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda d, resp: resp == "clear" and self._run_simple(
            lambda lp, p: ap09.clear_loop(lp), "Pedal cleared. Replug it to apply."))
        dlg.present(self)

    def _run_simple(self, fn, message):
        def done(_):
            self._end_progress()
            self.toast(message)
            self.refresh()

        self._start_progress("Writing…", pulse=True)
        self.worker.submit(fn, done, self._job_error)

    def scan_space(self):
        def done(result):
            self._end_progress()
            self.space = result
            free, slots = result
            secs = free * ap09.PER_BLOCK_S
            self.space_row.set_subtitle(f"{free} empty blocks · about {fmt_secs(secs)} of audio · "
                                        f"{slots} index slots left")
            self.space_level.set_visible(True)
            self.space_level.set_value(min(1.0, free / ap09.MAX_BLOCKS))

        self._start_progress("Scanning memory (about 4 minutes)…")
        self.worker.submit(lambda lp, p: ap09.count_space(lp, progress=p), done, self._job_error, self._on_progress)

    # ---------------------------------------------------------- progress / messages

    def _start_progress(self, text, pulse=False):
        import time
        self._progress_t0 = time.monotonic()
        self.progress_title.set_label(text)
        self.progress_detail.set_label("starting…")
        self.progress_pct.set_label("")
        self.progress.set_fraction(0)
        self.cancel_btn.set_label("Cancel")
        self.cancel_btn.set_sensitive(True)
        self.cancel_btn.set_visible(not pulse)
        self.progress_rev.set_reveal_child(True)
        self._pulsing = pulse
        if pulse:
            GLib.timeout_add(100, self._pulse)
        self.upload_btn.set_sensitive(False)
        self.refresh_btn.set_sensitive(False)

    def _pulse(self):
        if self._pulsing:
            self.progress.pulse()
        return self._pulsing

    def _on_progress(self, frac, text):
        import time
        self._pulsing = False
        self.progress.set_fraction(frac)
        self.progress_pct.set_label(f"{100 * frac:.0f}%")
        eta = ""
        elapsed = time.monotonic() - self._progress_t0
        if 0.03 < frac < 1 and elapsed > 2:
            left = elapsed * (1 - frac) / frac
            eta = f"  ·  about {int(left // 60)}:{int(left % 60):02d} left"
        self.progress_detail.set_label(text + eta)

    def _end_progress(self):
        self._pulsing = False
        self.progress_rev.set_reveal_child(False)
        self.upload_btn.set_sensitive(bool(self.connected))
        self.refresh_btn.set_sensitive(True)

    def _job_error(self, msg):
        self._end_progress()
        if msg == "__cancelled__":
            self.toast("Stopped. Nothing on the pedal changed.")
            self.refresh()
            return
        dlg = Adw.AlertDialog(heading="Something went wrong", body=msg)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    def done_dialog(self, heading, body):
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def show_about(self):
        def label(text, *classes, **kw):
            lbl = Gtk.Label(label=text, wrap=True, justify=kw.pop("justify", Gtk.Justification.CENTER), **kw)
            for c in classes:
                lbl.add_css_class(c)
            return lbl

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=32, margin_start=32, margin_end=32)
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        head.append(Gtk.Image(icon_name="audio-x-generic", pixel_size=96))
        head.append(label("AP-09 Looper", "title-1"))
        head.append(label(f"version {ap09.__version__}", "dim-label"))
        box.append(head)
        box.append(label(
            "Manage the loop stored on an Ammoon AP-09 nano looper from your computer, "
            "over the pedal's USB cable. The pedal has no official Linux software: its USB "
            "protocol was reverse-engineered from the Windows tool."))

        what = Adw.PreferencesGroup(title="What it does")
        for icon, title, sub in (
            ("media-playback-start-symbolic", "Listen", "play any loop on the computer, with its waveform"),
            ("document-save-symbolic", "Download", "save a loop as WAV: mono, 24-bit, 46875 Hz, bit-exact"),
            ("document-send-symbolic", "Upload", "put any audio file (wav, mp3, flac…) on the pedal"),
            ("edit-undo-symbolic", "Put back", "old loops stay in memory: make one play again"),
            ("user-trash-symbolic", "Delete / Clear", "remove loops from the list or leave the pedal empty"),
            ("drive-harddisk-symbolic", "Free space", "see how much audio still fits"),
        ):
            row = Adw.ActionRow(title=title, subtitle=sub)
            row.add_prefix(Gtk.Image(icon_name=icon))
            what.add(row)
        box.append(what)

        good = Adw.PreferencesGroup(title="Good to know")
        for title, sub in (
            ("Replug after changes", "after upload, put back, delete or clear, unplug and replug the pedal"),
            ("Audio is never wiped", "the pedal ignores erase over USB: deleted loops only disappear from the list"),
            ("One program at a time", "don't use the CLI (ap09.py) while this app is working on the pedal"),
        ):
            good.add(Adw.ActionRow(title=title, subtitle=sub))
        box.append(good)

        who = Adw.PreferencesGroup(title="Author")
        author = Adw.ActionRow(title="wdog", subtitle="wdog666@gmail.com", activatable=True)
        author.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
        author.add_suffix(Gtk.Image(icon_name="mail-send-symbolic"))
        author.connect("activated", lambda *_: Gio.AppInfo.launch_default_for_uri("mailto:wdog666@gmail.com", None))
        who.add(author)
        lic = Adw.ActionRow(title="MIT License",
                            subtitle="free to use, change and improve; keep the author's copyright notice")
        lic.add_prefix(Gtk.Image(icon_name="emblem-documents-symbolic"))
        who.add(lic)
        box.append(who)
        box.append(label("© 2026 wdog · not affiliated with Ammoon or Rowin · use at your own risk",
                         "dim-label", "caption"))

        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=Adw.Clamp(maximum_size=520, child=box), propagate_natural_height=True))
        view.add_top_bar(Adw.HeaderBar())
        dlg = Adw.Dialog(title="About AP-09 Looper", content_width=560, content_height=720, child=view)
        dlg.present(self)

    def _on_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_F5:
            self.refresh()
            return True
        if keyval == Gdk.KEY_space and self.playing_key:
            self._toggle_bar_play()
            return True
        if keyval == Gdk.KEY_o and state & Gdk.ModifierType.CONTROL_MASK:
            self.choose_upload()
            return True
        return False


class LooperApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = self.props.active_window or LooperWindow(self)
        win.present()


def main():
    return LooperApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
