"""
yt-dlp updater for ProTube.

Why this exists: yt-dlp publishes patches roughly weekly to keep up with
YouTube's extraction changes. If we bundle a frozen yt_dlp via PyInstaller,
downloads will silently break for users within ~30 days of release.

Strategy: Keep a writable yt-dlp-runtime/ folder outside the PyInstaller bundle.
Once a day, check PyPI for the latest version. If newer than what we have,
download the official Python wheel and extract its yt_dlp/ contents into our
runtime folder. At app startup we prepend that folder to sys.path BEFORE
importing yt_dlp — so Python finds the newer code there first, ignoring
whatever PyInstaller froze into the binary.

This works in BOTH modes:
- Dev (`python main.py`): prepends the runtime folder, falls back to the
  pip-installed yt_dlp if no update has been pulled
- Bundled .exe: prepends the runtime folder, falls back to the frozen yt_dlp.
  No subprocess pip calls — those don't work in a PyInstaller bundle.

Usage from main.py / logic.py:
    from updater import YtDlpUpdater
    YtDlpUpdater.bootstrap_sys_path(app_data_folder)  # BEFORE `import yt_dlp`
    # ... then later, in API.__init__:
    self.updater = YtDlpUpdater(app_data_folder)
    self.updater.check_on_startup(silent=True)
"""

import os
import sys
import io
import json
import time
import zipfile
import shutil
import tempfile
import threading


class YtDlpUpdater:
    # Once a day. yt-dlp ships patches faster (~weekly), so 24h is a reasonable
    # cadence — frequent enough that breakage windows are short, infrequent enough
    # that we don't hammer PyPI on every launch.
    CHECK_INTERVAL_SECONDS = 24 * 60 * 60

    PYPI_API = "https://pypi.org/pypi/yt-dlp/json"

    def __init__(self, app_data_folder):
        self.runtime_folder = os.path.join(app_data_folder, "yt-dlp-runtime")
        os.makedirs(self.runtime_folder, exist_ok=True)
        self.state_file = os.path.join(self.runtime_folder, "_state.json")
        self.is_checking = False

    # ----- Static bootstrap (called BEFORE importing yt_dlp anywhere) -----

    @staticmethod
    def bootstrap_sys_path(app_data_folder):
        """Prepend the runtime folder to sys.path so a downloaded yt_dlp wins
        over the bundled one. Safe to call even when the folder doesn't exist
        or contains nothing — Python just won't find anything there and falls
        through to the next path entry (the bundled yt_dlp)."""
        runtime_folder = os.path.join(app_data_folder, "yt-dlp-runtime")
        try:
            os.makedirs(runtime_folder, exist_ok=True)
        except OSError:
            return
        # Only prepend if there's an actual yt_dlp package waiting to be picked up
        if os.path.isdir(os.path.join(runtime_folder, "yt_dlp")):
            if runtime_folder not in sys.path:
                sys.path.insert(0, runtime_folder)
                print(f"[ProTube/updater] using updated yt-dlp from {runtime_folder}")

    # ----- State tracking -----

    def _load_state(self):
        try:
            with open(self.state_file, 'r') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state):
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass

    # ----- Version detection -----

    def get_current_version(self):
        """Get the version of yt-dlp Python actually has access to right now."""
        try:
            import yt_dlp
            return getattr(yt_dlp.version, '__version__', None)
        except Exception:
            return None

    def get_latest_version(self, include_nightly=False):
        """Query PyPI for the latest yt-dlp release.

        include_nightly=True walks every release on PyPI (including dev/nightly builds
        like '2026.5.3.233852.dev0') and returns the highest by packaging.version.parse.
        yt-dlp's stable releases lag YouTube extraction fixes by weeks — opting into
        nightly catches the breakage faster. Off by default to keep most users on the
        battle-tested stable.
        """
        try:
            import requests
            resp = requests.get(self.PYPI_API, timeout=8)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not include_nightly:
                return data.get('info', {}).get('version')
            # Pick the highest version across ALL releases.
            from packaging.version import parse, InvalidVersion
            releases = list((data.get('releases') or {}).keys())
            best = None
            best_parsed = None
            for v in releases:
                try:
                    p = parse(v)
                except InvalidVersion:
                    continue
                if best_parsed is None or p > best_parsed:
                    best_parsed = p
                    best = v
            return best or data.get('info', {}).get('version')
        except Exception:
            pass
        return None

    def _is_newer(self, candidate, current):
        """Compare yyyy.mm.dd versions. Returns True if candidate > current."""
        if not candidate or not current:
            return False
        try:
            from packaging.version import parse
            return parse(candidate) > parse(current)
        except Exception:
            # Fallback: lex compare. yt-dlp's date-based versioning makes this safe.
            return candidate > current

    # ----- Update download (the actual fix) -----

    def _download_and_install(self, version):
        """Download the wheel for the given version from PyPI, extract its
        yt_dlp/ subtree into our runtime folder. Wheels are zip files — we
        don't need pip, we just unzip the parts we want."""
        try:
            import requests

            resp = requests.get(f"https://pypi.org/pypi/yt-dlp/{version}/json", timeout=10)
            if resp.status_code != 200:
                return False, f"PyPI lookup failed: HTTP {resp.status_code}"

            data = resp.json()
            urls = data.get('urls', [])
            wheel_url = None
            for entry in urls:
                if entry.get('packagetype') == 'bdist_wheel':
                    wheel_url = entry.get('url')
                    break
            if not wheel_url:
                for entry in urls:
                    if entry.get('packagetype') == 'sdist':
                        wheel_url = entry.get('url')
                        break
            if not wheel_url:
                return False, "No installable file found on PyPI"

            print(f"[ProTube/updater] downloading {wheel_url}")
            wheel_resp = requests.get(wheel_url, timeout=60, stream=True)
            if wheel_resp.status_code != 200:
                return False, f"Wheel download failed: HTTP {wheel_resp.status_code}"

            # Wheels are usually 2-5MB, fine for in-memory handling
            buf = io.BytesIO()
            for chunk in wheel_resp.iter_content(64 * 1024):
                if chunk:
                    buf.write(chunk)
            buf.seek(0)

            staging = tempfile.mkdtemp(prefix="ytdlp_update_", dir=self.runtime_folder)
            try:
                with zipfile.ZipFile(buf) as zf:
                    for name in zf.namelist():
                        # Wheels: yt_dlp/...
                        # Source tarballs: yt-dlp-X.Y.Z/yt_dlp/...
                        if '/yt_dlp/' in name:
                            relative = name[name.index('/yt_dlp/') + 1:]
                        elif name.startswith('yt_dlp/'):
                            relative = name
                        else:
                            continue

                        if name.endswith('/'):
                            os.makedirs(os.path.join(staging, relative), exist_ok=True)
                            continue

                        target = os.path.join(staging, relative)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with zf.open(name) as src, open(target, 'wb') as dst:
                            shutil.copyfileobj(src, dst)

                staging_pkg = os.path.join(staging, 'yt_dlp')
                if not os.path.isfile(os.path.join(staging_pkg, '__init__.py')):
                    shutil.rmtree(staging, ignore_errors=True)
                    return False, "Wheel missing yt_dlp/__init__.py"

                live_pkg = os.path.join(self.runtime_folder, 'yt_dlp')
                if os.path.isdir(live_pkg):
                    # Move-aside instead of immediate delete in case the running process
                    # has files open (Windows). Next launch picks up the new one.
                    sidelined = os.path.join(
                        self.runtime_folder,
                        f'_old_yt_dlp_{int(time.time())}'
                    )
                    try:
                        shutil.move(live_pkg, sidelined)
                    except OSError:
                        shutil.rmtree(staging, ignore_errors=True)
                        return False, "Could not replace running yt_dlp (will retry next launch)"
                    self._cleanup_old_dirs()

                shutil.move(staging_pkg, live_pkg)
                shutil.rmtree(staging, ignore_errors=True)
                return True, f"Installed yt-dlp {version}"
            except Exception as e:
                shutil.rmtree(staging, ignore_errors=True)
                return False, f"Extraction failed: {e}"
        except Exception as e:
            return False, f"Update failed: {e}"

    def _cleanup_old_dirs(self):
        """Remove any sidelined _old_yt_dlp_* folders from previous updates.
        Best effort — if a file lock prevents deletion, skip and try later."""
        try:
            for entry in os.listdir(self.runtime_folder):
                if entry.startswith('_old_yt_dlp_'):
                    full = os.path.join(self.runtime_folder, entry)
                    if os.path.isdir(full):
                        shutil.rmtree(full, ignore_errors=True)
        except OSError:
            pass

    # ----- Public scheduling -----

    def _should_check(self):
        """True if we haven't checked within CHECK_INTERVAL_SECONDS."""
        state = self._load_state()
        last = state.get('last_check_at', 0)
        return (time.time() - last) > self.CHECK_INTERVAL_SECONDS

    def check_on_startup(self, silent=True, include_nightly=False):
        """Called from API.__init__. Fires a background thread that does the
        version check + update. Throttled to once per CHECK_INTERVAL_SECONDS."""
        if not self._should_check():
            return
        self.update_in_background(silent=silent, include_nightly=include_nightly)

    def update_in_background(self, silent=True, callback=None, include_nightly=False):
        """Run the check + update in a daemon thread so app startup isn't blocked.
        include_nightly=True grabs the latest dev build from PyPI; off by default."""
        if self.is_checking:
            return

        def worker():
            self.is_checking = True
            try:
                current = self.get_current_version()
                latest = self.get_latest_version(include_nightly=include_nightly)
                state = self._load_state()
                state['last_check_at'] = time.time()
                state['last_known_version'] = current
                state['last_latest_seen'] = latest
                self._save_state(state)

                if not latest:
                    if callback:
                        callback("Update check failed: couldn't reach PyPI")
                    return

                if not self._is_newer(latest, current):
                    if callback and not silent:
                        callback(f"yt-dlp is up to date ({current})")
                    return

                ok, msg = self._download_and_install(latest)
                if ok:
                    state['last_installed_version'] = latest
                    state['last_install_at'] = time.time()
                    self._save_state(state)
                if callback:
                    callback(msg)
            except Exception as e:
                if callback:
                    callback(f"Update worker crashed: {e}")
            finally:
                self.is_checking = False

        threading.Thread(target=worker, daemon=True).start()