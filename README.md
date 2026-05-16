# ProTube Saver

A single-window desktop YouTube downloader and player for Windows 11.

Paste a video, playlist, or channel URL — it downloads in the background, plays in-app with a custom player, and survives offline once cached. Search YouTube without opening a browser. Listen to music with a dedicated Music mode and a persistent mini-player. Optional AI summaries + chat for any video.

Built as a [pywebview](https://pywebview.flowrl.com/) app with the WebView2 (EdgeChromium) renderer. Python backend handles downloads, transcoding, and a localhost video server with HTTP Range support; the entire UI is one HTML/CSS/JS file with no build step.

## Install

**The easy way:** grab the latest `.exe` from [Releases](https://github.com/Cincade/ProTube-Saver/releases/latest). Single file, ~210 MB (yt-dlp + ffmpeg + ffprobe are bundled). Run it. The first launch creates a `data/` folder next to the exe — everything the app owns (library, settings, downloads, thumbnails) lives there. Move the folder, the install moves with it.

Windows 11 only. WebView2 ships with Windows 11 so there's nothing to install separately.

## What it does

- **Downloads via yt-dlp** — full and partial playlist downloads, channel videos tab, per-video quality selection up to 4K, pause / resume / cancel, error retry with categorized failures.
- **In-app playback** — local HTTP server streams files with proper `Range` support so the seek bar works. Background transcode worker remuxes incompatible containers (MKV, HEVC, etc.) to H.264/AAC on demand.
- **Library management** — import existing folders from disk, NEW-badge for recently added items, fix-metadata flow, archive-restore on re-import, library-wide search, multi-select bulk delete with progress card.
- **In-app YouTube search** — Search tab in the rail. Type a query → videos, channels, or playlists right inside ProTube (hits YouTube's Innertube API directly, sub-second). Suggestions as you type. Infinite scroll.
- **Music mode** — separate Music tab with a Spotify-style library grid + YouTube Music search. Tracks download as M4A with ID3 tags + cover art. Mini-player dock stays visible across every view once music starts playing.
- **Channels and playlists track updates** — manual "Check for new videos" button on any playlist or channel; diffs against your local entries, lets you pick which new videos to queue.
- **Survives offline** — thumbnails cached locally. Library and queue work fully offline; only fetching new content needs network.
- **Self-updating yt-dlp** — auto-pulls fresh yt-dlp wheels from PyPI in a background thread once a day. Updates persist in `data/yt-dlp-runtime/`, so the bundled exe stays current without rebuilding.
- **Optional AI features** — bring a free [Groq](https://console.groq.com/) API key; get per-video summaries, transcript-grounded chat, and auto-polished subtitles. No key configured → AI features stay disabled; the rest of the app works normally.
- **Polished player** — YouTube-style top overlay on hover, custom seek with hover-expansion, separate volume thumb, 300% volume boost via Web Audio, fullscreen via OS-level borderless window, square Win11 corners.

See [CHANGELOG.md](CHANGELOG.md) for the full release history.

## Build from source

You only need to build if you're hacking on the code. Most people just want the [release `.exe`](https://github.com/Cincade/ProTube-Saver/releases/latest).

**Prereqs:**

- Python 3.11+
- `pip install -r requirements.txt`
- Drop `ffmpeg.exe` and `ffprobe.exe` into `assets/` — grab them from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) (the "release essentials" build is enough). They're gitignored because they exceed GitHub's 100 MB per-file limit. `assets/icon.ico` is already in the repo.

**Run in dev mode:**

```
pythonw src\main.py
```

Or double-click `ProTube Launch.vbs` — same thing, but sets the cwd correctly regardless of where you launch it from.

**Build the exe:**

```
pyinstaller ProTubeSaver.spec
```

Output lands in `dist\ProTube Saver.exe`. Takes a few minutes — yt-dlp's ~1800 extractor modules are walked and embedded.

## Stack

- Python 3.11+ — `pywebview`, `yt_dlp`, `requests`, `packaging`
- WebView2 (EdgeChromium) — bundled with Windows 11
- ffmpeg + ffprobe — bundled at build time and used at runtime for transcoding + frame extraction
- PyInstaller — single-file `.exe` builds via `ProTubeSaver.spec`
- Llama 3.3 70B on Groq — optional, for AI summary / chat / subtitle polish

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture notes, WebView2 gotchas we've already paid for, and the dev workflow. Bug reports and PRs welcome via [Issues](https://github.com/Cincade/ProTube-Saver/issues).

## License

MIT — see [LICENSE](LICENSE).
