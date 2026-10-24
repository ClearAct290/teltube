<p><img width="429" height="160" alt="xl-logo-big-vfl2" src="https://github.com/user-attachments/assets/e6bd2367-5bcf-4b95-ba4a-f2658040bdb4" /></p>

# TelTube - YouTube over Telnet

**Official IP:** Not Yet... (:

A telnet proxy for youtube designed to work on ASCII and PETSCII supported terminals.
Browse, search and watch YouTube from a telnet client. TelTube is a telnet
server that serves a YouTube-style text interface (home, search, results and
watch pages) and a real-time 144p video player that turns video into coloured
ASCII characters, with captions. Every connection gets its own independent
session.

It works with modern terminals **and** with old hardware: the first screen asks
whether you are a **Generic Telnet** client or a **Commodore BBS** client, and
Commodore mode switches the whole session to PETSCII.

- No audio (it is a text terminal), so turn captions on with `C`.
- Plain ASCII output only - no Unicode symbols, so it survives old terminals.
- Adapts to your window size (NAWS) on generic terminals.

---
<p>Standard ASCII examples</p>
<p><img width="555" height="317" alt="image" src="https://github.com/user-attachments/assets/67c85982-4d88-4ad1-ad79-40f6339bd699" /></p>
<p><img width="555" height="317" alt="image" src="https://github.com/user-attachments/assets/534510f6-f907-4eb4-9627-538e2f04e2e7" /></p>
<br>
<p></p>Commodore 64 PETSCII Examples</p>
<p><img width="581" height="505" alt="image" src="https://github.com/user-attachments/assets/7795c02d-52de-4ccd-8033-2da1bdbc6669" /></p>
<p><img width="581" height="505" alt="image" src="https://github.com/user-attachments/assets/072d2723-5699-4aaa-983c-a3bb7d7b208d" /></p>
<p><img width="581" height="505" alt="image" src="https://github.com/user-attachments/assets/14ad91b9-a664-4f6e-9bb4-288c90a2c952" /></p>






## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Running](#running)
- [Connecting](#connecting)
- [Generic Telnet vs Commodore BBS](#generic-telnet-vs-commodore-bbs)
- [Controls](#controls)
- [Configuration](#configuration)
- [Hosting it publicly](#hosting-it-publicly)
- [Troubleshooting](#troubleshooting)
- [Disclaimer](#disclaimer)
- [License](#license)

---







## Requirements

| Dependency | Why | Install |
|------------|-----|---------|
| Python 3 (3.9+ recommended) | runs the server | https://www.python.org/downloads/ |
| `yt-dlp` | talks to YouTube (search, metadata, stream) | `pip install -U yt-dlp` |
| `numpy` | frame processing | `pip install numpy` |
| `pillow` | thumbnails | `pip install pillow` |
| `ffmpeg` (on your PATH) | turns the video stream into raw frames | see below |

Installing ffmpeg:

- **Windows:** `winget install ffmpeg` (or download a build and add its `bin`
  folder to PATH)
- **macOS:** `brew install ffmpeg`
- **Debian / Ubuntu:** `sudo apt install ffmpeg`
- **Anything else:** make sure `ffmpeg` runs from a terminal, or set
  `[video] ffmpeg_path` in the config to its full path.

TelTube checks for ffmpeg and yt-dlp at startup and exits with a clear message
if either is missing.

---

## Installation

```bash
git clone https://github.com/<your-username>/TelTube.git
cd TelTube
pip install -U yt-dlp numpy pillow
```

A virtual environment is a good idea:

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -U yt-dlp numpy pillow
```

**Keep yt-dlp updated.** YouTube changes often and an old yt-dlp is the most
common reason videos stop loading:

```bash
pip install -U yt-dlp
```

---

## Running

```bash
python TelTube.py
```

On first run TelTube writes a default `teltube.ini` next to where you ran it.
To use a different config file:

```bash
python TelTube.py -c my-config.ini
```

**Port:** the default port is **23**, the standard telnet port. On Linux and
macOS, ports below 1024 need root. Easiest fix: edit `teltube.ini` and set

```ini
[server]
port = 2323
```

then connect to port 2323. Restart TelTube after editing the config.

Stop the server with `Ctrl+C`.

---

## Connecting

From the same machine (port 2323 as above):

```bash
telnet localhost 2323
```

- **Linux / macOS:** `telnet` (install it if missing, e.g. `sudo apt install telnet`).
- **Windows:** enable "Telnet Client" in *Windows Features*, or use PuTTY
  (connection type **Telnet**, or **Raw** if telnet negotiation misbehaves).
- **Any BBS-style terminal program** that speaks telnet will work too.

For the best picture use a terminal with 24-bit colour and a window of roughly
80x24 or larger. Smaller and larger windows are supported; very short windows
get a compact list layout instead of thumbnail tiles.

---

## Generic Telnet vs Commodore BBS

The very first screen asks:

```
1 = GENERIC TELNET   (ANSI colour, any size)
2 = COMMODORE BBS    (PETSCII, 40 columns x 25)
```

This screen is plain uppercase text with no escape codes, so a Commodore
displays it correctly.

| | Generic Telnet | Commodore BBS |
|---|---|---|
| Output | ANSI escape codes | PETSCII control codes |
| Screen size | follows your window (NAWS) | fixed 40x25 (configurable) |
| Colour | truecolor / 256 / 16 (`T` cycles) | fixed 16-colour C64 palette |
| Charset | UTF-8 or CP437 (`E` cycles) | PETSCII (uppercase / unshifted) |
| Video list | thumbnail tiles | one video per row, text only |
| Video frame rate | `[video] fps` (10) | `[server] commodore_video_fps` (3) |

Commodore notes:

- Real Commodore hardware has no cursor-addressing escape codes, so every
  frame is a full-screen repaint. That is why the Commodore frame rate is low
  by default - raise it only if your link can keep up.
- A C64 has one screen background colour that cannot be set over the wire.
  TelTube assumes **black**, so set your terminal program's background to black.
- RUN/STOP is expected to arrive as `ESC` (the usual convention for telnet
  terminal programs), and it acts as "back".
- Commodore mode is **experimental**: it has been tested against a PETSCII
  byte-stream validator, not yet on real hardware. Reports and fixes from real
  C64 / C128 users are very welcome.
- Screens narrower than 40 columns (e.g. VIC-20) are not supported.

---

## Controls

**Menus**

| Key | Action |
|-----|--------|
| `1`-`5` | Select (1 = search box, 2-5 = videos) |
| Arrow keys / Commodore cursor keys | Move |
| `ENTER` / `RETURN` | Go |
| `9` | Home |
| `7` / `6` | Back up / more results (results page) |
| `T` | Cycle colour mode (generic only) |
| `E` | Cycle charset UTF-8 / CP437 (generic only) |
| `ESC` | Back |

**Watching a video**

| Key | Action |
|-----|--------|
| Left / Right | Seek back / forward (5 seconds by default) |
| `SPACE` | Pause / resume |
| `ESC` | Pause and return to the watch page |
| `Q` | Show time and progress bar for 5 seconds |
| `C` | Captions on / off (recommended - there is no audio) |
| `9` | Home (cancels the video) |

---

## Configuration

Everything lives in `teltube.ini`. Missing keys fall back to built-in defaults,
so you only need to list what you change. Restart after editing.

| Section | What it controls |
|---------|------------------|
| `[server]` | host, port, server name, connection limits, idle timeout, IP allow / deny lists, default window size, Commodore screen size and frame rate |
| `[display]` | output charset, colour mode, truecolor precision, accent colour |
| `[video]` | frame rate, buffering, seek time, delta compression, ffmpeg path, captions defaults |
| `[search]` | home page term, result counts, cache settings |
| `[ytdlp]` | player client fallbacks, cookies file, proxy, extra yt-dlp flags |
| `[logging]` | log level and optional log file |

Useful settings:

```ini
[server]
port = 2323
max_clients = 20
max_clients_per_ip = 3
idle_timeout = 900          ; seconds without a key press, 0 = never
commodore_cols = 40
commodore_rows = 25
commodore_video_fps = 3

[video]
fps = 10
captions_default = true

[logging]
file = teltube.log
```

If a slow link struggles, lower `[video] fps`, lower `[display] truecolor_bits`,
or keep `[video] delta_compression = true` (only changed cells are re-sent).

---

## Hosting it publicly

Telnet is **unencrypted** and TelTube spawns video processes per viewer, so
think before exposing it:

- Set `max_clients` and `max_clients_per_ip` to sensible limits.
- Use `allow_ips` / `deny_ips` (comma-separated IPs or CIDR ranges) to restrict
  who can connect.
- Forward an external port to your TelTube port on your router, or run it on a
  VPS. To expose it on the standard telnet port without running as root, forward
  23 to your high port (for example 23 -> 2323).
- Run it in `screen`, `tmux` or a service manager so it survives logouts.
- Each video stream uses CPU and bandwidth (yt-dlp + ffmpeg). Size your limits
  to your machine.

---

## Troubleshooting

**"ffmpeg not found" / "yt-dlp is not installed"**
Install them (see [Requirements](#requirements)) or set `[video] ffmpeg_path`.

**Videos fail with `HTTP Error 403: Forbidden` or "Requested format is not available"**
This comes from YouTube, not from the display code. TelTube already retries
with other YouTube clients automatically. Things to try, in order:

1. `pip install -U yt-dlp` and restart TelTube.
2. Set a client order in the config, e.g. `[ytdlp] player_client = tv,web_safari,android`.
3. Provide a logged-in `cookies_file` or a `proxy` under `[ytdlp]`.
4. Try `[video] stream_mode = direct`.

**Garbled characters or broken boxes**
Press `E` to switch UTF-8 / CP437, or set `[display] encoding`. Press `T` to
drop to 256 or 16 colours on terminals without truecolor.

**Picture is the wrong size or the layout looks off**
TelTube follows the window size your client reports. Resize the window and it
redraws. If your client does not report a size, set `default_cols` and
`default_rows` under `[server]`.

**"Permission denied" starting the server**
You are binding a port below 1024 without root. Use a port such as 2323.

---

## Disclaimer

TelTube is an independent, unofficial project and is not affiliated with or
endorsed by YouTube or Google. It uses yt-dlp to access YouTube - make sure
your use complies with YouTube's Terms of Service and the laws where you live.
You are responsible for what you run and who you let connect.

---

## License

Copyright (C) 2026 ClearAct290-Reddit

TelTube is free software, released under the **GNU General Public License,
version 2** (or, at your option, any later version). See the [LICENSE](LICENSE)
file for the full text.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.
