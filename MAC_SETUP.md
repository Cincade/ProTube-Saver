# MAC_SETUP.md

How to get ProTube Saver running on macOS for development, and what code needs
to change so the same `src/` runs on both platforms.

> **Status:** the codebase is mostly cross-platform already (`logic.py` guards
> all its Windows-only calls with `sys.platform == 'win32'`), but `main.py`
> needs to be split so the WebView2 / BrowserForm / DWM bits don't blow up on
> macOS. The "Platform-guard edits" section below is the checklist for that.

---

## 1. One-time machine setup (on the Mac)

```bash
# Python (3.11 or 3.12 recommended; 3.13 should work but Pillow/yt-dlp wheels lag)
brew install python@3.12

# Git, if not already installed
brew install git

# ffmpeg/ffprobe for runtime + bundling
brew install ffmpeg
```

Verify:

```bash
python3 --version       # 3.12.x
ffmpeg -version         # any 6.x or 7.x
which ffmpeg ffprobe    # note these paths; we copy them into assets/mac/ later
```

---

## 2. Clone the repo

You already have the private repo at `Cincade/ProTube-Saver-` (the trailing dash
is intentional — see `MEMORY.md`).

```bash
mkdir -p ~/Code
cd ~/Code
git clone git@github.com:Cincade/ProTube-Saver-.git "ProTube Saver"
cd "ProTube Saver"
git checkout -b mac-port
```

If SSH isn't set up on the Mac, use HTTPS + a GitHub PAT instead:

```bash
git clone https://github.com/Cincade/ProTube-Saver-.git "ProTube Saver"
```

---

## 3. Python environment

```bash
cd "~/Code/ProTube Saver"
python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
pip install pyobjc        # required by pywebview's macOS (Cocoa/WKWebView) backend
```

> `pyobjc` is the macOS-only dep — pywebview imports it lazily so it isn't in
> `requirements.txt` (which is Windows-targeted). Don't add it to
> `requirements.txt`; create a `requirements-mac.txt` instead (see step 5).

---

## 4. Drop Mac ffmpeg/ffprobe binaries into the repo

PyInstaller bundles whatever is in `assets/` into the final build. We keep
the Windows binaries where they are and add a Mac sibling folder.

```bash
mkdir -p assets/mac
cp "$(which ffmpeg)"  assets/mac/ffmpeg
cp "$(which ffprobe)" assets/mac/ffprobe
chmod +x assets/mac/ffmpeg assets/mac/ffprobe
```

Prefer the static universal2 builds from <https://evermeet.cx/ffmpeg/> if you
want a build that works on both Intel and Apple Silicon without depending on
Homebrew's dylibs (the Homebrew copy links against `/opt/homebrew/lib/*.dylib`
and won't run on a machine without those installed).

---

## 5. Add `requirements-mac.txt`

Same deps as Windows + `pyobjc`. Keeps `requirements.txt` clean for Windows
users.

```
-r requirements.txt
pyobjc>=10.0
```

---

## 6. Try to run (will likely crash — that's expected)

```bash
python src/main.py
```

Expected failures on first run:

- `ModuleNotFoundError: No module named 'webview.platforms.winforms'` — the
  BrowserForm monkey-patch block tries to import a Windows-only module.
- `AttributeError: module 'os' has no attribute 'startfile'` — won't hit this
  unless you exercise certain UI paths (already guarded in `logic.py`).
- ffmpeg path errors — the `_ffmpeg_path()` / `_ffprobe_path()` lookups in
  `logic.py` need to look in `assets/mac/` on Darwin.

These are exactly what the "Platform-guard edits" below fix.

---

## 7. Platform-guard edits (the actual port work)

Listed in priority order. Each is small and self-contained.

### 7.1 `src/main.py` — wrap all Windows-only blocks

Already done:
- `SetCurrentProcessExplicitAppUserModelID` (line 26) — already
  `if sys.platform == 'win32'`. ✅
- Single-instance mutex (line 41) — already
  `if sys.platform == 'win32'`. ✅

Needs guarding (currently runs unconditionally on import / startup):

- **`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` env var (line 129)** — harmless on
  Mac (Cocoa ignores it) but cleaner to guard:
  ```python
  if sys.platform == 'win32':
      os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = ' '.join(_chromium_flags)
  ```

- **`WEBVIEW2_DEFAULT_BACKGROUND_COLOR` env var (line 137)** — same, harmless
  but guard:
  ```python
  if sys.platform == 'win32':
      os.environ['WEBVIEW2_DEFAULT_BACKGROUND_COLOR'] = '0xFF000000'
  ```

- **BrowserForm monkey-patch (lines 146-160)** — THIS is the import that
  crashes on Mac. Wrap the entire `try:` block:
  ```python
  if sys.platform == 'win32':
      try:
          from webview.platforms import winforms as _wf_module
          # ... existing body unchanged ...
      except Exception as _e:
          print(f'[ProTube] BrowserForm BackColor monkey-patch skipped: {_e}')
  ```

- **`background_color='#000000'` arg in `webview.create_window` (line 230)** —
  leave it. On Mac pywebview's Cocoa backend honors it (unlike WebView2), so
  it actually does something useful here.

### 7.2 `src/logic.py` — `apply_window_polish` already no-ops on non-win32

Line 7055: `if sys.platform != 'win32': return` is already there. ✅

The Mac equivalent (rounded-corner / DWM polish) is **unnecessary** —
WKWebView doesn't have the white-flash issue WebView2 has. Leave the early
return as-is.

### 7.3 `src/logic.py` — ffmpeg/ffprobe path resolution

Find the existing `_ffmpeg_path()` and `_ffprobe_path()` helpers (around
lines 6565 and 6580). They look for `ffmpeg.exe` / `ffprobe.exe` in `assets/`.
Generalize:

```python
def _ffmpeg_path(self):
    ext = '.exe' if sys.platform == 'win32' else ''
    subdir = 'mac' if sys.platform == 'darwin' else ''  # Win files stay at assets/ root
    candidates = [
        resource_path(os.path.join('assets', subdir, f'ffmpeg{ext}')),
        resource_path(os.path.join('assets', f'ffmpeg{ext}')),  # fallback (Win layout)
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return f'ffmpeg{ext}'  # last resort: hope it's on PATH
```

Same shape for `_ffprobe_path()`. Read the actual current bodies first — the
helper may already do a path-search; just add the `assets/mac/` candidate
ahead of the existing one.

### 7.4 `src/app_paths.py` — Mac data directory

**Design decision:** match Mac convention (`~/Library/Application Support/ProTube Saver/`)
or stay portable (next to the `.app`)?

Recommended: Mac convention. Reason: `.app` bundles are signed and shipping
writable data inside them breaks code-signing later. Apple's HIG also expects
user data under `~/Library/Application Support/`.

Edit `app_dir()`:

```python
def app_dir():
    if sys.platform == 'darwin' and getattr(sys, 'frozen', False):
        # Frozen .app: store data outside the bundle so signing/notarization
        # isn't invalidated by runtime writes.
        return os.path.join(
            os.path.expanduser('~/Library/Application Support'),
            'ProTube Saver',
        )
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # Dev mode (Windows AND Mac) — anchor on this file's location
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```

Dev mode on Mac still uses the project-root `data/` folder so iteration is
fast (no `~/Library` indirection during development).

### 7.5 Build spec — `ProTubeSaver-mac.spec`

New file alongside `ProTubeSaver.spec`. Differences from the Windows spec:
- `Analysis(... datas=[('assets/mac/ffmpeg', 'assets/mac'), ('assets/mac/ffprobe', 'assets/mac'), ...])` instead of the `.exe` paths
- `BUNDLE(...)` block at the bottom that wraps the EXE into a `.app`:
  ```python
  app = BUNDLE(
      exe,
      name='ProTube Saver.app',
      icon='assets/icon.icns',     # need to convert icon.ico → icon.icns
      bundle_identifier='com.cincade.protubesaver',
      info_plist={
          'CFBundleShortVersionString': '1.1.0',
          'NSHighResolutionCapable': True,
          'LSMinimumSystemVersion': '11.0',
      },
  )
  ```
- Convert the icon: `sips -s format icns assets/icon.ico --out assets/icon.icns`
  (sips is built into macOS).

### 7.6 (Optional) WKWebView fullscreen sanity check

Once it boots, hit the fullscreen button. pywebview's macOS backend uses
`NSWindow toggleFullScreen:`, which on Mac means the green-button native
fullscreen (separate Space). If that feels wrong (most desktop apps prefer
borderless-fullscreen), there's a Cocoa workaround but it's involved —
defer until you've tried it.

---

## 8. Test loop

While iterating:

```bash
source .venv/bin/activate
python src/main.py
```

stdout is visible (unlike `pythonw` on Windows), so prints from background
threads show up immediately. The `data/protube.log` tail still works.

To force a fresh-state run:

```bash
mv data data.bak && python src/main.py
# ... test ...
rm -rf data && mv data.bak data
```

(Mac analogue of `fresh-demo.bat`. We can wrap this in a `fresh-demo.sh` later.)

---

## 9. Build the `.app`

Only do this after the dev run works end-to-end.

```bash
pip install pyinstaller
pyinstaller ProTubeSaver-mac.spec
open "dist/ProTube Saver.app"
```

To distribute outside your own machine you'll need to **codesign + notarize**
(otherwise Gatekeeper blocks it with "ProTube Saver can't be opened because
Apple cannot check it for malicious software"). That's a separate workstream
requiring an Apple Developer ID ($99/yr) — not blocking for personal use.

For a personal/internal build, users can right-click → Open the first time to
bypass Gatekeeper.

---

## 10. Merge back to `main`

Once Mac runs cleanly:

```bash
git checkout main
git merge mac-port
git push
```

The `sys.platform` guards mean Windows keeps working from the same codebase —
no fork, no separate branches long-term.

---

## 11. Known gaps (everything else that will come up)

The §7 edits get it **booting**. These are the things you'll hit once it boots
and start using it like a real Mac app. Ordered by how soon you'll trip on
them.

### 11.1 Single-instance guard on Mac (high priority)

`main.py` lines 41–66 use a Windows named mutex so two ProTubes can't race on
`settings.json` writes. We've already paid for one torn-write corruption (see
`_try_recover_truncated_json` in `logic.py`); we don't want it on Mac too.

Mac equivalent: a `fcntl.flock` advisory lock on a file in `data_dir()`.
Add inside the existing `if sys.platform == 'win32': ... else:` branching:

```python
elif sys.platform == 'darwin':
    try:
        import fcntl
        _lock_path = os.path.join(data_dir(), '.singleinstance.lock')
        _SINGLE_INSTANCE_LOCK_FH = open(_lock_path, 'w')   # keep ref alive
        try:
            fcntl.flock(_SINGLE_INSTANCE_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another ProTube already holds the lock — surface a native dialog and exit
            try:
                subprocess.run([
                    'osascript', '-e',
                    'display dialog "ProTube Saver is already running.\n\nLook in the Dock — only one instance can run at a time so your library and queue stay safe." with title "ProTube Saver" buttons {"OK"} default button "OK" with icon note'
                ], check=False)
            except Exception:
                pass
            sys.exit(0)
    except Exception as _e:
        print(f'[ProTube] single-instance lock setup failed: {_e}')
```

The lock is released automatically when the process exits (clean or crashed),
so there's no stale-lock window — same property as the Win32 mutex.

### 11.2 Cmd vs Ctrl hotkeys in `index.html` (high priority)

Anything `index.html` binds to `Ctrl+...` (Ctrl+F search, Ctrl+V paste-in-URL,
arrow-key scrubbing modifiers, etc.) needs to listen for `event.metaKey` on
Mac instead of `event.ctrlKey`. The standard pattern:

```javascript
const IS_MAC = navigator.platform.toUpperCase().includes('MAC');
const cmdOrCtrl = (e) => IS_MAC ? e.metaKey : e.ctrlKey;

// usage
if (cmdOrCtrl(e) && e.key === 'f') { ... }
```

Grep `index.html` for `ctrlKey`, `Ctrl+`, and `e.key === 'Control'` and
audit each call site. Should be under 20 spots.

### 11.3 PyInstaller architecture (medium priority — affects who can run the build)

PyInstaller on Mac defaults to building for **only the architecture of the
host machine**. An Apple Silicon build won't run on Intel and vice versa.

For a universal binary that runs on both:

1. Install a universal2 Python: <https://www.python.org/downloads/macos/> (the
   "macOS 64-bit universal2 installer", **not** Homebrew Python which is
   single-arch).
2. Recreate the venv with that Python: `/usr/local/bin/python3.12 -m venv .venv`.
3. Use universal2 ffmpeg binaries from evermeet.cx (the `static` builds are
   universal2).
4. Build with `pyinstaller --target-arch universal2 ProTubeSaver-mac.spec`.

If you only need it on your own Mac, skip this — the default single-arch
build is smaller and faster.

### 11.4 WKWebView video codec support (medium priority)

`_is_web_compatible()` in `logic.py` is calibrated to Chromium's `<video>`
support. WKWebView's support is similar but not identical:

- HEVC: works on macOS 11+ in WKWebView, but only with hardware decode (which
  Apple Silicon and most modern Intel Macs have). Should be fine.
- WebM with VP9: works.
- MKV containers: **not** natively supported by WKWebView. The existing
  transcode worker remuxes to MP4 — verify it actually runs on `.mkv` files
  on Mac.
- AC-3 / E-AC-3 audio: works.

Test plan: drop one file each of `.mkv`, `.webm`, HEVC `.mp4`, and a regular
H.264 `.mp4` into the library and play each. If any play silently or stutter,
extend `_is_web_compatible()` with a `sys.platform == 'darwin'` branch that
forces transcoding for those containers.

### 11.5 Media keys / Now Playing widget (medium — Mac UX gap)

On Windows we set `AppUserModelID` so the System Media Transport Controls
show "ProTube Saver" in the volume tray, and media keys (Play/Pause, Next,
Prev) control the app.

Mac equivalent is `MPNowPlayingInfoCenter` + `MPRemoteCommandCenter` (via
`pyobjc`'s `MediaPlayer` framework). Without it:
- The keyboard's media keys won't control ProTube.
- ProTube won't show up in Control Center's Now Playing widget.
- AirPods' play/pause won't pause ProTube.

Implementation lives in `logic.py` as a small Darwin-only `_NowPlayingMac`
class that the player view's `play`/`pause`/`seek` events call into. Pattern:

```python
if sys.platform == 'darwin':
    from MediaPlayer import MPNowPlayingInfoCenter, MPRemoteCommandCenter
    # ... wire title, artist, artwork, position, duration on play/pause/seek
```

~80 lines. Defer until §1–§10 are working.

### 11.6 Dark titlebar / appearance (low — cosmetic)

By default pywebview's Cocoa window has a light-mode titlebar that looks
wrong against our black UI. One-line fix in a small Mac-only `apply_window_polish`
branch (currently early-returns on non-win32 — replace with a Cocoa branch):

```python
if sys.platform == 'darwin':
    try:
        from AppKit import NSApp, NSAppearance
        for w in NSApp.windows():
            w.setAppearance_(NSAppearance.appearanceNamed_('NSAppearanceNameDarkAqua'))
        return True
    except Exception:
        return False
```

### 11.7 Firewall prompt on first launch (low — likely a non-issue)

The localhost video server binds to `127.0.0.1`, which **shouldn't** trigger
Mac's "Do you want to accept incoming network connections?" dialog. But if
the user's pf rules or a third-party firewall (Little Snitch, LuLu) prompt
anyway, the answer is "Allow" — we're only serving to ourselves.

Verify on first launch. If the prompt fires, the fix is to codesign the
build so Mac trusts it as a known app (which you need for distribution
anyway — see §11.8).

### 11.8 Codesign + notarization (deferred — only blocks distribution)

For **personal use**, ignore. Right-click the `.app` → Open the first time,
confirm in the dialog, done.

For **distributing to other people** (even one other person), Gatekeeper
will refuse to open an unsigned `.app` with no override option on newer
macOS. To fix:
1. Apple Developer Program: $99/yr.
2. Codesign: `codesign --deep --force --options runtime --sign "Developer ID Application: Your Name (TEAMID)" "dist/ProTube Saver.app"`
3. Notarize: zip the `.app`, upload via `xcrun notarytool submit ... --wait`, then `xcrun stapler staple "dist/ProTube Saver.app"`.

Not blocking, not urgent. Park until you actually have a Mac user other
than yourself.

### 11.9 Cross-platform settings.json migration (deferred — edge case)

If anyone copies a Windows `data/settings.json` to a Mac, every library
entry's `filepath` is a `C:\Users\...\file.mkv` string that doesn't resolve.
Same problem in reverse.

Not worth solving until someone hits it. When they do, the fix is a
"missing files" view in the UI that lets the user remap a folder root.

### 11.10 `fresh-demo.sh` (low — convenience)

Mac analogue of `fresh-demo.bat`:

```bash
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if pgrep -f "ProTube Saver" > /dev/null; then
    echo "ProTube Saver is running. Quit it first."
    exit 1
fi
if [ -d data.bak ]; then
    rm -rf data && mv data.bak data && echo "Restored real data."
else
    mv data data.bak && echo "Swapped to fresh demo state."
fi
```

`chmod +x fresh-demo.sh`.

### 11.11 `.command` launcher (low — convenience)

Mac equivalent of `ProTube Launch.vbs` for double-clickable launching from
Finder without going through the `.app` build:

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate
exec python src/main.py
```

Save as `ProTube Launch.command`, `chmod +x` it, double-click it.

### 11.12 Update `CLAUDE.md` after the port lands

Once Mac runs end-to-end, add a "Cross-platform notes" section to
`CLAUDE.md` so future-Claude knows:
- Mac dev: `source .venv/bin/activate && python src/main.py`
- Mac build: `pyinstaller ProTubeSaver-mac.spec`
- Mac data lives at `~/Library/Application Support/ProTube Saver/` in
  frozen builds; project-root `data/` in dev.
- WKWebView ≠ WebView2 — the landmines in the existing "WebView2 landmines"
  section are Windows-only. Don't apply them on Mac.

---

## Quick reference: platform-conditional code

| File | Symbol | Status |
|---|---|---|
| `main.py` L26 | `SetCurrentProcessExplicitAppUserModelID` | Guarded ✅ |
| `main.py` L41 | Single-instance mutex | Guarded ✅ |
| `main.py` L129 | `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` | **Needs guard** |
| `main.py` L137 | `WEBVIEW2_DEFAULT_BACKGROUND_COLOR` | **Needs guard** |
| `main.py` L146 | BrowserForm monkey-patch | **Needs guard** (will crash on import) |
| `logic.py` L7055 | `apply_window_polish` | Early-returns on non-win32 ✅ |
| `logic.py` L414+ | `subprocess.Popen` w/ `CREATE_NO_WINDOW` | Ternary-guarded ✅ |
| `logic.py` L6977 / L7165 | `os.startfile` | Branched (darwin → `open`) ✅ |
| `logic.py` L6565 | `_ffmpeg_path` | **Needs `assets/mac/` lookup** |
| `app_paths.py` L35 | `app_dir()` | **Needs darwin branch** |
