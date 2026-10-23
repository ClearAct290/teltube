#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TelTube - YouTube over Telnet.

A telnet server that serves a YouTube-like text interface (home / search /
results / watch page) and a realtime 144p -> coloured block-character video
player with captions.  Every connection gets its own independent session.

Requirements:   pip install yt-dlp numpy pillow     +   ffmpeg on PATH
Run:            python TelTube.py [-c teltube.ini]
                (port 23 needs root/admin; or set another port in the config)

Video transport: yt-dlp does the YouTube side (client negotiation, SABR when
the installed yt-dlp supports it, PO tokens, ...).  In the default "pipe"
mode yt-dlp's own downloader writes the 144p stream to stdout and ffmpeg
turns it into raw frames, so TelTube never needs a direct media URL.
Keep yt-dlp updated:  pip install -U yt-dlp
"""
import argparse
import asyncio
import bisect
import configparser
import ipaddress
import json
import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request
from collections import deque

import numpy as np
from PIL import Image

log = logging.getLogger("teltube")
VERSION = "1.0"

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
DEFAULT_INI = """\
; TelTube configuration.  Missing keys fall back to these defaults.
; Restart TelTube after editing.

[server]
host = 0.0.0.0
port = 23
name = TelTube
; connection limits
max_clients = 20
max_clients_per_ip = 3
; seconds without a key press (not counting video playback) before disconnect, 0 = never
idle_timeout = 900
; comma separated IPs / CIDR ranges. Empty allow_ips = everybody allowed.
allow_ips =
deny_ips =
; used until the client reports its window size (telnet NAWS)
default_cols = 80
default_rows = 24
; seconds to wait for telnet option negotiation before drawing the first screen
negotiate_wait = 0.6
; fixed screen size used whenever a connection selects "Commodore BBS" at the
; start screen - real Commodore hardware has no resizable window and sends no
; NAWS, so this is not a default, it is the only size Commodore mode ever uses
commodore_cols = 40
commodore_rows = 25
; video frame rate for Commodore connections. Every frame is a full-screen
; repaint there (no cursor addressing in PETSCII), roughly cols*rows bytes, so
; the normal [video] fps would overrun a typical C64 WiFi modem / serial link.
commodore_video_fps = 3

[display]
; output charset for Generic Telnet connections: utf-8 or cp437 (cp437 = old
; BBS terminals; all block chars exist in it). Commodore BBS connections
; (chosen at the start screen) always use PETSCII and ignore this setting.
encoding = utf-8
; truecolor | 256 | 16 | auto (auto guesses from the telnet terminal type).
; Users can cycle colour mode with T and charset with E. Ignored in
; Commodore mode, which always uses the fixed 16-colour VIC-II palette.
color_mode = truecolor
; colour precision (bits per channel) for truecolor. Lower = fewer changed cells = less bandwidth
truecolor_bits = 5
; hex RGB accent colour
accent_color = ff3b3b

[video]
fps = 10
; seconds of video converted before the watch page appears
preload_seconds = 3
; how far ahead of the playhead frames are buffered
buffer_ahead_seconds = 20
; how much already-played video is kept (instant short rewinds)
history_seconds = 15
; frames that must be buffered before playback (re)starts after seek/stall
resume_buffer_frames = 5
seek_seconds = 5
; pipe   = yt-dlp downloads/streams, ffmpeg reads stdin (works with SABR-only formats)
; direct = yt-dlp only resolves a URL, ffmpeg opens it (faster seeking, needs a plain URL)
stream_mode = pipe
; only redraw character cells that changed (huge bandwidth saver)
delta_compression = true
; a pixel only counts as "changed" once it moves by more than this (0-255)
; since the last frame actually shown. Video compression noise shifts almost
; every pixel by a little bit every frame even when nothing is really moving,
; which makes a plain exact diff repaint nearly the whole screen each frame -
; this holds a pixel at its last value until the change is big enough to be
; real motion. 0 = exact diff (most sensitive to noise, closest to the old behaviour).
motion_threshold = 12
; unchanged cells between two changed cells that are re-sent instead of cursor-jumping
delta_gap_merge = 4
; force a complete redraw every N seconds to heal any corruption
full_refresh_seconds = 30
; ffmpeg scaler: area | bilinear | bicubic | lanczos
scale_flags = area
ffmpeg_path = ffmpeg
max_height = 144
captions_default = false
caption_lang = en

[search]
; literal search term, or 'random' to pick a different everyday topic word
; each time the home page refreshes (avoids hammering one single query).
home_term = random
home_count = 4
results_total = 12
results_per_page = 4
cache_ttl = 3600
home_cache_ttl = 900
cache_dir = cache
cache_persist = true

[ytdlp]
; leave empty for the automatic 144p selector
format =
; comma separated youtube:player_client values to try in order on a stream
; failure (e.g. "tv,web_safari,android"). Empty = let TelTube try yt-dlp's own
; default client first, then a built-in fallback list - useful when a client
; starts getting 403 Forbidden because it needs a PO token TelTube can't supply.
player_client =
cookies_file =
proxy =
; extra raw yt-dlp command line flags used for streaming (pipe mode)
extra_args =
socket_timeout = 15

[logging]
level = INFO
; empty = console only
file =
"""


class Config:
    """cfg.<section>_<key> -> value (typed by guessing from the text)."""

    def __init__(self, path):
        cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
        cp.read_string(DEFAULT_INI)
        if os.path.exists(path):
            cp.read(path, encoding="utf-8")
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(DEFAULT_INI)
            print(f"Wrote default config to {path}")
        self._cp = cp

    def __getattr__(self, name):
        sec, _, key = name.partition("_")
        try:
            raw = self._cp.get(sec, key).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            raise AttributeError(name)
        low = raw.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        if re.fullmatch(r"-?\d+\.\d+", raw):
            return float(raw)
        return raw


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def fmt_time(s):
    s = int(max(0, s))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_views(n):
    if n is None:
        return "views n/a"
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= lim:
            v = n / lim
            return (f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}") + f"{suf} views"
    return f"{int(n)} view" + ("" if int(n) == 1 else "s")


def clean(s):
    """Make text safe for a one-char-per-cell grid."""
    out = []
    for c in str(s):
        if c in "\r\n\t":
            c = " "
        o = ord(c)
        if o < 32 or o == 127 or unicodedata.combining(c):
            continue
        if unicodedata.east_asian_width(c) in "WF" or o >= 0x1F000 or 0x2600 <= o <= 0x27BF:
            c = "?"
        out.append(c)
    return "".join(out)


def to_petscii_text(s):
    """ASCII text -> bytes a real unshifted-mode Commodore will show as the
    intended characters (see PETSCII_SUBST note above): uppercase first
    (unshifted PETSCII has no lowercase letters - they're graphics symbols),
    substitute the handful of punctuation marks that differ, then drop
    anything outside the printable unshifted range."""
    out = bytearray()
    for ch in s.upper():
        o = ord(ch)
        o = PETSCII_SUBST.get(o, o)
        if o == 0x22:
            # A double quote switches the Commodore screen editor into "quote
            # mode", where every control code after it (colour, reverse, even
            # cursor moves) is printed as a reverse-video glyph instead of
            # being obeyed - one stray quote in a video title would wreck the
            # rest of the frame. A plain apostrophe looks the same enough.
            o = 0x27
        if 0x20 <= o <= 0x5D:
            out.append(o)
        else:
            out.append(ord("?"))
    return bytes(out)


def wrap(text, width, maxlines):
    width = max(1, width)
    lines, cur = [], ""
    for w in text.split():
        while len(w) > width:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:width])
            w = w[width:]
        if not w:
            continue
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > maxlines:
        lines = lines[:maxlines]
        last = lines[-1]
        lines[-1] = (last[: max(0, width - 3)].rstrip() + "...") if width > 3 else last[:width]
    return lines


def http_get(url, timeout=15, retries=3, backoff=1.5):
    """GET with retry/backoff on 429 and transient errors (runs in a worker
    thread, so blocking time.sleep() here is fine)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_exc = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_exc = e
            if (e.code == 429 or e.code >= 500) and attempt < retries - 1:
                wait = backoff * (attempt + 1)
                try:
                    wait = max(wait, float(e.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                log.debug("http_get %s -> HTTP %s, retrying in %.1fs", url, e.code, wait)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    raise last_exc


CHAR_ASPECT = 2.0  # typical monospace cell height/width ratio


def fit_dims(cols, rows, aspect, char_aspect=CHAR_ASPECT):
    """Size (w, h), in character cells, of a frame that fits a cols x rows
    terminal with one video pixel sample per cell (no half-block packing -
    see blit_ascii). char_aspect corrects for cells being taller than they
    are wide so the picture isn't stretched."""
    if cols / (rows * char_aspect) > aspect:
        h = rows
        w = int(round(h * char_aspect * aspect))
    else:
        w = cols
        h = int(round(w / (char_aspect * aspect)))
    h = max(1, min(h, rows))
    w = max(1, min(w, cols))
    return w, h


# Everyday topic words used to rotate the home page when [search] home_term = random,
# instead of hammering YouTube with the single literal query "trending".
HOME_WORDS = [
    "music", "news", "gaming", "cooking", "travel", "science", "comedy", "sports",
    "technology", "nature", "history", "art", "space", "animals", "movies", "diy",
    "fitness", "cars", "photography", "documentary", "programming", "guitar",
    "painting", "chess", "ocean", "mountains", "robots", "astronomy", "podcast",
    "review", "tutorial", "highlights", "live", "funny", "relaxing", "puzzle",
    "crafts", "weather", "wildlife", "engineering", "camping",
]


def _swallow_future_error(f):
    """Read a Future's exception so asyncio doesn't log 'exception was never
    retrieved' when nobody else ends up checking (e.g. captions fetched but
    the viewer never toggles captions on, or the page changes first)."""
    if not f.cancelled():
        f.exception()


# ----------------------------------------------------------------------------
# Colour modes + cell grid + terminal diff renderer
# ----------------------------------------------------------------------------
PAL16 = np.array([
    (0, 0, 0), (170, 0, 0), (0, 170, 0), (170, 85, 0), (0, 0, 170), (170, 0, 170), (0, 170, 170), (170, 170, 170),
    (85, 85, 85), (255, 85, 85), (85, 255, 85), (255, 255, 85), (85, 85, 255), (255, 85, 255), (85, 255, 255),
    (255, 255, 255)], np.int32)

# Approximate real Commodore 64 (VIC-II) 16-colour palette, in the standard
# POKE 53280/53281 colour-number order (0=black ... 15=light grey).
PAL_C64 = np.array([
    (0, 0, 0), (255, 255, 255), (136, 0, 0), (170, 255, 238), (204, 68, 204),
    (0, 204, 85), (0, 0, 170), (238, 238, 119), (221, 136, 85), (102, 68, 0),
    (255, 119, 119), (51, 51, 51), (119, 119, 119), (170, 255, 102), (0, 136, 255),
    (187, 187, 187)], np.int32)

# Real CHR$ colour-control-code byte for each PAL_C64 entry above, in the
# same order - these are plain Commodore 64 control characters, not ANSI.
PETSCII_COLOR = [144, 5, 28, 159, 156, 30, 31, 158, 129, 149, 150, 151, 152, 153, 154, 155]
PETSCII_CLEAR = 0x93    # clear screen + home (clr/home)
PETSCII_HOME = 0x13     # cursor to top-left only, no clear
PETSCII_REV_ON = 0x12   # reverse video on
PETSCII_REV_OFF = 0x92  # reverse video off
PETSCII_DEL = 0x14      # delete / backspace
PETSCII_DOWN = 0x11
PETSCII_UP = 0x91
PETSCII_RIGHT = 0x1D
PETSCII_LEFT = 0x9D
# CRSR keys a real Commodore keyboard sends (not ANSI arrow escape sequences).
PETSCII_CRSR = {PETSCII_DOWN: "DOWN", PETSCII_UP: "UP", PETSCII_RIGHT: "RIGHT", PETSCII_LEFT: "LEFT",
                 PETSCII_DEL: "BACKSPACE"}
# A handful of ASCII punctuation marks that are a different glyph (not the
# same character) in the default/unshifted PETSCII charset - e.g. code 0x5C
# is the British pound sign on a C64, not a backslash. Substituted for
# something sane so a real Commodore never shows an unintended symbol.
PETSCII_SUBST = {ord("\\"): ord("/"), ord("^"): ord("@"), ord("_"): ord("-"), ord("|"): ord(":"),
                 ord("{"): ord("("), ord("}"): ord(")"), ord("~"): ord("-"), ord("`"): ord("'")}
# Unshifted PETSCII is byte-identical to 1963 ASCII for 0x20-0x5D and 0x60-0x7E
# (digits, most punctuation, uppercase A-Z) - the only real differences are
# 0x5C/0x5E/0x5F (handled by PETSCII_SUBST above) and that lowercase ASCII
# letters are graphics symbols in unshifted mode, not letters. So PETSCII
# text output is just: uppercase it, then apply PETSCII_SUBST, then strip
# anything outside 0x20-0x7E. No full screen-code translation table needed
# because we never touch screen memory directly - everything goes over the
# wire as CHR$ values, exactly like a real Commodore telnet/BBS client.
PETSCII_BOX_H, PETSCII_BOX_V, PETSCII_BOX_CORNER = "-", "|", "+"  # plain-ASCII, see Grid.box


# One byte per possible grid cell value, built once - emit_petscii runs on
# every frame for every cell, so it must not call the converter per cell.
_PET_LUT = bytes(to_petscii_text(chr(o))[0] if o != 0 else 32 for o in range(256))

# Shared, plain-ASCII-only art building blocks (no Unicode anywhere - some
# telnet clients and every real Commodore terminal can't render it, and on
# several terminals a glyph it doesn't recognise breaks the rest of that
# line's layout rather than just failing to display).
SPINNER = "|/-"                    # no backslash - see PETSCII_SUBST above
ASCII_RAMP = " .:-=+*#%@"          # dark -> light, used for video/thumbnail art
BAR_FULL, BAR_EMPTY = "#", "-"      # progress-bar fill characters

THEME = dict(
    bg=(10, 10, 22), panel=(26, 28, 48), hl=(48, 52, 110), white=(240, 240, 248), grey=(150, 152, 172),
    dim=(84, 86, 108), cyan=(80, 220, 255), yellow=(255, 214, 80), green=(90, 230, 120), red=(255, 59, 59),
    accent=(255, 59, 59), black=(0, 0, 0), magenta=(224, 96, 255), blue=(70, 120, 255))


class ColorMode:
    def __init__(self, name, bits=5, palette16=None):
        self.name = name
        self.bits = max(3, min(8, bits))
        self.palette16 = PAL16 if palette16 is None else palette16
        self._code = {}
        self._sgr = {}

    def quant(self, a):
        """uint8 (...,3) -> int32 colour codes (...)."""
        a = np.asarray(a, dtype=np.uint8)
        if self.name == "truecolor":
            s = 8 - self.bits
            q = (a >> s) << s
            q |= q >> self.bits
            q = q.astype(np.int32)
            return (q[..., 0] << 16) | (q[..., 1] << 8) | q[..., 2]
        a = a.astype(np.int32)
        if self.name == "256":
            lv = (a * 5 + 127) // 255
            return 16 + 36 * lv[..., 0] + 6 * lv[..., 1] + lv[..., 2]
        d = ((a[..., None, :] - self.palette16) ** 2).sum(-1)
        return d.argmin(-1).astype(np.int32)

    def code(self, r, g, b):
        k = (r, g, b)
        c = self._code.get(k)
        if c is None:
            c = self._code[k] = int(self.quant(np.array([[[r, g, b]]], np.uint8))[0, 0])
        return c

    def sgr(self, f, b):
        k = (f, b)
        s = self._sgr.get(k)
        if s is None:
            if len(self._sgr) > 60000:
                self._sgr.clear()
            if self.name == "truecolor":
                s = f"\x1b[38;2;{f >> 16};{(f >> 8) & 255};{f & 255};48;2;{b >> 16};{(b >> 8) & 255};{b & 255}m"
            elif self.name == "256":
                s = f"\x1b[38;5;{f};48;5;{b}m"
            else:
                s = f"\x1b[{30 + f if f < 8 else 90 + f - 8};{40 + b if b < 8 else 100 + b - 8}m"
            self._sgr[k] = s
        return s


class Pal:
    def __init__(self, mode, accent, petscii=False):
        for k, v in THEME.items():
            setattr(self, k, mode.code(*(accent if k == "accent" else v)))
        if petscii:
            # In the C64 palette the dark-blue selection colour and the panel
            # colour both round to the same dark grey, so a selected item
            # looked exactly like an unselected one. Give the selection a
            # distinct, light colour (emit_petscii draws it as a solid bar).
            self.hl = mode.code(119, 119, 119)


class Grid:
    """rows x cols of (char, fg, bg).  Colours are already-quantised codes."""

    def __init__(self, rows, cols, mode, fg, bg):
        self.rows, self.cols, self.mode = rows, cols, mode
        self.ch = np.full((rows, cols), 32, np.int32)
        self.fg = np.full((rows, cols), fg, np.int32)
        self.bg = np.full((rows, cols), bg, np.int32)

    def copy(self):
        g = Grid.__new__(Grid)
        g.rows, g.cols, g.mode = self.rows, self.cols, self.mode
        g.ch, g.fg, g.bg = self.ch.copy(), self.fg.copy(), self.bg.copy()
        return g

    def put(self, r, c, ch, fg=None, bg=None):
        if 0 <= r < self.rows and 0 <= c < self.cols:
            self.ch[r, c] = ord(ch)
            if fg is not None:
                self.fg[r, c] = fg
            if bg is not None:
                self.bg[r, c] = bg

    def text(self, r, c, s, fg=None, bg=None):
        if not 0 <= r < self.rows:
            return
        for i, ch in enumerate(s):
            x = c + i
            if 0 <= x < self.cols:
                self.ch[r, x] = ord(ch)
                if fg is not None:
                    self.fg[r, x] = fg
                if bg is not None:
                    self.bg[r, x] = bg

    def center(self, r, s, fg=None, bg=None):
        self.text(r, (self.cols - len(s)) // 2, s, fg, bg)

    def fill(self, r, c, h, w, bg=None, fg=None, ch=32):
        r0, c0 = max(r, 0), max(c, 0)
        r1, c1 = min(r + h, self.rows), min(c + w, self.cols)
        if r1 <= r0 or c1 <= c0:
            return
        self.ch[r0:r1, c0:c1] = ch
        if bg is not None:
            self.bg[r0:r1, c0:c1] = bg
        if fg is not None:
            self.fg[r0:r1, c0:c1] = fg

    def box(self, r, c, h, w, fg, bg=None, double=False):
        """Plain-ASCII border only (+, -, | / +, =, | for emphasis) - no
        Unicode box-drawing glyphs, since those aren't valid in cp437-only,
        strict 7-bit, or PETSCII terminals and can corrupt a whole line's
        layout on a terminal that doesn't recognise them."""
        corner, horiz, vert = ("+", "=", "|") if double else ("+", "-", "|")
        for x in range(c + 1, c + w - 1):
            self.put(r, x, horiz, fg, bg)
            self.put(r + h - 1, x, horiz, fg, bg)
        for y in range(r + 1, r + h - 1):
            self.put(y, c, vert, fg, bg)
            self.put(y, c + w - 1, vert, fg, bg)
        self.put(r, c, corner, fg, bg)
        self.put(r, c + w - 1, corner, fg, bg)
        self.put(r + h - 1, c, corner, fg, bg)
        self.put(r + h - 1, c + w - 1, corner, fg, bg)

    def blit_ascii(self, r, c, rgb, ramp=ASCII_RAMP):
        """Draw an RGB pixel array (h, w, 3) as one plain ASCII character per
        pixel - the character is picked from `ramp` by brightness, the colour
        from the (quantised) pixel colour. One pixel sample = one terminal
        cell: no Unicode half-block trick, safe for any 7-bit/PETSCII
        terminal and for every colour mode."""
        h, w = rgb.shape[:2]
        r0, c0 = max(r, 0), max(c, 0)
        r1, c1 = min(r + h, self.rows), min(c + w, self.cols)
        if r1 <= r0 or c1 <= c0:
            return
        sr, sc = slice(r0 - r, r1 - r), slice(c0 - c, c1 - c)
        lum = (0.299 * rgb[..., 0].astype(np.float32) + 0.587 * rgb[..., 1].astype(np.float32)
               + 0.114 * rgb[..., 2].astype(np.float32))
        idx = np.clip((lum / 255.0 * (len(ramp) - 1)).round().astype(np.int32), 0, len(ramp) - 1)
        ramp_ord = np.array([ord(ch) for ch in ramp], np.int32)
        self.ch[r0:r1, c0:c1] = ramp_ord[idx[sr, sc]]
        self.fg[r0:r1, c0:c1] = self.mode.quant(rgb)[sr, sc]


# Which VIC-II colours are light enough to be worth showing as a reverse-video
# block with dark text on top (white search box, grey selection bar, ...).
PETSCII_BRIGHT = frozenset(i for i, (r, g, b) in enumerate(PAL_C64.tolist())
                           if (299 * r + 587 * g + 114 * b) // 1000 >= 90)


def emit_petscii(grid, bg_code):
    """Full-screen PETSCII repaint: HOME, then every cell in row-major order.

    Real Commodore hardware has no cursor-addressing escape codes (no ANSI
    CSI, no way to jump to an arbitrary row/col), so unlike emit() this
    cannot do a partial/delta repaint - the whole screen is walked top to
    bottom, left to right, every frame. Every cell is overwritten, so HOME is
    used rather than CLR/HOME (no flicker from a blank screen between frames).

    Colour model - the VIC-II has ONE screen background colour, and a
    character cell is one glyph in one colour. Reverse video fills the cell
    with the character's colour and draws the glyph in the screen colour
    (assumed black). So a cell whose own background is a light colour is sent
    as reverse video *coloured with that background* (solid block, dark glyph
    - the intended look of the search box and the selection bar), while a
    cell with a dark background (panels) is just drawn on the normal black
    screen in its foreground colour; reversing those would turn every dark
    panel into a bright white slab.

    The bottom-right cell is never written: printing into the last position
    of a real screen scrolls the whole display up one line."""
    out = bytearray([PETSCII_HOME])
    cur_color = None
    cur_rev = False
    rows, cols = grid.ch.shape
    last = rows * cols - 1
    lut = _PET_LUT
    bright = PETSCII_BRIGHT
    idx = 0
    for r in range(rows):
        chs, fgs, bgs = grid.ch[r].tolist(), grid.fg[r].tolist(), grid.bg[r].tolist()
        for c in range(cols):
            if idx == last:
                break
            idx += 1
            ch, fg, bg = chs[c], fgs[c], bgs[c]
            rev = bg != bg_code and (bg % 16) in bright
            if rev != cur_rev:
                out.append(PETSCII_REV_ON if rev else PETSCII_REV_OFF)
                cur_rev = rev
            ch = lut[ch] if ch < 256 else 63
            if rev or ch != 32:                    # a plain space needs no colour code
                col = bg % 16 if rev else fg % 16
                if col != cur_color:
                    out.append(PETSCII_COLOR[col])
                    cur_color = col
            out.append(ch)
        # no newline byte: the real screen wraps every `cols` characters on its
        # own, and a CR/LF would scroll a screen that is already exactly full.
    if cur_rev:
        out.append(PETSCII_REV_OFF)
    return bytes(out)


def emit(grid, mask, gap):
    """Escape sequence string that repaints the masked cells of grid."""
    mode = grid.mode
    out = []
    cur = None
    for r in np.nonzero(mask.any(axis=1))[0].tolist():
        cols = np.nonzero(mask[r])[0].tolist()
        chs, fgs, bgs = grid.ch[r].tolist(), grid.fg[r].tolist(), grid.bg[r].tolist()
        i, n = 0, len(cols)
        while i < n:
            s = e = cols[i]
            i += 1
            while i < n and cols[i] - e - 1 <= gap:
                e = cols[i]
                i += 1
            out.append(f"\x1b[{r + 1};{s + 1}H")
            for x in range(s, e + 1):
                c = chs[x]
                f, b = fgs[x], bgs[x]
                if c == 32 and cur is not None:
                    f = cur[0]  # foreground of a space is invisible
                if (f, b) != cur:
                    out.append(mode.sgr(f, b))
                    cur = (f, b)
                out.append(chr(c))
    out.append("\x1b[0m")
    return "".join(out)


# ----------------------------------------------------------------------------
# Captions
# ----------------------------------------------------------------------------
def load_captions(info, lang):
    """Return a sorted list of (start, end, text) for the default caption track."""
    def pick(tracks):
        keys = [k for k in tracks if k == lang] or [k for k in tracks if k.split("-")[0] == lang]
        for k in keys:
            for ext in ("json3", "vtt"):
                for f in tracks[k]:
                    if f.get("ext") == ext and f.get("url"):
                        return f
        return None

    f = pick(info.get("subtitles") or {}) or pick(info.get("automatic_captions") or {})
    if not f:
        return []
    try:
        txt = http_get(f["url"]).decode("utf-8", "replace")
    except Exception as e:
        log.warning("caption track fetch failed (%s) - continuing without captions", e)
        return []
    caps = []
    if f["ext"] == "json3":
        for ev in json.loads(txt).get("events", []):
            segs = ev.get("segs")
            if not segs:
                continue
            t = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
            if t:
                st = ev.get("tStartMs", 0) / 1000.0
                caps.append((st, st + ev.get("dDurationMs", 0) / 1000.0, clean(t)))
    else:
        ts = re.compile(r"(?:(\d+):)?(\d+):(\d+)[.,](\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d+)[.,](\d+)")

        def sec(h, m, s, ms):
            return int(h or 0) * 3600 + int(m) * 60 + int(s) + float("0." + ms)

        lines = txt.splitlines()
        i = 0
        while i < len(lines):
            m = ts.search(lines[i])
            i += 1
            if not m:
                continue
            g = m.groups()
            buf = []
            while i < len(lines) and lines[i].strip():
                buf.append(re.sub(r"<[^>]+>", "", lines[i]))
                i += 1
            t = " ".join(buf).strip()
            if t:
                caps.append((sec(*g[0:4]), sec(*g[4:8]), clean(t)))
    caps.sort(key=lambda c: c[0])
    return caps


# ----------------------------------------------------------------------------
# Video stream: yt-dlp -> ffmpeg -> raw RGB frames in a bounded buffer
# ----------------------------------------------------------------------------
class VideoStream:
    """Frames are addressed by absolute index (time = index / fps) so the clock
    never drifts: captions, timeline and playback all derive from the index."""

    def __init__(self, cfg, info, w, h, size_ver, fps=None):
        self.cfg, self.info, self.w, self.h, self.size_ver = cfg, info, w, h, size_ver
        self.fps = max(1, int(fps or cfg.video_fps))
        self.size = w * h * 3
        self.ahead = max(2, int(cfg.video_buffer_ahead_seconds * self.fps))
        self.hist = max(0, int(cfg.video_history_seconds * self.fps))
        dur = info.get("duration")
        self.maxidx = int(dur * self.fps) - 1 if dur else None
        self.frames = deque()
        self.base = self.cursor = self.prod = 0
        self.eof = False
        self.error = None
        self.closed = False
        self.task = None
        self.started = False       # user pressed SPACE at least once
        self.shown_idx = 0
        self.shown_data = None

    # --- buffer access
    def frame_at(self, idx):
        if self.base <= idx < self.prod:
            return self.frames[idx - self.base]
        return None

    def in_buffer(self, idx):
        return self.base <= idx < self.prod

    def trim(self):
        while self.frames and self.cursor - self.base > self.hist:
            self.frames.popleft()
            self.base += 1

    def advance(self):
        data = self.frames[self.cursor - self.base]
        self.shown_idx, self.shown_data = self.cursor, data
        self.cursor += 1
        self.trim()
        return data

    def skip(self):
        self.cursor += 1
        self.trim()

    def seek_local(self, idx):
        self.cursor = idx

    # --- lifecycle
    async def start(self, idx):
        await self.stop()
        self.frames.clear()
        self.base = self.cursor = self.prod = idx
        self.eof, self.error, self.closed = False, None, False
        self.task = asyncio.ensure_future(self._produce(idx))

    async def stop(self):
        self.closed = True
        t, self.task = self.task, None
        if t:
            t.cancel()
            try:
                await t
            except BaseException:
                pass

    def _client_list(self):
        """youtube:player_client values to try in order. An explicit config
        list wins; otherwise try yt-dlp's own default client first (None =
        no override), then a few clients that commonly dodge the PO-token/
        403 Forbidden issue a single fixed client can run into."""
        raw = str(self.cfg.ytdlp_player_client).strip()
        if raw:
            lst = [c.strip() for c in raw.split(",") if c.strip()]
            return lst or [None]
        return [None, "tv", "web_safari", "android"]

    def _commands(self, start_idx, player_client=None):
        cfg = self.cfg
        info = self.info
        S = start_idx / self.fps
        vf = f"fps={self.fps},scale={self.w}:{self.h}:flags={cfg.video_scale_flags}"
        out = ["-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        ff = [cfg.video_ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin"]
        direct = cfg.video_stream_mode == "direct" and info.get("stream_url")
        if direct:
            cmd = ff + ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            hdr = info.get("headers") or {}
            if hdr:
                cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in hdr.items())]
            if S > 0:
                cmd += ["-ss", f"{S:.3f}"]
            return cmd + ["-i", info["stream_url"]] + out, None
        fmt = App.format_selector(cfg)
        yt = [sys.executable, "-m", "yt_dlp", "-f", fmt, "-o", "-", "--quiet", "--no-warnings", "--no-progress",
              "--no-playlist", "--no-part", "--socket-timeout", str(cfg.ytdlp_socket_timeout)]
        if player_client:
            yt += ["--extractor-args", f"youtube:player_client={player_client}"]
        if cfg.ytdlp_cookies_file:
            yt += ["--cookies", str(cfg.ytdlp_cookies_file)]
        if cfg.ytdlp_proxy:
            yt += ["--proxy", str(cfg.ytdlp_proxy)]
        if cfg.ytdlp_extra_args:
            yt += shlex.split(str(cfg.ytdlp_extra_args))
        yt.append("https://www.youtube.com/watch?v=" + info["id"])
        cmd = ff + ["-i", "pipe:0"] + (["-ss", f"{S:.3f}"] if S > 0 else []) + out
        return cmd, yt

    async def _produce(self, start_idx):
        """Try each configured player_client in turn until one actually
        yields frames. A client that returns zero frames (e.g. HTTP 403
        because that client needs a PO token we don't have) is logged and
        the next client is tried; a client that produced frames and then
        legitimately hit end-of-stream is NOT retried."""
        clients = self._client_list()
        last_msg = None
        for i, client in enumerate(clients):
            if self.closed:
                return
            self.error = None
            result = await self._produce_attempt(start_idx, client)
            if result == "closed":
                return
            if result == "ended":
                self.eof = True
                return
            # result == "failed": zero frames from this client
            last_msg = self.error
            self.error = None
            if i < len(clients) - 1:
                log.info("stream client=%r gave no data (%s) - retrying with client=%r",
                          client, last_msg, clients[i + 1])
                await asyncio.sleep(0.3)
        self.eof = True
        self.error = last_msg or ("No video data received from any player client (video may be "
                                   "unavailable at 144p, or yt-dlp needs updating: pip install -U yt-dlp)")

    async def _produce_attempt(self, start_idx, player_client):
        """Run one yt-dlp -> ffmpeg pipeline for a single player_client.
        Returns 'ended' (reached a genuine end-of-stream after producing at
        least one frame), 'failed' (zero frames, see self.error) or 'closed'
        (stream was torn down from outside, e.g. user navigated away)."""
        proc = ytp = err_task = None
        try:
            cmd, yt = self._commands(start_idx, player_client)
            if yt:
                ytp = subprocess.Popen(yt, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=ytp.stdout if ytp else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            if ytp:
                ytp.stdout.close()
            err_task = asyncio.ensure_future(proc.stderr.read())
            while True:
                data = await proc.stdout.readexactly(self.size)
                while self.prod - self.cursor >= self.ahead and not self.closed:
                    await asyncio.sleep(0.03)
                if self.closed:
                    return "closed"
                self.frames.append(data)
                self.prod += 1
                self.trim()
        except asyncio.IncompleteReadError:
            if self.prod > start_idx:
                return "ended"
            msg = ""
            try:
                if ytp:
                    ytp.kill()
                    msg += (await asyncio.get_running_loop().run_in_executor(None, ytp.stderr.read)).decode(
                        "utf-8", "replace")
                msg += (await asyncio.wait_for(err_task, 3)).decode("utf-8", "replace")
            except Exception:
                pass
            msg = " ".join(msg.split())[-300:]
            self.error = msg or "No video data received (is the video available at 144p?)"
            return "failed"
        except FileNotFoundError as e:
            self.error = f"Program not found: {e.filename}"
            return "failed"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = f"Stream error: {e}"
            return "failed" if self.prod == start_idx else "ended"
        finally:
            for pr in (proc, ytp):
                try:
                    if pr is not None:
                        pr.kill()
                except Exception:
                    pass
            if err_task:
                err_task.cancel()
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except BaseException:
                    pass


# ----------------------------------------------------------------------------
# Shared application state (search cache, thumbnails, logo, yt-dlp glue)
# ----------------------------------------------------------------------------
class Nav(Exception):
    def __init__(self, state):
        super().__init__("nav")
        self.state = state


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sessions = set()
        self.ip_count = {}
        self.cache = {}
        self.inflight = {}
        self.thumb_img = {}
        self.thumb_px = {}
        self.last_home_term = str(cfg.search_home_term).strip() or "random"
        self.cache_dir = str(cfg.search_cache_dir)
        os.makedirs(os.path.join(self.cache_dir, "thumbs"), exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "search_cache.json")
        if cfg.search_cache_persist and os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, encoding="utf-8") as f:
                    self.cache = {k: tuple(v) for k, v in json.load(f).items()}
            except Exception as e:
                log.warning("could not read search cache: %s", e)
        self.allow = self._nets(cfg.server_allow_ips)
        self.deny = self._nets(cfg.server_deny_ips)
        self.accent = tuple(int(str(cfg.display_accent_color).lstrip("#").zfill(6)[i:i + 2], 16) for i in (0, 2, 4))

    # --- access control
    @staticmethod
    def _nets(s):
        return [ipaddress.ip_network(x.strip(), strict=False) for x in str(s).split(",") if x.strip()]

    def allowed(self, ip):
        try:
            a = ipaddress.ip_address(ip.split("%")[0])
            if a.version == 6 and a.ipv4_mapped:
                a = a.ipv4_mapped
        except ValueError:
            return False
        if any(a in n for n in self.deny):
            return False
        return not self.allow or any(a in n for n in self.allow)

    # --- yt-dlp
    @staticmethod
    def format_selector(cfg):
        if cfg.ytdlp_format:
            return str(cfg.ytdlp_format)
        h = cfg.video_max_height
        return f"bv[height<={h}][ext=webm]/bv[height<={h}]/bv*[height<={h}]/wv/worst"

    def ydl_opts(self, **extra):
        c = self.cfg
        o = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "skip_download": True,
             "socket_timeout": c.ytdlp_socket_timeout}
        if c.ytdlp_proxy:
            o["proxy"] = str(c.ytdlp_proxy)
        if c.ytdlp_cookies_file:
            o["cookiefile"] = str(c.ytdlp_cookies_file)
        o.update(extra)
        return o

    def _client_list(self):
        raw = str(self.cfg.ytdlp_player_client).strip()
        if raw:
            lst = [c.strip() for c in raw.split(",") if c.strip()]
            return lst or [None]
        return [None, "tv", "web_safari", "android"]

    def _search_sync(self, q, n):
        import yt_dlp
        with yt_dlp.YoutubeDL(self.ydl_opts(extract_flat=True)) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
        res = []
        for e in (info or {}).get("entries") or []:
            if not e or not e.get("id") or len(str(e["id"])) != 11:
                continue
            res.append(dict(id=e["id"], title=clean(e.get("title") or "?"),
                            channel=clean(e.get("channel") or e.get("uploader") or "Unknown"),
                            views=e.get("view_count"), duration=e.get("duration")))
        return res

    async def search(self, q, n, ttl):
        key = f"{n}:{q.strip().lower()}"
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        t = self.inflight.get(key)
        if t is None:
            t = asyncio.ensure_future(self._search_store(key, q, n))
            self.inflight[key] = t
            t.add_done_callback(lambda _t, k=key: self.inflight.pop(k, None))
        return await asyncio.shield(t)

    async def _search_store(self, key, q, n):
        res = await asyncio.get_running_loop().run_in_executor(None, self._search_sync, q, n)
        if not res:
            raise RuntimeError("No results found")
        self.cache[key] = (time.time(), res)
        if self.cfg.search_cache_persist:
            try:
                now = time.time()
                self.cache = {k: v for k, v in self.cache.items() if now - v[0] < 86400}
                with open(self.cache_file, "w", encoding="utf-8") as f:
                    json.dump(self.cache, f)
            except Exception as e:
                log.warning("could not write search cache: %s", e)
        return res

    async def get_home(self):
        c = self.cfg
        term = str(c.search_home_term).strip()
        if not term or term.lower() == "random":
            term = random.choice(HOME_WORDS)
        self.last_home_term = term
        vids = await self.search(term, c.search_home_count, c.search_home_cache_ttl)
        await self.ensure_thumbs(vids)
        return vids

    async def warm(self):
        while True:
            try:
                await self.get_home()
                log.info("home page cache ready")
            except Exception as e:
                log.warning("home refresh failed: %s", e)
            await asyncio.sleep(self.cfg.search_home_cache_ttl + 1)

    def extract_info(self, vid):
        import yt_dlp
        last_exc = None
        for client in self._client_list():
            o = self.ydl_opts(format=self.format_selector(self.cfg), ignore_no_formats_error=True)
            if client:
                o["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    i = ydl.extract_info("https://www.youtube.com/watch?v=" + vid, download=False)
                rf = (i.get("requested_formats") or [{}])[0]
                return dict(
                    id=vid, title=clean(i.get("title") or vid),
                    channel=clean(i.get("channel") or i.get("uploader") or "?"),
                    views=i.get("view_count"), duration=i.get("duration"),
                    width=i.get("width") or rf.get("width"), height=i.get("height") or rf.get("height"),
                    stream_url=i.get("url") if not i.get("requested_formats") else rf.get("url"),
                    headers=i.get("http_headers") or rf.get("http_headers") or {},
                    subtitles=i.get("subtitles") or {}, automatic_captions=i.get("automatic_captions") or {})
            except Exception as e:
                last_exc = e
                log.debug("extract_info client=%r failed: %s", client, e)
        raise last_exc or RuntimeError("could not read video info")

    # --- thumbnails
    def _fetch_thumb(self, vid):
        safe = re.sub(r"[^A-Za-z0-9_-]", "", vid)
        path = os.path.join(self.cache_dir, "thumbs", safe + ".jpg")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(http_get(f"https://i.ytimg.com/vi/{safe}/mqdefault.jpg"))
        img = Image.open(path).convert("RGB")
        img.load()
        return img

    async def ensure_thumbs(self, vids):
        loop = asyncio.get_running_loop()

        async def one(v):
            if v["id"] in self.thumb_img:
                return
            try:
                img = await loop.run_in_executor(None, self._fetch_thumb, v["id"])
                if len(self.thumb_img) > 300:
                    self.thumb_img.pop(next(iter(self.thumb_img)))
                self.thumb_img[v["id"]] = img
            except Exception as e:
                log.debug("thumbnail %s failed: %s", v["id"], e)

        await asyncio.gather(*(one(v) for v in vids))

    def thumb_pixels(self, vid, w, h):
        key = (vid, w, h)
        a = self.thumb_px.get(key)
        if a is None:
            img = self.thumb_img.get(vid)
            if img is None:
                return None
            if len(self.thumb_px) > 400:
                self.thumb_px.clear()
            res = getattr(Image, "Resampling", Image).LANCZOS
            a = self.thumb_px[key] = np.asarray(img.resize((w, h), res), dtype=np.uint8)
        return a

    # --- telnet connections
    async def handle(self, reader, writer):
        ip = (writer.get_extra_info("peername") or ("?",))[0]
        c = self.cfg
        if not self.allowed(ip):
            log.info("[%s] refused (ACL)", ip)
            writer.write(b"Access denied.\r\n")
            writer.close()
            return
        if len(self.sessions) >= c.server_max_clients or self.ip_count.get(ip, 0) >= c.server_max_clients_per_ip:
            log.info("[%s] refused (limits)", ip)
            writer.write(b"Server is full, try again later.\r\n")
            writer.close()
            return
        s = Session(self, reader, writer, ip)
        self.sessions.add(s)
        self.ip_count[ip] = self.ip_count.get(ip, 0) + 1
        log.info("[%s] connected (%d online)", ip, len(self.sessions))
        try:
            await s.run()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("[%s] session crashed", ip)
        finally:
            await s.close()
            self.sessions.discard(s)
            self.ip_count[ip] = self.ip_count.get(ip, 1) - 1
            if self.ip_count[ip] <= 0:
                self.ip_count.pop(ip, None)
            log.info("[%s] disconnected (%d online)", ip, len(self.sessions))


# ----------------------------------------------------------------------------
# One telnet client = one Session (own screen, own state, own video buffer)
# ----------------------------------------------------------------------------
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_BINARY, OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 0, 1, 3, 24, 31
CTRL_HINT = "1-5 Select   ARROWS Move   ENTER Go   9 Home   T Colors   E Charset"
CTRL_HINT_PETSCII = "1-5 PICK  CRSR MOVE  RETURN GO  9 HOME"   # 38 chars: fits 40 columns


class Session:
    def __init__(self, app, reader, writer, ip):
        self.app, self.cfg, self.reader, self.writer, self.ip = app, app.cfg, reader, writer, ip
        self.cols, self.rows = self.cfg.server_default_cols, self.cfg.server_default_rows
        self.size_ver = 0
        self.keys = deque()
        self.key_ev = asyncio.Event()
        self.closed = False
        self.inbuf = b""
        self.after_cr = False
        self.ttype = ""
        self.refused = set()
        self.enc = str(self.cfg.display_encoding)
        try:
            "x".encode(self.enc)
        except LookupError:
            self.enc = "utf-8"
        # term_mode is chosen by the user at the start screen (page_choose_terminal)
        # before anything else is drawn: "ansi" = Generic Telnet (what this whole
        # renderer did before), "petscii" = Commodore BBS (real C64 control codes,
        # fixed screen size, no NAWS/colour negotiation - see set_petscii below).
        self.term_mode = "ansi"
        self.chosen = False         # has the start screen been answered yet?
        self.auto_color = str(self.cfg.display_color_mode) == "auto"
        self.set_mode("truecolor" if self.auto_color else str(self.cfg.display_color_mode))
        self.prev = None
        self.last_full = 0.0
        self.last_key = time.monotonic()
        self._grad = {}
        self.vs = None
        self.vinfo = None
        self.captions = None
        self.cap_starts = []
        self.cap_fut = None
        self.captions_on = bool(self.cfg.video_captions_default)
        self._vkey = None
        self._vbase = None
        self._motion_key = None
        self._motion_prev = None
        self.rtask = None

    # ---- colour / charset
    def set_mode(self, name):
        if self.term_mode == "petscii":
            name = "16"
        if name not in ("truecolor", "256", "16"):
            name = "truecolor"
        self.mode = ColorMode(name, self.cfg.display_truecolor_bits,
                              PAL_C64 if self.term_mode == "petscii" else None)
        self.pal = Pal(self.mode, self.app.accent, self.term_mode == "petscii")
        self._grad = {}
        self._vkey = None
        self.prev = None

    def set_petscii(self):
        """Switch this session into real Commodore mode: fixed 40x25 screen
        (no NAWS - a real C64 can't report or change its window size), the
        actual VIC-II 16-colour palette, and PETSCII output/input instead of
        ANSI. Called once, from the start screen, before anything else is
        drawn."""
        self.term_mode = "petscii"
        self.auto_color = False
        self.enc = "petscii"
        w = max(20, min(int(self.cfg.server_commodore_cols), 80))
        h = max(10, min(int(self.cfg.server_commodore_rows), 60))
        self.cols, self.rows = w, h
        self.size_ver += 1
        self.set_mode("16")

    def video_fps(self):
        """Frame rate for this connection's video (lower on Commodore - see
        commodore_video_fps in the config)."""
        fps = int(self.cfg.video_fps)
        if self.term_mode == "petscii":
            fps = min(fps, max(1, int(self.cfg.server_commodore_video_fps)))
        return fps

    def cycle_mode(self):
        if self.term_mode == "petscii":
            return  # real VIC-II hardware only has the one 16-colour palette
        order = ["truecolor", "256", "16"]
        self.set_mode(order[(order.index(self.mode.name) + 1) % 3])

    def cycle_enc(self):
        if self.term_mode == "petscii":
            return  # Commodore mode's charset is fixed PETSCII, not a toggle
        self.enc = "cp437" if self.enc != "cp437" else "utf-8"
        self.prev = None

    def invalidate(self):
        self.prev = None

    def new_grid(self, video=False):
        p = self.pal
        return Grid(self.rows, self.cols, self.mode, p.white, p.black if video else p.bg)

    # ---- low level io
    def write(self, text):
        self.writer.write(text.encode(self.enc, "replace").replace(b"\xff", b"\xff\xff"))

    def raw(self, b):
        self.writer.write(bytes(b))

    def clear_screen_raw(self, extra_ansi=""):
        """Clear + home the screen outside of present()'s normal draw path
        (idle timeout, startup, shutdown, a jump straight into video
        playback). Must never send a literal ANSI escape sequence to a
        PETSCII session - a real Commodore has no CSI parser and would just
        print the raw escape bytes and bracket characters as text."""
        if self.term_mode == "petscii":
            self.raw(bytes([PETSCII_CLEAR]))
        elif self.chosen:
            self.write("\x1b[0m\x1b[2J" + extra_ansi)

    async def flush(self):
        try:
            await asyncio.wait_for(self.writer.drain(), 30)
        except asyncio.TimeoutError:
            raise ConnectionError("client too slow")

    async def present(self, g, delta=True):
        if self.term_mode == "petscii":
            # Real Commodore hardware has no cursor-addressing escapes, so
            # there is no equivalent of the ANSI delta/diff repaint below -
            # every call redraws the whole 40x25 screen from HOME. bg_code is
            # the grid's own corner-cell background (new_grid always fills
            # the whole grid with one flat background before anything else
            # is drawn onto it), used so ordinary cells come out normal video
            # and only genuinely highlighted cells (search box, selection)
            # come out reverse video.
            bg_code = int(g.bg[0, 0])
            self.raw(emit_petscii(g, bg_code))
            self.prev = g
            await self.flush()
            return
        now = time.monotonic()
        prev = self.prev
        pre = ""
        if (prev is None or prev.ch.shape != g.ch.shape or prev.mode is not g.mode):
            mask = np.ones(g.ch.shape, bool)
            pre = "\x1b[0m\x1b[2J"
            self.last_full = now
        elif not delta or now - self.last_full > self.cfg.video_full_refresh_seconds:
            mask = np.ones(g.ch.shape, bool)
            self.last_full = now
        else:
            mask = (g.ch != prev.ch) | (g.bg != prev.bg) | ((g.fg != prev.fg) & (g.ch != 32))
            if not mask.any():
                self.prev = g
                return
        self.write(pre + emit(g, mask, self.cfg.video_delta_gap_merge))
        self.prev = g
        await self.flush()

    # ---- telnet input
    def emit_key(self, k):
        if len(self.keys) < 64:
            self.keys.append(k)
            self.key_ev.set()

    def set_size(self, w, h):
        if self.term_mode == "petscii":
            return  # Commodore mode's screen size is fixed - see set_petscii
        if w > 0 and h > 0:
            w, h = max(40, min(w, 400)), max(12, min(h, 150))
            if (w, h) != (self.cols, self.rows):
                self.cols, self.rows = w, h
                self.size_ver += 1
                log.info("[%s] terminal size %dx%d", self.ip, w, h)

    def on_negotiate(self, cmd, opt):
        offered = (OPT_BINARY, OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS)
        if opt in offered:
            if cmd == WILL and opt == OPT_TTYPE:
                self.raw([IAC, SB, OPT_TTYPE, 1, IAC, SE])
            return
        if opt in self.refused:
            return
        if cmd == DO:
            self.refused.add(opt)
            self.raw([IAC, WONT, opt])
        elif cmd == WILL:
            self.refused.add(opt)
            self.raw([IAC, DONT, opt])

    def on_sb(self, d):
        if not d:
            return
        if d[0] == OPT_NAWS and len(d) >= 5:
            self.set_size((d[1] << 8) | d[2], (d[3] << 8) | d[4])
        elif d[0] == OPT_TTYPE and len(d) >= 2 and d[1] == 0:
            self.ttype = d[2:].decode("ascii", "ignore").lower()
            if self.auto_color and self.term_mode != "petscii":
                t = self.ttype
                if any(x in t for x in ("truecolor", "direct", "24bit")):
                    self.set_mode("truecolor")
                elif "256" in t:
                    self.set_mode("256")
                else:
                    self.set_mode("16")

    def parse(self, data):
        buf = self.inbuf + data
        i, n = 0, len(buf)
        while i < n:
            b = buf[i]
            if b == IAC:
                if i + 1 >= n:
                    break
                c = buf[i + 1]
                if c in (WILL, WONT, DO, DONT):
                    if i + 2 >= n:
                        break
                    self.on_negotiate(c, buf[i + 2])
                    i += 3
                elif c == SB:
                    j = buf.find(bytes([IAC, SE]), i + 2)
                    if j < 0:
                        break
                    self.on_sb(buf[i + 2:j])
                    i = j + 2
                else:
                    i += 2
                continue
            # Real Commodore cursor keys (0x11/0x91/0x1D/0x9D) and DEL (0x14)
            # are plain control bytes, not an ESC-prefixed sequence like ANSI
            # arrow keys. Only honoured in Commodore mode: 0x91/0x9D are also
            # ordinary UTF-8 continuation bytes, which must not turn into
            # arrow presses for a generic terminal.
            if self.term_mode == "petscii":
                if b in PETSCII_CRSR:
                    self.emit_key(PETSCII_CRSR[b])
                    i += 1
                    self.after_cr = False
                    continue
                if 0xC1 <= b <= 0xDA:           # SHIFT + letter on a Commodore keyboard
                    self.emit_key(chr(b - 0x80))
                    i += 1
                    self.after_cr = False
                    continue
            if b == 27:
                if i + 1 >= n:
                    break
                c = buf[i + 1]
                if c in (ord("["), ord("O")):
                    j = i + 2
                    while j < n and not (0x40 <= buf[j] <= 0x7E):
                        j += 1
                    if j >= n:
                        break
                    fin, par = chr(buf[j]), buf[i + 2:j].decode("ascii", "ignore")
                    k = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}.get(fin)
                    if fin == "~":
                        k = {"5": "PGUP", "6": "PGDN", "1": "HOME", "7": "HOME", "4": "END", "8": "END"}.get(par)
                    if k:
                        self.emit_key(k)
                    i = j + 1
                else:
                    # A real C64's RUN/STOP key is conventionally sent by
                    # Commodore telnet clients (e.g. VTX) as plain ESC, same
                    # as here, so this doubles as RUN/STOP in Commodore mode.
                    self.emit_key("ESC")
                    i += 1
                self.after_cr = False
                continue
            i += 1
            if b in (13, 10):
                if b == 10 and self.after_cr:
                    self.after_cr = False
                    continue
                self.after_cr = b == 13
                self.emit_key("ENTER")
                continue
            if b == 0 and self.after_cr:
                self.after_cr = False
                continue
            self.after_cr = False
            if b in (8, 127):
                self.emit_key("BACKSPACE")
            elif b == 9:
                self.emit_key("TAB")
            elif b == 32:
                self.emit_key("SPACE")
            elif 32 < b < 127:
                self.emit_key(chr(b))
        self.inbuf = buf[i:]

    async def _read_loop(self):
        try:
            while True:
                lone_esc = self.inbuf == b"\x1b"
                try:
                    data = await asyncio.wait_for(self.reader.read(4096), 0.06 if lone_esc else None)
                except asyncio.TimeoutError:
                    self.inbuf = b""
                    self.emit_key("ESC")
                    continue
                if not data:
                    break
                self.parse(data)
        except (ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            self.key_ev.set()

    def try_key(self):
        if self.keys:
            self.last_key = time.monotonic()
            return self.keys.popleft()
        if self.closed:
            raise ConnectionError("closed")
        return None

    async def get_key(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            k = self.try_key()
            if k is not None:
                return k
            idle = self.cfg.server_idle_timeout
            if idle and time.monotonic() - self.last_key > idle:
                self.clear_screen_raw("\x1b[H")
                if self.term_mode == "petscii":
                    self.raw(to_petscii_text("\r\nIdle timeout - goodbye.\r\n"))
                else:
                    self.write("\r\nIdle timeout - goodbye.\r\n")
                await self.flush()
                raise ConnectionError("idle")
            rem = None if end is None else end - time.monotonic()
            if rem is not None and rem <= 0:
                return None
            self.key_ev.clear()
            try:
                await asyncio.wait_for(self.key_ev.wait(), 5 if rem is None else min(rem, 5))
            except asyncio.TimeoutError:
                pass

    # ---- lifecycle
    async def run(self):
        self.raw([IAC, WILL, OPT_ECHO, IAC, WILL, OPT_SGA, IAC, WILL, OPT_BINARY, IAC, DO, OPT_BINARY,
                  IAC, DO, OPT_NAWS, IAC, DO, OPT_TTYPE])
        await self.flush()
        self.rtask = asyncio.ensure_future(self._read_loop())
        await asyncio.sleep(self.cfg.server_negotiate_wait)
        # nothing is drawn or escaped until the start screen has been answered
        state = ("choose_terminal",)
        while True:
            try:
                state = await getattr(self, "page_" + state[0])(*state[1:])
            except Nav as n:
                state = n.state
            except ConnectionError:
                raise
            except Exception as e:
                log.exception("[%s] page error", self.ip)
                state = ("error", str(e) or e.__class__.__name__, ("home",))

    async def close(self):
        if self.rtask:
            self.rtask.cancel()
        try:
            await self.drop_video()
        except BaseException:
            pass
        try:
            if self.term_mode == "petscii":
                self.raw(bytes([PETSCII_CLEAR]))
            elif self.chosen:
                self.write("\x1b[0m\x1b[2J\x1b[H\x1b[?7h\x1b[?25h")
            await asyncio.wait_for(self.writer.drain(), 2)
        except BaseException:
            pass
        try:
            self.writer.close()
        except Exception:
            pass

    async def drop_video(self):
        vs, self.vs = self.vs, None
        if vs:
            await vs.stop()
        self.vinfo = self.captions = self.cap_fut = None
        self.cap_starts = []
        self._vkey = None
        self._motion_key = None
        self._motion_prev = None

    # ---- caption access
    def caps(self):
        if self.captions is None and self.cap_fut is not None and self.cap_fut.done():
            try:
                self.captions = self.cap_fut.result()
            except Exception as e:
                log.info("[%s] captions failed: %s", self.ip, e)
                self.captions = []
            self.cap_starts = [c[0] for c in self.captions]
        return self.captions

    def caption_at(self, T):
        caps = self.caps()
        if not caps:
            return ""
        i = bisect.bisect_right(self.cap_starts, T) - 1
        for j in range(i, max(-1, i - 8), -1):
            if caps[j][1] > T:
                return caps[j][2]
        return ""

    # ---- generic menu with arrow / number navigation
    @staticmethod
    def nav(layout, sel, k):
        ri = ci = None
        for r, row in enumerate(layout):
            if sel in row:
                ri, ci = r, row.index(sel)
                break
        if ri is None:
            return sel
        if k == "LEFT":
            if ci > 0:
                ci -= 1
            elif ri > 0:
                ri -= 1
                ci = len(layout[ri]) - 1
        elif k == "RIGHT":
            if ci < len(layout[ri]) - 1:
                ci += 1
            elif ri < len(layout) - 1:
                ri += 1
                ci = 0
        elif k == "UP" and ri > 0:
            ri -= 1
            ci = min(ci, len(layout[ri]) - 1)
        elif k == "DOWN" and ri < len(layout) - 1:
            ri += 1
            ci = min(ci, len(layout[ri]) - 1)
        return layout[ri][ci]

    async def menu(self, draw, keys, layout, sel=0, extra=()):
        while True:
            ver = self.size_ver
            g = self.new_grid()
            draw(g, sel)
            await self.present(g)
            redraw = False
            while not redraw:
                k = await self.get_key(0.4)
                if k is None:
                    redraw = ver != self.size_ver
                elif k in ("LEFT", "RIGHT", "UP", "DOWN"):
                    sel = self.nav(layout, sel, k)
                    redraw = True
                elif k == "ENTER":
                    return ("opt", sel)
                elif k == "9":
                    return ("home",)
                elif k in keys:
                    sel = keys.index(k)
                    redraw = True
                elif k.lower() == "t":
                    self.cycle_mode()
                    redraw = True
                elif k.lower() == "e":
                    self.cycle_enc()
                    redraw = True
                elif k in extra or k.lower() in extra:
                    return ("key", k.lower() if len(k) == 1 else k)

    async def spinner_wait(self, task, title, lines=(), min_time=0.6, esc=("home",)):
        t0 = time.monotonic()
        try:
            while not (task.done() and time.monotonic() - t0 >= min_time):
                g = self.new_grid()
                self.chrome(g)
                self.draw_loading(g, title, lines, int(time.monotonic() * 10))
                await self.present(g)
                k = await self.get_key(0.1)
                if k == "9":
                    raise Nav(("home",))
                if k == "ESC":
                    raise Nav(esc)
        except BaseException:
            task.cancel()
            raise
        return task.result()

    # ======================================================================
    # drawing helpers
    # ======================================================================
    def gradient(self):
        key = self.cols
        if key not in self._grad:
            t = np.linspace(0, 1, self.cols)[:, None]
            st = np.array([[255, 59, 59], [224, 96, 255], [70, 120, 255]], float)
            a = np.where(t < 0.5, st[0] + (st[1] - st[0]) * (t * 2), st[1] + (st[2] - st[1]) * ((t - 0.5) * 2))
            self._grad[key] = self.mode.quant(a.astype(np.uint8))
        return self._grad[key]

    def chrome(self, g, home_sel=False, info=""):
        p, C, R = self.pal, self.cols, self.rows
        if self.term_mode == "petscii":
            # A per-cell colour gradient has no real equivalent in Commodore
            # text mode (one screen background, one colour per character) -
            # and if a gradient's own cell values happened to land back on
            # the base background colour, emit_petscii's bg-vs-base-colour
            # test would read those cells as "not reverse video", breaking
            # up what should be one solid bar. A flat reverse-video bar
            # (p.hl, a colour that never equals the grid's own background)
            # is what this becomes on real hardware, so draw that directly.
            g.fill(0, 0, 1, C, bg=p.hl)
        else:
            g.bg[0, :] = self.gradient()
        g.text(0, 1, f" {self.cfg.server_name} ", p.white)
        clock = time.strftime("%H:%M")
        g.text(0, C - len(clock) - 2, clock, p.white)
        g.fill(R - 3, 0, 2, C, bg=p.panel)
        if home_sel:
            g.text(R - 3, 2, " [9] HOME ", p.black, p.white)
        else:
            g.text(R - 3, 2, " [9] HOME ", p.white, p.accent)
        if info:
            g.text(R - 3, C - len(info) - 2, info, p.yellow, p.panel)
        hint = CTRL_HINT_PETSCII if self.term_mode == "petscii" else CTRL_HINT
        g.center(R - 2, hint[:C - 2], p.grey, p.panel)
        if self.term_mode == "petscii":
            g.fill(R - 1, 0, 1, C, bg=p.hl)
        else:
            g.bg[R - 1, :] = self.gradient()
        foot = f"{self.cfg.server_name} {VERSION} - YouTube over Telnet - {len(self.app.sessions)} online"
        if len(foot) > C - 2:                 # narrow screen: shorten instead of cutting mid-word
            foot = f"{self.cfg.server_name} {VERSION} - {len(self.app.sessions)} online"
        g.center(R - 1, foot[:C - 2], p.white)

    LOGO_TEXT = "[>] TELTUBE"
    LOGO_SPLIT = 4  # index where the accent colour starts ("[>] " vs "TELTUBE")
    LOGO_H = 1       # the logo is always exactly one row now

    def logo_width(self):
        """Width in cells draw_logo() will use, without actually drawing -
        needed to center it (e.g. on the loading screen)."""
        return len(self.LOGO_TEXT)

    def draw_logo(self, g, r, c):
        """A single-row, plain-ASCII wordmark - no Unicode block glyphs and
        no image conversion, so it's always correct (never blurry, never a
        risk of an unsupported character breaking a terminal's layout) on
        any terminal, including a real Commodore one."""
        p = self.pal
        g.text(r, c, self.LOGO_TEXT[:self.LOGO_SPLIT], p.dim)
        g.put(r, c + 1, ">", p.accent)
        g.text(r, c + self.LOGO_SPLIT, self.LOGO_TEXT[self.LOGO_SPLIT:], p.accent)
        return len(self.LOGO_TEXT)

    def rule(self, g, r, c, w, label, fg):
        p = self.pal
        g.text(r, c, "-" * max(0, w), p.dim)
        if label:
            g.text(r, c, f" {label} ", fg)

    def draw_searchbar(self, g, y, sel, text, flash=False):
        p, C = self.pal, self.cols
        x, w = 2, C - 4
        bgp = p.white if flash else (p.hl if sel else p.panel)
        g.fill(y, x, 3, w, bg=bgp)
        g.box(y, x, 3, w, p.accent if sel else p.dim, bgp, double=sel)
        g.text(y + 1, x + 2, " 1 ", p.black if sel else p.white, p.white if sel else p.accent)
        if flash:
            g.text(y + 1, x + 6, (text or "")[:w - 12], p.black, bgp)
        elif text:
            g.text(y + 1, x + 6, text[:w - 20], p.white, bgp)
        else:
            g.text(y + 1, x + 6, "Search TelTube...", p.grey, bgp)
        if sel and not flash:
            g.text(y + 1, x + w - 10, "[ENTER]", p.yellow, bgp)

    def draw_loading(self, g, title, lines, tick, pct=None):
        p, C, R = self.pal, self.cols, self.rows
        y = max(2, R // 2 - 5)
        if R >= 26:
            self.draw_logo(g, 1, (C - self.logo_width()) // 2)
        for i, l in enumerate(wrap(title, C - 8, 2)):
            g.center(y + i, l, p.white)
        y += 3
        g.center(y, "[ " + SPINNER[tick % len(SPINNER)] + " ]", p.accent)
        y += 2
        if pct is not None:
            w = min(50, C - 14)
            filled = int(w * pct / 100)
            x = (C - w) // 2
            g.text(y, x, BAR_FULL * filled, p.accent)
            g.text(y, x + filled, BAR_EMPTY * (w - filled), p.dim)
            g.center(y + 1, f"{int(pct)}%", p.yellow)
            y += 3
        for i, l in enumerate(lines):
            g.center(y + i, l[:C - 4], p.grey)
        g.center(self.rows - 4, "ESC = cancel     9 = Home", p.dim)

    def tile_geometry(self, y):
        C = self.cols
        gap = 2
        cw = max(8, (C - 4 - gap * 3) // 4)
        return gap, cw

    def draw_video_tiles(self, g, y, sel_base, sel, vids, avail_rows):
        """Shared video-list renderer for the home/results pages. In ANSI
        mode this is the usual 4-wide thumbnail grid; that grid's minimum
        tile width (gap*3 + 4*8 = 38) barely fits even an 80-column ANSI
        terminal and cannot fit a real Commodore 40-column screen at all, so
        Commodore mode instead lists videos one per row - plain text only,
        no thumbnail blit (a useful pixel-art thumbnail needs more width per
        tile than a 40-column screen has to give one, let alone four).
        Returns the number of rows this drew, so the caller can lay out
        whatever comes after it."""
        p, C = self.pal, self.cols
        tile_h = max(2, min(int(round(self.tile_geometry(y)[1] * 9 / 16 / 2)), avail_rows - 5)) + 5
        # Thumbnail tiles only when the terminal is both wide enough and tall
        # enough for them. Otherwise (Commodore, narrow, or short windows -
        # the tiles used to overflow into the footer there) list one video per
        # row, plain text only.
        if self.term_mode == "petscii" or C < 56 or avail_rows < tile_h:
            two_line = avail_rows >= 2 * len(vids)   # else one cramped line each
            rows_per = 2 if two_line else 1
            n = max(1, min(len(vids), avail_rows // rows_per))
            for i, v in enumerate(vids[:n]):
                s = sel == sel_base + i
                row = y + i * rows_per
                hl = p.hl if s else p.bg
                g.fill(row, 0, rows_per, C, bg=hl)
                num = f"{i + 2}" if sel_base else f"{i + 1}"
                g.text(row, 0, f"{num})", p.yellow if s else p.white, hl)
                if two_line:
                    g.text(row, 3, v["title"][:C - 4], p.white, hl)
                    d = fmt_time(v["duration"]) if v.get("duration") else "--:--"
                    g.text(row + 1, 3, f"{v['channel']} - {d}"[:C - 4], p.cyan, hl)
                else:
                    g.text(row, 3, f"{v['title']} - {v['channel']}"[:C - 4], p.white, hl)
            return n * rows_per
        gap, cw = self.tile_geometry(y)
        th = max(2, min(int(round(cw * 9 / 16 / 2)), avail_rows - 5))
        n = len(vids)
        for i, v in enumerate(vids):
            x = 2 + i * (cw + gap)
            s = sel == sel_base + i
            bgc = p.hl if s else p.bg
            g.fill(y, x - 1, th + 5, cw + 2, bg=bgc)
            g.text(y, x, f" {i + (2 if sel_base else 1)} ", p.black if s else p.white, p.white if s else p.accent)
            d = fmt_time(v["duration"]) if v.get("duration") else ""
            g.text(y, x + cw - len(d), d, p.yellow, bgc)
            px = self.app.thumb_pixels(v["id"], cw, th)
            if px is not None:
                g.blit_ascii(y + 1, x, px)
            else:
                g.fill(y + 1, x, th, cw, bg=p.panel)
                g.text(y + 1 + th // 2, x + 1, "(no image)"[:cw], p.dim, p.panel)
            for k, l in enumerate(wrap(v["title"], cw, 2)):
                g.text(y + th + 1 + k, x, l, p.yellow if s else p.white, bgc)
            g.text(y + th + 3, x, v["channel"][:cw], p.cyan, bgc)
            g.text(y + th + 4, x, fmt_views(v.get("views"))[:cw], p.grey, bgc)
        return th + 5

    # ======================================================================
    # pages (each returns the next state tuple)
    # ======================================================================
    def draw_home(self, g, sel, vids, err):
        p, C, R = self.pal, self.cols, self.rows
        self.chrome(g, sel == len(vids) + 1)
        lw = self.draw_logo(g, 1, 2)
        lr = self.LOGO_H
        if self.term_mode != "petscii":
            g.text(2, 2 + lw + 3, "Watch YouTube from your terminal", p.grey)
            g.text(3, 2 + lw + 3, "No audio over telnet - captions recommended (C)", p.dim)
        y = lr + 2
        self.draw_searchbar(g, y, sel == 0, "")
        y += 4
        self.rule(g, y, 2, C - 4, "TRENDING", p.accent)
        y += 1
        if err:
            g.text(y + 1, 3, "Recommendations unavailable:", p.red)
            for i, l in enumerate(wrap(err, C - 8, 3)):
                g.text(y + 2 + i, 3, l, p.grey)
            g.text(y + 6, 3, "You can still use the search bar (1).", p.white)
            return
        avail = max(1, R - 4 - y)
        self.draw_video_tiles(g, y, 1, sel, vids, avail)

    async def page_choose_terminal(self):
        """First thing every connection sees, before any escape sequence or
        control code is sent. It is deliberately plain uppercase 7-bit text:
        at this point we do not know whether the other end is an ANSI terminal
        or a Commodore, and a Commodore would print raw ESC/CSI bytes as
        garbage (and show lowercase letters as graphics symbols). Picking
        Commodore locks the session to the fixed Commodore screen and PETSCII
        control codes (Session.set_petscii); picking Generic switches on the
        ANSI setup that used to be sent unconditionally at connect time."""
        self.invalidate()
        t = self.ttype
        guess = 2 if any(x in t for x in ("petscii", "c64", "commodore", "cbm")) else 1
        mark1 = "  (DETECTED)" if guess == 1 and t else ""
        mark2 = "  (DETECTED)" if guess == 2 else ""
        lines = ["", "TELTUBE - YOUTUBE OVER TELNET", "",
                 "WHAT KIND OF TERMINAL ARE YOU USING?", "",
                 "  1 = GENERIC TELNET" + mark1,
                 "      (ANSI COLOUR, ANY SIZE)", "",
                 "  2 = COMMODORE BBS" + mark2,
                 "      (PETSCII, 40 COLUMNS X 25)", "",
                 "PRESS 1 OR 2 (RETURN = " + ("2" if guess == 2 else "1") + "): "]
        # push whatever was on screen out of view (a Commodore screen scrolls
        # cleanly; a modern terminal just scrolls its history), then the menu
        text = "\r\n" * max(self.rows, 25) + "\r\n".join(lines)
        self.raw(text.encode("ascii", "replace"))
        await self.flush()
        while True:
            k = await self.get_key()
            ku = k.upper() if len(k) == 1 else k
            if ku == "ENTER":
                ku = str(guess)
            if ku in ("1", "G", "T"):
                self.chosen = True
                self.write("\x1b[0m\x1b[2J\x1b[?25l\x1b[?7l")
                return ("home",)
            if ku in ("2", "C"):
                self.chosen = True
                self.set_petscii()
                self.clear_screen_raw()
                return ("home",)


    async def page_home(self):
        await self.drop_video()
        self.invalidate()
        task = asyncio.ensure_future(self.app.get_home())
        vids, err = [], None
        try:
            vids = (await self.spinner_wait(task, "Loading recommendations...",
                                            (f'Searching YouTube for "{self.app.last_home_term}"',), min_time=0))[:4]
        except Nav:
            raise
        except ConnectionError:
            raise
        except Exception as e:
            err = str(e)[:200] or "unknown error"
        n = len(vids)
        keys = ["1"] + [str(i + 2) for i in range(n)] + ["9"]
        layout = [[0]] + ([list(range(1, n + 1))] if n else []) + [[n + 1]]
        r = await self.menu(lambda g, s: self.draw_home(g, s, vids, err), keys, layout)
        if r[0] == "opt":
            if r[1] == 0:
                return ("search",)
            if r[1] <= n:
                return ("load", vids[r[1] - 1], ("home",))
        return ("home",)

    async def page_search(self, q=""):
        await self.drop_video()
        self.invalidate()
        p = self.pal

        def draw(flash=False):
            g = self.new_grid()
            self.chrome(g)
            self.draw_logo(g, 1, 2)
            C = self.cols
            y = self.LOGO_H + 3
            w = min(C - 8, 64)
            x = (C - w) // 2
            g.center(y, "S E A R C H", p.accent)
            bgp = p.white if flash else p.panel
            g.fill(y + 2, x, 3, w, bg=bgp)
            g.box(y + 2, x, 3, w, p.accent, bgp, double=True)
            shown = (q + ("#" if self.term_mode == "petscii" else "_"))[-(w - 6):]   # "_" is not a real glyph on a Commodore
            g.text(y + 3, x + 2, " " + shown, p.black if flash else p.white, bgp)
            g.center(y + 6, "Type what you want to watch, then press ENTER", p.grey)
            g.center(y + 7, "ESC (or 9 on an empty box) = Home", p.dim)
            return g

        while True:
            await self.present(draw())
            k = await self.get_key(0.4)
            if k is None:
                continue
            if k == "ESC" or (k == "9" and not q):
                return ("home",)
            if k == "BACKSPACE":
                q = q[:-1]
            elif k == "SPACE":
                q += " " if len(q) < 100 else ""
            elif len(k) == 1 and len(q) < 100:
                q += k
            elif k == "ENTER" and q.strip():
                for _ in range(2):  # flicker twice
                    await self.present(draw(True))
                    await asyncio.sleep(0.09)
                    await self.present(draw(False))
                    await asyncio.sleep(0.09)
                return ("search_run", q.strip())

    async def page_search_run(self, q):
        log.info("[%s] search: %r", self.ip, q)
        c = self.cfg
        task = asyncio.ensure_future(self.app.search(q, c.search_results_total, c.search_cache_ttl))
        try:
            res = await self.spinner_wait(task, f'Searching for "{q}"', ("Asking YouTube...",), esc=("search",))
        except (Nav, ConnectionError):
            raise
        except Exception as e:
            return ("error", str(e) or "Search failed", ("search",))
        return ("results", q, res, 0)

    def draw_scroll_btn(self, g, y, x, w, sel, label, arrow):
        p = self.pal
        bgc = p.hl if sel else p.panel
        g.fill(y, x, 3, w, bg=bgc)
        g.box(y, x, 3, w, p.accent if sel else p.dim, bgc, double=sel)
        # '^' is a different glyph on a Commodore, so Commodore mode has no arrow
        text = label if self.term_mode == "petscii" else f"{arrow} {label}"
        if len(text) > w - 2:
            text = label                      # narrow button: drop the arrow first
        text = text[:max(0, w - 2)]
        # centred inside the button itself - this used to be g.center(), which
        # centres on the whole screen and so landed outside (and on top of the
        # border of) any button that was not in the middle of the screen
        g.text(y + 1, x + (w - len(text)) // 2, text, p.black if sel else p.white, bgc)

    def draw_results(self, g, sel, q, res, page, pages, show_prev, show_next, prev_idx, next_idx, home_idx):
        p, C, R = self.pal, self.cols, self.rows
        chunk = res[page * 4:page * 4 + 4]
        self.chrome(g, sel == home_idx, f"Page {page + 1}/{pages}" if pages > 1 else "")
        self.draw_logo(g, 1, 2)
        y = self.LOGO_H + 2
        self.draw_searchbar(g, y, sel == 0, q)
        y += 4
        self.rule(g, y, 2, C - 4, f"RESULTS ({len(res)})", p.accent)
        y += 1
        avail = max(1, R - 4 - y - 3)
        used = self.draw_video_tiles(g, y, 1, sel, chunk, avail)
        y += used + 1
        if (show_prev or show_next) and y + 3 <= R - 3:
            bw = (C - 6) // 2
            short = bw < 22
            if show_prev:
                self.draw_scroll_btn(g, y, 2, bw, sel == prev_idx, "[7] BACK" if short else "[7] BACK UP", "^")
            if show_next:
                self.draw_scroll_btn(g, y, C - 2 - bw, bw, sel == next_idx, "[6] MORE" if short else "[6] MORE RESULTS", "v")

    async def page_results(self, q, res, page):
        await self.drop_video()
        self.invalidate()
        pages = max(1, (len(res) + 3) // 4)
        page = max(0, min(page, pages - 1))
        chunk = res[page * 4:page * 4 + 4]
        n = len(chunk)
        show_prev, show_next = page > 0, page < pages - 1

        keys = ["1"] + [str(i + 2) for i in range(n)]
        layout = [[0]] + ([list(range(1, n + 1))] if n else [])
        idx = n + 1
        nav_row = []
        prev_idx = next_idx = None
        if show_prev:
            prev_idx = idx
            keys.append("7")
            nav_row.append(prev_idx)
            idx += 1
        if show_next:
            next_idx = idx
            keys.append("6")
            nav_row.append(next_idx)
            idx += 1
        if nav_row:
            layout.append(nav_row)
        home_idx = idx
        keys.append("9")
        layout.append([home_idx])

        r = await self.menu(lambda g, s: self.draw_results(g, s, q, res, page, pages, show_prev, show_next,
                                                             prev_idx, next_idx, home_idx),
                            keys, layout, sel=1 if n else 0, extra=("ESC",))
        if r[0] == "opt":
            if r[1] == 0:
                return ("search", q)
            if r[1] <= n:
                return ("load", chunk[r[1] - 1], ("results", q, res, page))
            if show_next and r[1] == next_idx:
                return ("results", q, res, page + 1)
            if show_prev and r[1] == prev_idx:
                return ("results", q, res, page - 1)
            if r[1] == home_idx:
                return ("home",)
        elif r[0] == "key" and r[1] == "ESC":
            return ("home",)
        return ("home",)

    async def page_load(self, video, back):
        await self.drop_video()
        self.invalidate()
        cfg, loop = self.cfg, asyncio.get_running_loop()
        log.info("[%s] loading video %s (%s)", self.ip, video["id"], video["title"][:50])
        title = video["title"]
        fut = loop.run_in_executor(None, self.app.extract_info, video["id"])
        tick = 0
        try:
            while not fut.done():
                g = self.new_grid()
                self.chrome(g)
                self.draw_loading(g, title, ("Contacting YouTube...",), tick, pct=min(10, tick // 3))
                await self.present(g)
                tick += 1
                k = await self.get_key(0.1)
                if k == "9":
                    raise Nav(("home",))
                if k == "ESC":
                    raise Nav(back)
            info = fut.result()
        except BaseException as e:
            if isinstance(e, (Nav, ConnectionError, asyncio.CancelledError)):
                fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
                raise
            return ("error", f"Could not load video: {str(e)[:250]}", back)
        self.vinfo = info
        self.captions = None
        self.cap_fut = loop.run_in_executor(None, load_captions, info, str(cfg.video_caption_lang))
        self.cap_fut.add_done_callback(_swallow_future_error)
        await self.make_stream(0)
        vs = self.vs
        need = max(2, int(cfg.video_preload_seconds * self.video_fps()))
        while True:
            have = vs.prod - vs.cursor
            if vs.error and have == 0:
                return ("error", f"Could not stream video: {vs.error}", back)
            if have >= need or (vs.eof and have > 0):
                break
            g = self.new_grid()
            self.chrome(g)
            self.draw_loading(g, title, (f"Converting frames to text: {have}/{need}",), tick,
                              pct=10 + 90 * min(1.0, have / need))
            await self.present(g)
            tick += 1
            k = await self.get_key(0.08)
            if k == "9":
                raise Nav(("home",))
            if k == "ESC":
                raise Nav(back)
        return ("watch", back)

    async def make_stream(self, idx):
        if self.vs:
            await self.vs.stop()
        info = self.vinfo
        aspect = (info["width"] / info["height"]) if info.get("width") and info.get("height") else 16 / 9
        # a Commodore text cell is 8x8 pixels (square); the ANSI default assumes
        # cells twice as tall as wide and would squash the picture to half height
        w, h = fit_dims(self.cols, self.rows, aspect, 1.0 if self.term_mode == "petscii" else CHAR_ASPECT)
        self.vs = VideoStream(self.cfg, info, w, h, self.size_ver, self.video_fps())
        self._vkey = None
        await self.vs.start(idx)

    def draw_watch(self, g, sel, back):
        p, C, R = self.pal, self.cols, self.rows
        v, vs, fps = self.vinfo, self.vs, self.video_fps()
        self.chrome(g, sel == 1)
        self.draw_logo(g, 1, 2)
        y = self.LOGO_H + 1
        tl = wrap(v["title"], C - 6, 2)
        for i, l in enumerate(tl):
            g.text(y + i, 3, l, p.white)
        y += 2
        views_s = fmt_views(v.get("views"))
        dur_s = fmt_time(v["duration"]) if v.get("duration") else ""
        chan = v["channel"]
        if 3 + len(chan) + 3 + len(views_s) + 3 + len(dur_s) <= C - 2:
            g.text(y, 3, chan, p.cyan)
            g.text(y, 3 + len(chan) + 3, views_s, p.grey)
            g.text(y, 3 + len(chan) + 3 + len(views_s) + 3, dur_s, p.yellow)
        else:
            # too narrow for one line: channel on the first, views/length on the next
            g.text(y, 3, chan[:C - 6], p.cyan)
            g.text(y + 1, 3, views_s, p.grey)
            g.text(y + 1, 3 + len(views_s) + 3, dur_s, p.yellow)
        y += 2
        s = self.cfg.video_seek_seconds
        bw = min(C - 4, 66)
        narrow = bw - 14 < 24   # description column too tight for the full sentences below
        if narrow:
            rows = [("<-/->", f"Seek {s}s"), ("SPACE", "Pause/resume"), ("ESC", "Pause, go back"),
                    ("Q", "Show progress bar"), ("C", "Captions on/off"), ("9", "Home")]
        else:
            rows = [("<-  /  ->", f"Back / forward {s} seconds"), ("SPACE", "Pause / resume"),
                    ("ESC", "Pause and return to this screen"), ("Q", "Show time + progress bar for 5 seconds"),
                    ("C", "Captions on/off (recommended - there is no audio)"), ("9", "Home - cancels the video")]
        # rows still free above the footer: controls box + progress row + 3-row play button + a spare
        if R - 3 - y >= len(rows) + 2 + 1 + 3 + 1:
            g.fill(y, 2, len(rows) + 2, bw, bg=p.panel)
            g.box(y, 2, len(rows) + 2, bw, p.dim, p.panel)
            g.text(y, 4, " VIDEO CONTROLS ", p.accent, p.panel)
            for i, (k, d) in enumerate(rows):
                g.text(y + 1 + i, 4, f"{k:<10}", p.yellow, p.panel)
                g.text(y + 1 + i, 15, d[:bw - 14], p.white, p.panel)
            y += len(rows) + 2
        else:
            # short window: the full box would run into the footer, so show the keys as text
            hint = f"Keys: <-/-> seek {s}s, SPACE pause, ESC back, C captions, Q time, 9 home"
            for l in wrap(hint, C - 6, 2):
                g.text(y, 3, l, p.yellow)
                y += 1
            y += 1
        pos = (vs.shown_idx if vs.shown_data else vs.cursor) / fps
        dur = v.get("duration")
        g.text(y, 3, fmt_time(pos), p.yellow)
        w = max(10, min(C - 24, 50))
        frac = min(1.0, pos / dur) if dur else 0
        f = int(w * frac)
        g.text(y, 10, BAR_FULL * f, p.accent)
        g.text(y, 10 + f, BAR_EMPTY * (w - f), p.dim)
        g.text(y, 11 + w, fmt_time(dur) if dur else "--:--", p.yellow)
        y += 1
        if R - 3 - y >= 3:
            g.fill(y, 3, 3, 24, bg=p.white if sel == 0 else p.accent)
            g.text(y + 1, 5, "[1] PLAY VIDEO", p.black if sel == 0 else p.white, p.white if sel == 0 else p.accent)
        else:                       # very short window: one-row button instead of three
            g.fill(y, 3, 1, 24, bg=p.white if sel == 0 else p.accent)
            g.text(y, 5, "[1] PLAY VIDEO", p.black if sel == 0 else p.white, p.white if sel == 0 else p.accent)

    async def page_watch(self, back):
        if self.vs is None or self.vinfo is None:
            return ("home",)
        self.invalidate()
        r = await self.menu(lambda g, s: self.draw_watch(g, s, back), ["1", "9"], [[0], [1]], extra=("ESC",))
        if r[0] == "opt" and r[1] == 0:
            return ("play", back)
        if r[0] == "key":
            await self.drop_video()
            return back
        await self.drop_video()
        return ("home",)

    async def page_error(self, msg, back):
        await self.drop_video()
        self.invalidate()
        p = self.pal
        while True:
            g = self.new_grid()
            self.chrome(g)
            C = self.cols
            y = max(3, self.rows // 2 - 5)
            g.center(y, "  SOMETHING WENT WRONG  ", p.white, p.red)
            for i, l in enumerate(wrap(clean(msg), C - 10, 6)):
                g.center(y + 2 + i, l, p.grey)
            g.center(y + 9, "Press ENTER to go back, 9 for Home", p.yellow)
            await self.present(g)
            k = await self.get_key(0.5)
            if k == "9":
                return ("home",)
            if k is not None and k not in ("UP", "DOWN", "LEFT", "RIGHT"):
                return back

    # ======================================================================
    # video player
    # ======================================================================
    def motion_gate(self, arr, vs):
        """Hold a pixel at the RGB value it last actually showed as, unless the
        new frame moves it by more than `motion_threshold`. Raw video frames
        are noisy even over a static background (compression artifacts shift
        pixels by a few levels every frame), and that noise alone is normally
        enough to make almost every character cell compare as "different"
        each frame in present()'s delta diff - this gate removes that noise
        before quantization so only genuine motion survives into the diff.
        Real motion (big per-pixel jumps, scene cuts, seeks) always passes
        straight through, so there's no added lag on anything that matters."""
        thr = int(self.cfg.video_motion_threshold)
        key = (id(vs), arr.shape)
        if thr <= 0 or self._motion_key != key:
            self._motion_key, self._motion_prev = key, arr.copy()
            return arr
        held = self._motion_prev
        diff = np.abs(arr.astype(np.int16) - held.astype(np.int16)).max(axis=-1)
        out = np.where(diff[..., None] <= thr, held, arr)
        self._motion_prev = out.copy()
        return out

    def video_grid(self, data, vs):
        g = self.new_grid(video=True)
        arr = np.frombuffer(data, np.uint8).reshape(vs.h, vs.w, 3)
        if self.cfg.video_delta_compression:
            arr = self.motion_gate(arr, vs)
        g.blit_ascii((self.rows - vs.h) // 2, (self.cols - vs.w) // 2, arr)
        return g

    def base_grid(self):
        vs = self.vs
        key = (id(vs), vs.shown_idx if vs.shown_data else -1, self.cols, self.rows, self.mode.name)
        if key != self._vkey:
            self._vbase = self.video_grid(vs.shown_data, vs) if vs.shown_data else self.new_grid(video=True)
            self._vkey = key
        return self._vbase

    def msg_box(self, g, lines, fg, bg, row=None):
        w = max(len(l) for l in lines) + 4
        r = (self.rows - len(lines)) // 2 if row is None else row
        c = (self.cols - w) // 2
        g.fill(r - 1, c, len(lines) + 2, w, bg=bg)
        for i, l in enumerate(lines):
            g.text(r + i, c + 2, l, fg, bg)

    async def page_play(self, back):
        vs, cfg, p = self.vs, self.cfg, self.pal
        if vs is None:
            return ("home",)
        loop = asyncio.get_running_loop()
        fps = self.video_fps()
        seek_n = cfg.video_seek_seconds
        need = cfg.video_resume_buffer_frames
        if vs.size_ver != self.size_ver:
            await self.make_stream(vs.shown_idx if vs.shown_data else vs.cursor)
            vs = self.vs
        self.invalidate()
        self.clear_screen_raw()
        paused, ended, wall0 = True, False, 0.0
        waiting = "seek" if (vs.prod - vs.cursor < need and not vs.eof) else None
        bar_until, osd, toast = 0.0, None, None
        dur = self.vinfo.get("duration")
        log.info("[%s] playing %s", self.ip, self.vinfo["id"])

        def overlays(g, now):
            T = (vs.shown_idx if vs.shown_data else vs.cursor) / fps
            bar_on = now < bar_until
            if self.captions_on:
                text = self.caption_at(T)
                if text:
                    lines = wrap(text, min(self.cols - 6, 70), 3)
                    r0 = self.rows - (4 if bar_on else 3) - len(lines) + 1
                    for i, l in enumerate(lines):
                        g.center(r0 + i, f" {l} ", p.white, p.black)
            if bar_on:
                R, C = self.rows, self.cols
                g.fill(R - 1, 0, 1, C, bg=p.black)
                left, right = f" {fmt_time(T)} ", f" {fmt_time(dur) if dur else '--:--'} "
                w = max(4, C - len(left) - len(right) - 2)
                f = int(w * min(1.0, T / dur)) if dur else 0
                g.text(R - 1, 0, left, p.yellow, p.black)
                g.text(R - 1, len(left) + 1, BAR_FULL * f, p.accent, p.black)
                g.text(R - 1, len(left) + 1 + f, BAR_EMPTY * (w - f), p.dim, p.black)
                g.text(R - 1, C - len(right), right, p.yellow, p.black)
                note = " ESC to Pause/Exit "
                g.text(0, 0, note, p.white, p.panel)
            if toast and now < toast[1]:
                g.text(1, self.cols - len(toast[0]) - 2, f" {toast[0]} ", p.black, p.yellow)
            if waiting == "stall":
                g.fill(self.rows // 2 - 1, self.cols // 2 - 3, 3, 7, bg=p.black)
                g.put(self.rows // 2, self.cols // 2, SPINNER[int(now * 10) % len(SPINNER)], p.white, p.black)
            elif osd and now < osd[1]:
                if int(now * 6) % 2:
                    self.msg_box(g, [osd[0]], p.black, p.yellow, self.rows // 2)
                else:
                    self.msg_box(g, [osd[0]], p.yellow, p.black, self.rows // 2)
            if paused:
                if ended:
                    self.msg_box(g, ["END OF VIDEO", "SPACE = replay    ESC = back"], p.white, p.panel)
                elif not vs.started:
                    if int(now * 3) % 2:
                        self.msg_box(g, ["Press SPACE to start video"], p.black, p.yellow)
                    else:
                        self.msg_box(g, ["Press SPACE to start video"], p.yellow, p.black)
                elif waiting is None:
                    self.msg_box(g, ["|| VIDEO PAUSED", "SPACE = resume    ESC = back"], p.white, p.panel)

        async def do_seek(tgt, label, now):
            nonlocal waiting, osd, wall0, ended
            ended = False
            tgt = max(0, tgt)
            if vs.maxidx is not None:
                tgt = min(tgt, vs.maxidx)
            if vs.in_buffer(tgt):
                vs.seek_local(tgt)
                vs.trim()
                osd = (label, now + 0.6)
                if paused:
                    vs.advance()
                else:
                    wall0 = now - (vs.cursor - 1) / fps
            else:
                await vs.start(tgt)
                waiting = "seek"
                osd = (label, now + 3600)

        while True:
            now = loop.time()
            self.last_key = time.monotonic()
            if vs.size_ver != self.size_ver:
                # Terminal was resized: debounce before rebuilding. A resize
                # tears down and restarts the whole yt-dlp/ffmpeg pipeline,
                # which can take several seconds (client negotiation,
                # reconnect-on-403 retries, ...) - some telnet clients send a
                # burst of NAWS updates in quick succession while they settle
                # on a final window size (seen in practice as e.g. 83x30 ->
                # 156x41 -> 83x30 within a few seconds), and rebuilding on
                # every single one of those would restart the stream, show a
                # stale frame, restart it again, and so on - looking like the
                # picture is "drawing over itself" as old and new frames
                # interleave mid-transition. Waiting here for size_ver to stop
                # changing collapses a whole burst into one rebuild.
                ver = self.size_ver
                await asyncio.sleep(0.5)
                if self.size_ver != ver:
                    continue
                await self.make_stream(vs.shown_idx if vs.shown_data else vs.cursor)
                vs = self.vs
                waiting, self.prev = "seek", None
                continue
            # ---- keys
            while True:
                k = self.try_key()
                if k is None:
                    break
                kl = k.lower() if len(k) == 1 else k
                if k == "9":
                    return ("home",)
                if k == "ESC":
                    self.invalidate()
                    return ("watch", back)
                if k == "SPACE":
                    if waiting:
                        continue
                    if ended:
                        vs.started = True
                        paused = False
                        await do_seek(0, "[<<] REPLAY", now)
                        if not waiting:
                            wall0 = now - (vs.cursor - 1) / fps
                    else:
                        vs.started = True
                        paused = not paused
                        if not paused:
                            wall0 = now - (vs.cursor - 1) / fps
                elif k in ("LEFT", "RIGHT") and vs.started and not waiting:
                    cur = vs.shown_idx if vs.shown_data else vs.cursor
                    sign = -1 if k == "LEFT" else 1
                    await do_seek(cur + sign * seek_n * fps, f"[{'<-' if sign < 0 else '->'}] {seek_n}", now)
                elif kl == "q":
                    bar_until = now + 5
                elif kl == "c":
                    self.captions_on = not self.captions_on
                    c = self.caps()
                    toast = ("CC ON" if c else ("CC ON (loading...)" if c is None else "CC ON (no captions)"),
                             now + 2) if self.captions_on else ("CC OFF", now + 1.5)
            # ---- state machine
            if vs.shown_data is None and not waiting and vs.frame_at(vs.cursor) is not None:
                vs.advance()
            if waiting:
                avail = vs.prod - vs.cursor
                if vs.error and avail == 0:
                    return ("error", f"Stream failed: {vs.error}", back)
                if avail >= need or (vs.eof and avail > 0):
                    waiting = None
                    if osd:
                        osd = (osd[0], now + 0.5)
                    if paused:
                        vs.advance()
                    else:
                        wall0 = now - (vs.cursor - 1) / fps
                elif vs.eof:
                    waiting, paused, ended = None, True, True
            elif not paused:
                if vs.frame_at(vs.cursor) is None:
                    if vs.error and vs.prod == vs.cursor:
                        return ("error", f"Stream failed: {vs.error}", back)
                    if vs.eof:
                        paused, ended = True, True
                    else:
                        waiting = "stall"          # buffer ran dry: freeze frame + spinner
                else:
                    due = wall0 + vs.cursor / fps
                    if now < due:
                        await asyncio.sleep(min(due - now, 0.02))
                        continue
                    while vs.frame_at(vs.cursor + 1) is not None and now >= wall0 + (vs.cursor + 1) / fps:
                        vs.skip()                  # running late: drop frames, never drift
                    vs.advance()
            # ---- draw
            g = self.base_grid().copy()
            overlays(g, now)
            await self.present(g, delta=cfg.video_delta_compression)
            if paused or waiting:
                await asyncio.sleep(0.05)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
async def serve(cfg):
    app = App(cfg)
    server = await asyncio.start_server(app.handle, cfg.server_host, cfg.server_port)
    log.info("TelTube %s listening on %s:%s", VERSION, cfg.server_host, cfg.server_port)
    print(f"TelTube {VERSION} listening on telnet://{cfg.server_host}:{cfg.server_port}  (Ctrl+C to stop)")
    asyncio.ensure_future(app.warm())
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="TelTube - YouTube over Telnet")
    ap.add_argument("-c", "--config", default="teltube.ini", help="config file (created if missing)")
    args = ap.parse_args()
    cfg = Config(args.config)
    handlers = [logging.StreamHandler()]
    if cfg.logging_file:
        handlers.append(logging.FileHandler(str(cfg.logging_file), encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, str(cfg.logging_level).upper(), logging.INFO), handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not shutil.which(str(cfg.video_ffmpeg_path)):
        sys.exit(f"ffmpeg not found ({cfg.video_ffmpeg_path}). Install it or set [video] ffmpeg_path.")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        sys.exit("yt-dlp is not installed:  pip install -U yt-dlp")
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
