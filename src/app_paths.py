"""
Single source of truth for ProTube Saver's filesystem layout.

Imported by main.py and logic.py BEFORE any heavy imports. Contains zero
side-effects at import time — just constants and pure functions.

Layout next to the exe (frozen) or project dir (dev):

    <app-dir>/
        ProTubeSaver.exe              ← the executable (or main.py in dev)
        data/                         ← everything the app owns
            settings.json             ← user preferences
            thumbnails/               ← cached thumbnail jpegs
            yt-dlp-runtime/           ← auto-updated yt-dlp (managed by updater.py)
            downloads/                ← default video output (configurable; user
                                         can repoint to anywhere via the UI)

Why next-to-exe instead of ~/Downloads/ProTube Saver/:
- Folder doesn't clutter ~/Downloads sorted by Date Modified
- Fully portable — move the exe folder, everything moves with it
- Fresh installs are self-contained (no AppData orphans)
- Demos can swap the data folder for a clean state without touching anywhere else

Migration: the first launch on the new layout copies settings.json + thumbnails/
+ yt-dlp-runtime/ from ~/Downloads/ProTube Saver/ if it exists. The old folder
is left in place untouched as a backup. Video files (download_folder contents)
are NOT moved — the existing settings.json points at wherever the user already
configured, and library entries reference absolute paths which keep working.
"""

import os
import sys


def app_dir():
    """Directory the app data lives in. Always a real, existing directory.

    Frozen (PyInstaller) build: the directory containing the exe. We use
    sys.executable here, not sys._MEIPASS — _MEIPASS is the temp extraction
    dir, which gets wiped on exit and is read-only anyway. We want the
    persistent location next to the exe.

    Dev (running `python main.py`): the directory of main.py / the script that
    started the process. We can't use cwd() because someone could launch from
    anywhere; we want the project folder where the source lives.
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller --onefile sets sys.frozen and sys.executable points at
        # the extracted exe, which lives in the user's chosen install location.
        return os.path.dirname(os.path.abspath(sys.executable))
    # Dev mode — anchor on this file's location and climb one level out of src/
    # to reach the project root (where data/ lives). Post-reorg layout:
    #   <project-root>/
    #       src/app_paths.py  ← __file__
    #       data/
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    """The <app-dir>/data/ folder. Created on demand."""
    p = os.path.join(app_dir(), 'data')
    os.makedirs(p, exist_ok=True)
    return p


def settings_path():
    """Absolute path to settings.json. Parent dir is guaranteed to exist."""
    return os.path.join(data_dir(), 'settings.json')


def thumbnails_dir():
    """Cached thumbnails folder. Created on demand."""
    p = os.path.join(data_dir(), 'thumbnails')
    os.makedirs(p, exist_ok=True)
    return p


def ytdlp_runtime_dir():
    """Folder the YtDlpUpdater downloads its yt-dlp wheel into. Created on demand.

    Kept separate from data_dir() root so the updater can `rm -rf` it safely
    without nuking user data.
    """
    p = os.path.join(data_dir(), 'yt-dlp-runtime')
    os.makedirs(p, exist_ok=True)
    return p


def default_downloads_dir():
    """Default location for downloaded videos when the user hasn't configured
    one. Lives inside data/ so a portable install is fully self-contained.
    Created on demand.

    NOTE: this is the *default*. The actual download folder used at runtime is
    settings['download_folder'], which the user can repoint anywhere via the
    UI. Existing users keep whatever they configured — see migrate_legacy().
    """
    p = os.path.join(data_dir(), 'downloads')
    os.makedirs(p, exist_ok=True)
    return p


def music_dir():
    """Where audio-only music downloads land. Tree under here is organized by
    Artist/Album so files are also usable from any external music player.
    Created on demand."""
    p = os.path.join(data_dir(), 'music')
    os.makedirs(p, exist_ok=True)
    return p


def transcoded_cache_dir():
    """Cache for in-app-playable copies of legacy library videos that the
    Chromium <video> tag can't decode natively (MKV containers, HEVC, etc.).
    The video server transcodes/remuxes into here on first play and serves
    the cached copy on subsequent plays. Created on demand."""
    p = os.path.join(data_dir(), 'transcoded')
    os.makedirs(p, exist_ok=True)
    return p


def legacy_app_data_dir():
    """The OLD location ProTube used before the portable refactor:
    ~/Downloads/ProTube Saver/. We read from here once on first launch to
    migrate existing settings, then never touch it again."""
    return os.path.join(os.path.expanduser('~'), 'Downloads', 'ProTube Saver')


def migrate_legacy():
    """One-time copy from legacy ~/Downloads/ProTube Saver/ → <app-dir>/data/.

    Safe to call on every launch — checks markers and no-ops if migration is
    already done or if there's nothing to migrate. Copies (doesn't move) so the
    legacy folder stays as a backup until the user manually deletes it.

    Files migrated:
      - settings.json
      - thumbnails/   (whole subtree)
      - yt-dlp-runtime/   (whole subtree)

    Files NOT migrated:
      - Video downloads. Library entries store absolute paths which keep
        resolving to ~/Downloads/ProTube Saver/<video>/ unchanged.
      - .corrupt backups, logs, anything else.

    Returns a short status string for logging. Never raises.
    """
    try:
        marker = os.path.join(data_dir(), '.migrated_from_legacy')
        if os.path.exists(marker):
            return 'already-migrated'

        legacy = legacy_app_data_dir()
        if not os.path.isdir(legacy):
            # No legacy folder = fresh install. Drop the marker so we don't
            # re-check on every launch.
            with open(marker, 'w') as f:
                f.write('no-legacy-folder\n')
            return 'no-legacy'

        import shutil
        copied = []

        # settings.json — copy only if dest doesn't already exist (don't
        # clobber whatever the user might have created in the new location).
        legacy_settings = os.path.join(legacy, 'settings.json')
        new_settings = settings_path()
        if os.path.isfile(legacy_settings) and not os.path.exists(new_settings):
            shutil.copy2(legacy_settings, new_settings)
            copied.append('settings.json')

        # thumbnails/ — merge (copy any file that doesn't exist in dest)
        legacy_thumbs = os.path.join(legacy, 'thumbnails')
        if os.path.isdir(legacy_thumbs):
            new_thumbs = thumbnails_dir()
            for name in os.listdir(legacy_thumbs):
                src = os.path.join(legacy_thumbs, name)
                dst = os.path.join(new_thumbs, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    try:
                        shutil.copy2(src, dst)
                    except OSError:
                        pass
            copied.append('thumbnails/')

        # yt-dlp-runtime/ — same merge approach
        legacy_runtime = os.path.join(legacy, 'yt-dlp-runtime')
        if os.path.isdir(legacy_runtime):
            new_runtime = ytdlp_runtime_dir()
            for root, dirs, files in os.walk(legacy_runtime):
                rel = os.path.relpath(root, legacy_runtime)
                target_root = new_runtime if rel == '.' else os.path.join(new_runtime, rel)
                os.makedirs(target_root, exist_ok=True)
                for name in files:
                    src = os.path.join(root, name)
                    dst = os.path.join(target_root, name)
                    if not os.path.exists(dst):
                        try:
                            shutil.copy2(src, dst)
                        except OSError:
                            pass
            copied.append('yt-dlp-runtime/')

        # Drop the marker so we never run again
        with open(marker, 'w') as f:
            f.write(f'migrated: {", ".join(copied) if copied else "(empty legacy folder)"}\n')

        return f'migrated: {", ".join(copied)}' if copied else 'legacy-folder-empty'
    except Exception as e:
        # Never let migration crash the app. Silent failure → user just gets
        # a fresh-install experience, which is recoverable.
        return f'migration-error: {e}'
