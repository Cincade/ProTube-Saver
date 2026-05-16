# Contributing to ProTube Saver

Thanks for poking around. This file is the architecture map + the list of WebView2 gotchas we've already paid for in real time, so you don't have to.

## Project layout

```
.
├── ProTubeSaver.spec         PyInstaller build spec
├── ProTube Launch.vbs        Dev launcher — runs `pythonw src\main.py`
├── fresh-demo.bat            Toggle between real `data/` and a clean demo state
├── requirements.txt
├── src/
│   ├── main.py               Entry: SMTC AppUserModelID, Chromium throttle flags,
│   │                         WebView2 background-color env, BrowserForm BackColor
│   │                         monkey-patch, window-polish callback
│   ├── logic.py              `API` class — video server, downloader, library,
│   │                         transcoder, frame extractor, thumbnail cache
│   ├── app_paths.py          Single source of truth for filesystem layout
│   ├── updater.py            yt-dlp self-updater
│   ├── groq_client.py        Optional AI features wrapper
│   └── index.html            Entire frontend, one file, no build step
├── assets/
│   ├── icon.ico              (committed)
│   ├── ffmpeg.exe            (gitignored — drop yours in before building)
│   └── ffprobe.exe           (gitignored — drop yours in before building)
├── docs/                     User guides + frontend section map
└── scripts/                  Dev helpers (startup profiling, etc.)
```

The `data/` folder is created at runtime next to the exe (or in the project root in dev mode). It holds the user's library, settings, downloads, thumbnails, transcoded cache, the auto-updated yt-dlp runtime, and logs. It's gitignored.

## Run / build

- **Dev (from project root):** `pythonw src/main.py` (or `python src/main.py` to see stdout).
- **Bundled exe:** `pyinstaller ProTubeSaver.spec` — output is `dist/ProTube Saver.exe`. Takes a few minutes; yt-dlp's ~1800 extractor modules get walked and embedded.
- **Diagnostics:** tail `data/protube.log`. `pythonw` discards stdout so persistent logs go there.
- **Demo mode:** `fresh-demo.bat` toggles `data/` ↔ `data.bak/` so you can record demos against a clean install state without losing your real data. Refuses to run while the exe is open.

## Architecture

### Process model

One Python process. Inside it:

1. `webview.create_window` opens a host WinForm with WebView2 inside.
2. `API` (logic.py) is exposed as `pywebview.api` to JS — every backend call from `index.html` is `await pywebview.api.<method>(...)`.
3. A localhost HTTP server (`_NoDelayServer`, started from `API.__init__`) serves video bytes to the `<video>` tag with Range support, 1MB chunks, TCP_NODELAY, 4MB SO_SNDBUF. Random port; filepath validated via base64-encoded handshake.
4. Background daemon threads: yt-dlp updater check, frame extraction worker (ffmpeg dumps for thumbs), transcode worker (legacy formats → H.264/AAC cache).

### Filesystem layout

`app_paths.py` is the single source of truth. `app_dir()` switches between `sys.executable`'s dir (frozen) and the parent of `src/` (dev).

```
<app-dir>/
    main.py / ProTubeSaver.exe
    data/
        settings.json         User library + preferences (~1MB+, library is inline)
        thumbnails/           Cached jpegs (offline-capable)
        transcoded/           H.264/AAC remuxes for non-natively-playable files
        yt-dlp-runtime/       Auto-updated yt_dlp package, prepended to sys.path
        downloads/            Default download target (configurable in UI)
        music/                Music mode downloads
        protube.log
```

`migrate_legacy()` runs once on first launch to copy from the pre-portable layout. Idempotent — drops a marker and no-ops afterwards.

### Critical startup ordering (`main.py`)

Order matters here and is not obvious:

1. **stdout/stderr UTF-8 reconfigure** — without this, prints with emoji/non-cp1252 chars from video titles crash threads with `UnicodeEncodeError` that masquerades as logic bugs.
2. **`SetCurrentProcessExplicitAppUserModelID('ProTubeSaver.App.1')`** — Windows SMTC source-name resolution. Without it, the volume tray says "Unknown app is playing audio".
3. **`from app_paths import ...` + `migrate_legacy()`** — must run before anything imports `yt_dlp`.
4. **`YtDlpUpdater.bootstrap_sys_path(ytdlp_runtime_dir())`** — prepends the runtime folder to `sys.path` so any auto-downloaded yt-dlp wins over the bundled one. **MUST run before `import webview` / `from logic import API`** since `logic.py` imports `yt_dlp` at module load.
5. **Chromium throttle-disable flags** via `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` — disables background timer throttling, IntensiveWakeUpThrottling V1+V2, TabFreeze, PageFreeze, FreezePolicy, HighEfficiencyModeAvailable, BackForwardCache. Without these the UI freezes when the window loses focus or after a few hours idle.
6. **`WEBVIEW2_DEFAULT_BACKGROUND_COLOR=0xFF000000`** — must be set before WebView2 initializes. Format is `0xAARRGGBB`. See "WebView2 landmines" below.
7. **`BrowserForm.BackColor` monkey-patch** — forces the host WinForm's BackColor black because pywebview's `background_color=` arg is silently ignored on the EdgeChromium backend.
8. **`_on_window_ready` → `api.apply_window_polish()`** — polls until `webview.windows[0]` exists, then applies Win11 square corners and a frame redraw. Re-fires after every fullscreen toggle.

### Backend: `logic.py` (~6000 lines, single `API` class)

Method clusters — names are self-explanatory once you know the cluster exists:

- **Window polish / fullscreen:** `apply_window_polish`, `set_fullscreen`, `toggle_fullscreen`
- **Download:** `fetch_url_info`, `start_download`, `restart_download`, `pause_download`, `resume_download`, `cancel_download`, `get_active_progress`
- **Streaming:** `_start_video_server`, `get_video_stream_url`
- **Transcode:** `_transcode_worker`, `_streamable_cache_path`, `_is_web_compatible`
- **Frame extraction:** `_start_frame_extraction_worker`, `_extract_video_frame`, `force_video_frame_thumbnail`
- **Library:** `load_library`, `save_library`, `add_to_library`, `remove_from_library`, `delete_video_from_library_and_disk`, `add_playlist_to_library`, `repair_library`
- **Import:** `import_from_folder`, `scan_folder_preview`, `scan_folder_full`, `_build_video_entry_from_file`, `get_import_progress`
- **Refetch:** `refetch_all`, `refetch_single`, `force_refetch_now`, `fix_metadata_from_url`
- **Settings/state:** `get_setting`, `set_setting`, `save_queue`, `load_queue`, `save_playback_position`, `mark_watched_to_end`
- **Search:** `search_youtube`, `search_youtube_suggestions` (Innertube), `search_music`
- **Music:** `download_music_track`, `get_music_library`
- **AI:** methods backed by `groq_client.py` — summary, chat, subtitle polish
- **OS integration:** `open_folder`, `open_file`, `reveal_in_folder`, `open_external_url`, `choose_folder`

`settings.json` carries the entire library inline (~1MB+ for active users). It's loaded once into `self.settings` and re-saved via `_save_settings()` after each mutation.

### Frontend: `index.html` (~24,000 lines, ~1.4MB)

One file. Bundled DM Sans + JetBrains Mono base64 woff2 inside `<style>` so it works offline. Major sections (search section comments to find them): splash, scrollbars, cockpit shell (rail + main grid), library view, queue view, **player view** (with overlay topbar, hover-expanding seek + volume sliders, side panels), Music view, music mini-player dock, drag-drop overlay, offline pill, confirm modal, toast container, import-progress card.

All backend calls go through `pywebview.api.<method>(...)` and return whatever `logic.py` returns. There's no separate API client layer — JS calls Python directly via the bridge.

See [docs/index-html.md](docs/index-html.md) for a more granular section map.

## WebView2 landmines (do NOT redo)

These are bugs we've already paid for. Each one cost real time. Read before touching the player view, fullscreen, or any window-state code.

- **Never put `transform: translateZ(0)` on `#player-video` or `.player-canvas`.** Forces a GPU compositor layer that interacts with WebView2's video pipeline and **blanks the video** during state changes. WebView2Feedback issue #2256. Was added once to fix sub-pixel white edges; caused worse bugs.
- **Never `display: none` the `.rail` during fullscreen.** Removing an element from layout fires a heavier reflow that races with the OS borderless toggle and **blanks the WebView2 surface**. Current solution: `body.player-is-fullscreen .cockpit { grid-template-columns: 0 minmax(0, 1fr); transition: none; }` — the rail's column collapses to zero width, but the rail stays in flow. `overflow: hidden` clips its contents.
- **Never animate `grid-template-columns` while the OS window is also resizing.** Two simultaneous layout changes blank the WebView2 surface. The `transition: none` on `.cockpit` inside `.player-is-fullscreen` is deliberate — leave it.
- **Don't combine element-level `requestFullscreen()` with OS-level `set_fullscreen()`.** Two state machines drift after a few toggles. We use ONLY OS borderless via `pywebview.set_fullscreen(bool)` (idempotent, see `_window_is_fullscreen` flag in logic.py). The canvas is `position: absolute; inset: 0` so it naturally fills the parent when the title bar disappears.
- **`pywebview.create_window(background_color=...)` is silently ignored on WebView2.** Documented in pywebview's own changelog. Set `WEBVIEW2_DEFAULT_BACKGROUND_COLOR=0xFF000000` env var BEFORE `import webview` AND monkey-patch `BrowserForm.__init__` to force `BackColor = Color.FromArgb(255,0,0,0)`. Both are in main.py — leave both.
- **Win11 DWM rounded corners drift after fullscreen toggles** → white slivers at corners. Fix is `DwmSetWindowAttribute(hwnd, 33, DWMWCP_DONOTROUND)` in `api.apply_window_polish()`, called from `_on_window_ready` AND from `set_fullscreen` after every transition. **Don't** call dwmapi from `API.__init__` — the window doesn't exist yet, ctypes fails silently or weirdly. Always go through the polled `_on_window_ready` callback.
- **WebView2 paints its `DefaultBackgroundColor` (white by default) in the gap between native control resize and HTML repaint.** That's the root cause of all the "white flashes during fullscreen toggle" reports. Fix: env var above.

## Design conventions

These are conventions baked into the app. Don't relitigate without a reason:

- Player progress bar uses `#3b82f6` blue, NOT red.
- Volume slider has its own thumb that follows the fill, expands on hover (matches seek).
- URL input autofocus on tab switch: queue YES (paste-ready), library NO (don't steal search focus).
- Long-press to enter selection mode was removed (brittle on desktop). Right-click and per-card checkbox are the entry points.
- Offline indicator is a fixed-position pill, NOT a layout-pushing banner.
- For features with multiple entry-creation paths (`add_to_library`, `add_playlist_to_library`, `_build_video_entry_from_file`), stamp the same field set everywhere.
- Music play buttons: Spotify-spec `#1ED760` green with black icon, no scale-on-hover.

## Debugging tips

- **UI / rendering / window state issues:** search WebView2Feedback issues first. Many problems are tracked-but-unfixed Microsoft bugs needing app-side workarounds, not app logic bugs.
- **"App won't launch" after a Python edit:** check that nothing risky is in `API.__init__` synchronously (`ctypes` calls, anything touching `webview.windows[0]`). Daemon threads are fine; synchronous Win32 API calls are not.
- **Video pipeline blanks/glitches:** look for layout changes or CSS transforms on/around the video element happening during state transitions. See landmines above.
- **Imports break across re-imports:** check `_build_video_entry_from_file`'s archive-restore path in logic.py. The archive at `settings['library_archive']` preserves entries through "remove from library" so a re-import restores the original metadata.

## PRs

Standard flow: fork → branch → PR against `main`. Keep PRs focused (one bug or one feature). If your change touches the player or window state, please mention what you tested — the WebView2 surface has a lot of state-machine corners.
