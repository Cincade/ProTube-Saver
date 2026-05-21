# main.py (Updated for PyInstaller compatibility)
import sys
import os

# Force stdout/stderr to UTF-8 with replacement on errors. Without this, any
# print() that includes emoji or non-cp1252 characters from a video title or
# filepath crashes the calling thread with UnicodeEncodeError. That used to
# bubble up through pywebview as "Stream prep failed: 'charmap' codec can't
# encode characters..." — a print()-side crash masquerading as a logic bug.
# `errors='replace'` swaps unencodable chars for '?', so prints stay best-
# effort without taking down anything they were just supposed to log.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

# Tell Windows our process is its own distinct app for shell purposes (taskbar
# grouping, jumplists, and — critically for us — the System Media Transport
# Controls source-name resolution that the volume/media tray uses). Without an
# explicit AppUserModelID, Chromium-inside-WebView2 falls back to the host
# process's defaults, which the OS surfaces as "Unknown app is playing audio".
# Must run before any window is created so Windows associates the ID with this
# process from the start. Silent no-op on non-Windows.
if sys.platform == 'win32':
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ProTubeSaver.App.1')
    except Exception as _e:
        print(f'[ProTube] AppUserModelID set failed: {_e}')

# Single-instance guard. Two ProTube processes writing to the same data/
# folder produce torn settings.json saves (we already paid for one such
# corruption — see _try_recover_truncated_json). A Windows named mutex is
# the cleanest fix: the OS releases it the moment our process exits, clean
# or crashed, so there's no stale-lock window. If creation reports
# ERROR_ALREADY_EXISTS we know another ProTube is already alive and we
# bail with a Win32 message box. Held for the lifetime of the process via
# the module-level _SINGLE_INSTANCE_MUTEX reference (do NOT delete it).
if sys.platform == 'win32':
    try:
        import ctypes
        _ERROR_ALREADY_EXISTS = 183
        _MUTEX_NAME = 'Local\\ProTubeSaver.SingleInstance.v1'
        _kernel32 = ctypes.windll.kernel32
        _kernel32.CreateMutexW.restype = ctypes.c_void_p
        _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        _SINGLE_INSTANCE_MUTEX = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if _kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            # Another ProTube is already running — show a message and exit.
            # MB_OK | MB_ICONINFORMATION = 0x00 | 0x40 = 0x40
            try:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    'ProTube Saver is already running.\n\nLook for it on the taskbar — only one instance can run at a time so your library and queue stay safe.',
                    'ProTube Saver',
                    0x40,
                )
            except Exception:
                pass
            sys.exit(0)
    except Exception as _e:
        # Mutex setup failed — log and continue. Worst case we lose the
        # single-instance guarantee but the app still launches.
        print(f'[ProTube] single-instance mutex setup failed: {_e}')

# CRITICAL ORDER: Bootstrap the yt-dlp runtime folder onto sys.path BEFORE we
# import anything that imports yt_dlp. This way, when logic.py does `from yt_dlp
# import YoutubeDL`, Python finds the freshly-downloaded version (if any) ahead
# of the bundled one. Without this, a stale yt_dlp gets cached in sys.modules
# and the updater's downloads do nothing.
#
# As of the portable-storage refactor, app_paths is the single source of truth
# for where data lives. It MUST be imported before anything else that builds
# paths or imports yt_dlp, so the directory layout is established and the
# legacy ~/Downloads/ProTube Saver/ migration runs once before logic.py boots.
from app_paths import ytdlp_runtime_dir, migrate_legacy

# Run the one-time legacy migration. Idempotent — drops a marker file inside
# data/ on first run and no-ops on subsequent launches. Never raises.
_migration_status = migrate_legacy()
print(f'[ProTube] storage migration: {_migration_status}')

try:
    from updater import YtDlpUpdater
    YtDlpUpdater.bootstrap_sys_path(ytdlp_runtime_dir())
except Exception as e:
    print(f"[ProTube] updater bootstrap failed (continuing with bundled yt-dlp): {e}")

import webview
from logic import API

# Disable background throttling on Windows WebView2 / Chromium. Without this,
# the webview's JavaScript gets throttled when the window loses focus — progress
# bars pause, UI updates queue up, nothing refreshes until you click the window
# again. These flags keep JS running at full speed even when the window is hidden
# or in the background.
#
# Note: the first four flags handle BACKGROUND throttling (other tab/window
# focused, window minimized, occluded). The IntensiveWakeUpThrottling pair
# handles FOREGROUND-IDLE throttling — Chromium ratchets timers down to once
# per minute after ~5 min of no user input.
#
# 2026-05 (B6 fix): user reported the player going unresponsive after ~4hr idle —
# took ~1min of clicking to wake. Adding more aggressive throttle-disable + the
# memory-saver / page-freeze features Chromium introduced for resource-saver mode
# (TabFreeze, PageFreeze, FreezePolicy) that we also need to disable for a long-
# running desktop app. Also disabling HighEfficiencyModeAvailable so Edge's "memory
# saver" can't kick in. The frontend pairs this with a 60s heartbeat that touches
# the document so any scheduler-based throttling has a recent activity signal.
_chromium_flags = [
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows',
    '--disable-features=' + ','.join([
        'CalculateNativeWinOcclusion',
        'IntensiveWakeUpThrottling',
        'IntensiveWakeUpThrottling_V2',
        'ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes',
        'TabFreeze',                       # disable proactive tab freezing after idle
        'PageFreeze',                      # disable page lifecycle "frozen" state
        'FreezePolicy',                    # disable the older freeze-policy heuristic
        'HighEfficiencyModeAvailable',     # disable Edge memory-saver / efficiency mode
        'BackForwardCache',                # we don't need it; it's a sleep-the-page mechanism
        'HeavyAdIntervention',
    ]),
]
os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = ' '.join(_chromium_flags)

# WebView2's native control paints its DefaultBackgroundColor (white by default)
# in the gap between when the control resizes and when HTML rendering catches up
# — that's the white sliver at the window corners after toggling fullscreen.
# Format is 0xAARRGGBB; 0xFF000000 = opaque black, invisible against our dark UI.
# Must be set before webview.start() runs. (pywebview's background_color= param
# is silently ignored on WebView2 per its changelog, so the env var is the only path.)
os.environ['WEBVIEW2_DEFAULT_BACKGROUND_COLOR'] = '0xFF000000'

# Monkey-patch pywebview's BrowserForm.__init__ to force-set the host Form's
# BackColor to black. pywebview supposedly does this from background_color=, but
# the assignment is inside a conditional that gets skipped on the WebView2 backend
# in our case, leaving the form with the default light gray that flashes white
# at launch (before WebView2 paints the splash) and during the un-maximize→resize
# sequence inside toggle_fullscreen. Wrapping __init__ guarantees BackColor is set
# *before* the form is ever shown — so the launch flash is black, not white.
try:
    from webview.platforms import winforms as _wf_module
    _form_cls = getattr(_wf_module, 'BrowserView', None) or getattr(_wf_module, 'BrowserForm', None)
    if _form_cls is not None:
        _orig_form_init = _form_cls.__init__
        def _patched_form_init(self, *args, **kwargs):
            _orig_form_init(self, *args, **kwargs)
            try:
                from System.Drawing import Color
                self.BackColor = Color.FromArgb(255, 0, 0, 0)
            except Exception:
                pass
        _form_cls.__init__ = _patched_form_init
except Exception as _e:
    print(f'[ProTube] BrowserForm BackColor monkey-patch skipped: {_e}')

def resource_path(relative_path):
    """ Get absolute path to resource, works in dev and for PyInstaller.

    In frozen builds: PyInstaller exposes sys._MEIPASS pointing at the temp
    extraction dir where bundled resources live.

    In dev: anchor on this file's location, NOT cwd. cwd depends on where the
    user launched python from (the .vbs runs from project root, but a manual
    `cd src && python main.py` would have cwd=src/) — anchoring on __file__
    means index.html resolves correctly regardless of how main.py was started.
    """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def _log_diag(msg):
    # pythonw (the VBS launcher) discards stdout, so persist diagnostics to a
    # log file in the app data folder. Tail this file to see what happened.
    try:
        os.makedirs(_app_data, exist_ok=True)
        with open(os.path.join(_app_data, 'protube.log'), 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


def _schedule_mac_maximize(window):
    """Maximize the window to fill the visible screen on launch (menu bar + Dock
    stay visible) WITHOUT entering native macOS fullscreen.

    We deliberately no longer call toggleFullScreen_: a window in a native
    fullscreen Space CANNOT be minimized (macOS greys out the yellow button and
    ⌘M) — that was the user's "I can't minimize" bug. A maximized normal window
    fills the screen the same way but keeps minimize working; the user can still
    go full-immersive anytime via the green traffic-light button or the in-app F
    shortcut.

    Recipe:
      1. Hook `window.events.shown`/`loaded` for correct timing.
      2. AppHelper.callAfter to dispatch on the main thread (Cocoa requires
         UI ops on main).
      3. Read `window.native` for the actual NSWindow instance.
      4. setFrame_display_(screen.visibleFrame(), True) — the Cocoa "zoom to
         fill" without the fullscreen-Space side effects.

    Guarded with `_did_fs['done']` so the timer fallbacks don't re-fire."""
    if sys.platform != 'darwin' or window is None:
        return
    import threading as _th
    _did_fs = {'done': False}

    def _go_fullscreen(caller='?'):
        # Lets the verbose log distinguish whether the call came from the
        # shown-event hook or one of the timer fallbacks.
        if _did_fs['done']:
            _log_diag(f'[ProTube] mac fullscreen ({caller}): already done, skipping')
            return
        try:
            from PyObjCTools import AppHelper
            from AppKit import NSApp

            def _on_main():
                if _did_fs['done']:
                    _log_diag(f'[ProTube] mac fullscreen on-main ({caller}): already done')
                    return
                try:
                    ns_window = getattr(window, 'native', None)
                    _log_diag(f'[ProTube] mac fullscreen on-main ({caller}): window.native={ns_window is not None}')
                    candidates = [ns_window] if ns_window is not None else list(NSApp.windows())
                    _log_diag(f'[ProTube] mac fullscreen on-main ({caller}): candidates={len(candidates)}')
                    for w in candidates:
                        if w is None:
                            continue
                        try:
                            vis = bool(w.isVisible())
                            mini = bool(w.isMiniaturized())
                            in_fs = bool(w.styleMask() & (1 << 14))
                            _log_diag(f'[ProTube] mac maximize on-main ({caller}): visible={vis} mini={mini} in_fs={in_fs}')
                            if not vis:
                                continue
                            # Maximize = fill the visible screen as a NORMAL window.
                            # We intentionally do NOT toggleFullScreen_: a window in a
                            # fullscreen Space can't be minimized (the user's bug). If
                            # a prior build left us in fullscreen, back out first so
                            # minimize is available again.
                            if in_fs:
                                w.toggleFullScreen_(None)
                                _log_diag(f'[ProTube] mac maximize on-main ({caller}): exited stale fullscreen')
                            if mini:
                                w.deminiaturize_(None)
                            # Keep the window fullscreen-CAPABLE so the green
                            # traffic-light button + the in-app F shortcut can still
                            # enter true fullscreen on demand — we just don't enter it
                            # now. 128 == NSWindowCollectionBehaviorFullScreenPrimary.
                            # (pywebview's own toggle_fullscreen re-sets this on F, so
                            # this only matters for the green button before first F.)
                            try:
                                cb = w.collectionBehavior()
                                if not (cb & 128):
                                    w.setCollectionBehavior_(cb | 128)
                            except Exception:
                                pass
                            try:
                                from AppKit import NSScreen as _NSScreen
                                screen = w.screen() or _NSScreen.mainScreen()
                                if screen is not None:
                                    w.setFrame_display_(screen.visibleFrame(), True)
                                    _log_diag(f'[ProTube] mac maximize on-main ({caller}): setFrame to visibleFrame')
                            except Exception as _e:
                                _log_diag(f'[ProTube] mac maximize on-main ({caller}) setFrame: {_e}')
                            _did_fs['done'] = True
                            break
                        except Exception as e:
                            _log_diag(f'[ProTube] mac fullscreen on-main ({caller}) per-window: {e}')
                except Exception as e:
                    _log_diag(f'[ProTube] mac fullscreen on-main ({caller}) outer: {e}')
            AppHelper.callAfter(_on_main)
            _log_diag(f'[ProTube] mac fullscreen dispatch ({caller}): callAfter scheduled')
        except Exception as e:
            _log_diag(f'[ProTube] mac fullscreen dispatch ({caller}): {e}')

    # Try `loaded` (DOM ready) first so WebKit has actually painted at least
    # the first frame before we toggle fullscreen. The previous `shown`-event
    # hook fired too early — OS fullscreen captured the pre-paint state and
    # showed a black window (user 2026-05-17 regression). 500ms post-event
    # delay leaves a comfortable margin for the splash/index.html to render.
    hooked = False
    for ev_name in ('loaded', 'shown'):
        try:
            ev = getattr(window.events, ev_name)
            def _delayed(_ev=ev_name):
                _th.Timer(0.5, _go_fullscreen, args=(f'{_ev}-event',)).start()
            ev += (lambda: _delayed())
            _log_diag(f'[ProTube] mac fullscreen hooked to window.events.{ev_name} (+500ms)')
            hooked = True
            break
        except Exception as e:
            _log_diag(f'[ProTube] mac fullscreen {ev_name}-hook failed: {e}')
    # Fallback retries — only fire if the event hook didn't (or much later).
    # Bumped to 2/3.5/5s so they don't pre-empt the paint either.
    for delay in (2.0, 3.5, 5.0):
        _th.Timer(delay, _go_fullscreen, args=(f'timer-{delay}s',)).start()


def _on_window_ready(api, window):
    # On macOS, pywebview's Cocoa backend creates the window but doesn't bring
    # the process to the foreground — the window ends up behind other apps and
    # the dock icon doesn't bounce. Force-activate so the user sees the window
    # immediately on launch (matches Windows' behavior). No-op on Win/Linux.
    if sys.platform == 'darwin':
        try:
            from AppKit import NSApp, NSApplicationActivationPolicyRegular
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            NSApp.activateIgnoringOtherApps_(True)
            _log_diag('[ProTube] mac window activation: ok')
            # Schedule window maximize via pywebview's main-thread-safe path
            # (was previously using setFrame_ from a worker thread; Cocoa
            # silently ignored some of those calls).
            _schedule_mac_maximize(window)
        except Exception as e:
            _log_diag(f'[ProTube] mac window activation: {e}')
        # Mac doesn't need the Win11 corner/frame polish — skip the loop below.
        return

    # Apply our window polish (square Win11 corners + frame redraw) once the
    # native window is realized. The API method is shared with set_fullscreen,
    # which calls it again after every fullscreen toggle to undo pywebview's
    # re-rounding of the corners on exit.
    import time as _time
    for _ in range(30):
        try:
            if api.apply_window_polish():
                _log_diag('[ProTube] startup window-polish: ok')
                return
        except Exception as e:
            _log_diag(f'[ProTube] startup window-polish exception (continuing): {e}')
            return
        _time.sleep(0.1)
    _log_diag('[ProTube] startup window-polish: failed (window not found in 3s)')


if __name__ == '__main__':
    api = API()
    # Window size at creation time. pywebview's `maximized=True` is honored on
    # Windows (the WinForms backend) but is a no-op on the Cocoa backend —
    # confirmed via pywebview's source. After many failed attempts to resize
    # after-the-fact (window.events.shown hook, AppHelper.callAfter +
    # setFrame_display_, NSWindow.zoom_), the only thing that reliably gives
    # the user a "maximized on launch" experience on macOS is to pre-size
    # the window at creation time to the visible-screen dimensions. We read
    # NSScreen.visibleFrame() (which excludes menubar + dock = correct
    # "maximize" semantics) and pass those as width/height. Centered placement
    # falls back to pywebview's default.
    _init_w, _init_h = 1280, 800
    if sys.platform == 'darwin':
        try:
            from AppKit import NSScreen
            _screen = NSScreen.mainScreen() or (NSScreen.screens()[0] if NSScreen.screens() else None)
            if _screen is not None:
                _vf = _screen.visibleFrame()
                _init_w = max(int(_vf.size.width), 1024)
                _init_h = max(int(_vf.size.height), 700)
        except Exception:
            pass
    window = webview.create_window(
        'ProTube Saver',
        # --- CRITICAL CHANGE: Use resource_path to find the bundled HTML file ---
        resource_path('index.html'),
        js_api=api,
        width=_init_w,
        height=_init_h,
        resizable=True,
        maximized=True,  # Honored on Windows; no-op on Mac (handled via size)
        # Sets the host WinForm's BackColor. Without this, the form paints its
        # default white during the gap before WebView2 loads (visible at launch
        # before the splash) and during the un-maximize→resize→re-maximize
        # sequence inside toggle_fullscreen (visible as white flashes during
        # the transition). Black eliminates both — the gaps blend into our UI.
        background_color='#000000',
    )

    # Debug mode should be False for the final deployed application
    webview.start(_on_window_ready, (api, window), debug=False)