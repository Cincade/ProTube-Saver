import os, sys, threading, subprocess, webview, importlib.metadata, json


class UiMixin:
    def open_folder(self, path=None):
        """Open a folder in the OS file manager. Defaults to the main download folder."""
        target = path or self.download_folder
        if not target or not os.path.exists(target):
            return False
        try:
            if sys.platform == 'win32':
                os.startfile(target)
            elif sys.platform == 'darwin':
                subprocess.run(['open', target], check=False)
            else:
                subprocess.run(['xdg-open', target], check=False)
            return True
        except OSError:
            return False

    def open_external_url(self, url):
        """Open a URL in the user's default browser. Used by 'Open on YouTube' in detail panel."""
        if not url or not isinstance(url, str):
            return False
        # Only allow http(s) URLs — guard against file:// or anything weirder
        if not (url.startswith('http://') or url.startswith('https://')):
            return False
        try:
            if sys.platform == 'win32':
                os.startfile(url)
            elif sys.platform == 'darwin':
                subprocess.run(['open', url], check=False)
            else:
                subprocess.run(['xdg-open', url], check=False)
            return True
        except OSError:
            return False
        except Exception:
            return False

    def toggle_fullscreen(self):
        """Toggle the OS window's fullscreen state. Used by the player to enter true
        borderless fullscreen — JS requestFullscreen alone leaves the pywebview title
        bar visible because WebView2 doesn't propagate fullscreen to the host window."""
        try:
            if webview.windows:
                webview.windows[0].toggle_fullscreen()
                return {'ok': True}
        except Exception as e:
            print(f'[ProTube] toggle_fullscreen failed: {e}')
        return {'ok': False}

    def set_fullscreen(self, want_fullscreen):
        """Idempotent fullscreen — set the OS window to a specific state instead of
        toggling. The player calls this on enter (True) and exit (False); we only fire
        the underlying toggle_fullscreen() when state actually needs to change. Without
        this, JS↔Python state would drift over multiple toggles and the window would
        end up stuck in a half-borderless state. pywebview doesn't expose a way to
        query the current fullscreen state portably, so we track it ourselves."""
        try:
            want = bool(want_fullscreen)
            if not hasattr(self, '_window_is_fullscreen'):
                self._window_is_fullscreen = False
            if self._window_is_fullscreen == want:
                return {'ok': True, 'changed': False}
            if webview.windows:
                webview.windows[0].toggle_fullscreen()
                self._window_is_fullscreen = want
                # pywebview's toggle_fullscreen marshals its WinForms work onto the
                # UI thread, so it runs partly *after* this call returns. If we only
                # apply polish immediately, pywebview's own DWM re-round-corners and
                # FormBorderStyle restore can land *after* our polish, undoing it.
                # Schedule polish at multiple offsets so at least one fires after
                # pywebview's UI-thread work has fully settled.
                for _delay in (0.0, 0.05, 0.2, 0.5):
                    threading.Timer(_delay, self.apply_window_polish).start()
                return {'ok': True, 'changed': True}
        except Exception as e:
            print(f'[ProTube] set_fullscreen failed: {e}')
        return {'ok': False}

    def apply_window_polish(self):
        """Force square Win11 corners on our window AND invalidate the cached frame.
        pywebview's toggle_fullscreen flips DWMWA_WINDOW_CORNER_PREFERENCE back to
        DEFAULT (rounded) on every exit, and never calls SetWindowPos with
        SWP_FRAMECHANGED — both contribute to the white-sliver/rounded-corner artifact
        after exiting fullscreen. This method is called at startup once and from
        set_fullscreen() after every toggle, undoing pywebview's behavior."""
        if sys.platform != 'win32':
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi
            user32.FindWindowW.restype = ctypes.c_void_p
            user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
            dwmapi.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint
            ]
            user32.SetWindowPos.restype = ctypes.c_int
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint
            ]
            user32.RedrawWindow.restype = ctypes.c_int
            user32.RedrawWindow.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint
            ]
            hwnd = user32.FindWindowW(None, 'ProTube Saver')
            if not hwnd:
                return False
            pref = ctypes.c_int(1)  # DWMWCP_DONOTROUND
            dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)
            )
            # DWMWA_BORDER_COLOR (34): pywebview restores this to DEFAULT on
            # exit, which is a light gray on Win11 that reads as a near-white
            # 1px edge. Force it to opaque black so any leaked border pixel
            # blends into our dark UI. COLORREF is 0x00BBGGRR.
            border = ctypes.c_uint(0x00000000)
            dwmapi.DwmSetWindowAttribute(
                hwnd, 34, ctypes.byref(border), ctypes.sizeof(border)
            )
            SWP_NOMOVE       = 0x0002
            SWP_NOSIZE       = 0x0001
            SWP_NOZORDER     = 0x0004
            SWP_NOACTIVATE   = 0x0010
            SWP_FRAMECHANGED = 0x0020
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags)
            # SetWindowPos(SWP_FRAMECHANGED) only invalidates the non-client area.
            # The WebView2 child's cached paint at the corners survives unless we
            # redraw all children synchronously too. RDW_ALLCHILDREN propagates the
            # invalidate down into the WebView2 control; RDW_UPDATENOW makes it
            # synchronous (no flicker window where old pixels are visible).
            RDW_INVALIDATE  = 0x0001
            RDW_UPDATENOW   = 0x0100
            RDW_ALLCHILDREN = 0x0080
            RDW_FRAME       = 0x0400
            user32.RedrawWindow(
                hwnd, None, None,
                RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN | RDW_FRAME,
            )
            # Force the WinForms host Form's BackColor to opaque black. pywebview's
            # background_color='#000000' parameter sets this conditionally and may
            # be skipped on the WebView2 backend, so we set it ourselves via
            # window.native (the BrowserForm) or the BrowserView.instances fallback.
            # Without this, the form paints its default color during the gap before
            # WebView2's first frame (visible white flash on launch) and during the
            # un-maximize→resize→re-maximize sequence inside toggle_fullscreen.
            backcolor_path = 'skipped'
            try:
                from System.Drawing import Color
                black = Color.FromArgb(255, 0, 0, 0)
                form = None
                if webview.windows:
                    win = webview.windows[0]
                    native = getattr(win, 'native', None)
                    if native is not None and hasattr(native, 'BackColor'):
                        form = native
                        backcolor_path = 'native'
                    else:
                        try:
                            from webview.platforms import winforms as _wf
                            cls = getattr(_wf, 'BrowserView', None) or getattr(_wf, 'BrowserForm', None)
                            if cls is not None:
                                instances = getattr(cls, 'instances', {})
                                if isinstance(instances, dict):
                                    cand = instances.get(win.uid) or (next(iter(instances.values()), None))
                                    if cand is not None and hasattr(cand, 'BackColor'):
                                        form = cand
                                        backcolor_path = 'BrowserView.instances'
                        except Exception as _e:
                            backcolor_path = f'instances-err:{_e}'
                if form is not None:
                    form.BackColor = black
            except Exception:
                # Silent — the failure path of the outer try/except still logs
                # genuinely broken cases. We don't log per-call BackColor results.
                pass
            return True
        except Exception as e:
            try:
                log_path = os.path.join(
                    os.path.expanduser('~'), 'Downloads', 'ProTube Saver', 'protube.log'
                )
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f'[ProTube] apply_window_polish failed: {e}\n')
            except Exception:
                pass
            return False

    def open_file(self, path):
        """Open a specific file with the OS default application."""
        if not path or not os.path.exists(path):
            return False
        try:
            if sys.platform == 'win32':
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.run(['open', path], check=False)
            else:
                subprocess.run(['xdg-open', path], check=False)
            return True
        except Exception:
            return False

    def reveal_in_folder(self, path):
        """Open the containing folder of `path` with the file selected (if OS supports it)."""
        if not path:
            return False
        if not os.path.exists(path):
            # File got deleted — open the folder anyway if we can
            parent = os.path.dirname(path)
            if os.path.exists(parent):
                return self.open_folder(parent)
            return False
        try:
            if sys.platform == 'win32':
                # /select, arg opens Explorer with the file highlighted
                subprocess.run(['explorer', '/select,', path], check=False)
            elif sys.platform == 'darwin':
                subprocess.run(['open', '-R', path], check=False)
            else:
                # Most Linux file managers don't have a standard "reveal" verb; just open parent
                subprocess.run(['xdg-open', os.path.dirname(path)], check=False)
            return True
        except Exception:
            return False

    def pause_download(self, vid):
        """Mark a video as paused. The _hook will raise on the next progress tick, stopping yt-dlp.
        The .part file on disk stays, so resuming picks up where it left off."""
        self.paused_ids.add(vid)

    def resume_download(self, vid):
        """Remove pause flag (so the next start_download pass can proceed)."""
        self.paused_ids.discard(vid)
        self.cancelled_ids.discard(vid)

    def cancel_all_downloads(self):
        """
        Mark every active download as cancelled. The _hook will raise on the next
        progress tick, which tears down the yt-dlp call. Unstarted threads waiting
        on the semaphore will see their id in cancelled_ids and bail out.
        """
        # Snapshot active ids so we don't mutate the dict we're iterating
        active_ids = list(self.active_downloads.keys())
        for vid in active_ids:
            self.cancelled_ids.add(vid)

    def cancel_download(self, vid):
        """Cancel a single download by id."""
        self.cancelled_ids.add(vid)
    def get_engine_version(self): return importlib.metadata.version('yt-dlp')
    def force_update_ytdlp(self):
        """Manual update trigger from UI. Honors the same nightly setting as startup."""
        def on_complete(msg):
            self._send_to_js('showToast', msg, None, None)
        use_nightly = bool(self.settings.get('yt_dlp_use_nightly', False))
        self.updater.update_in_background(callback=on_complete, include_nightly=use_nightly)
    
    def _send_to_js(self, func, *args):
        if webview.windows: webview.windows[0].evaluate_js(f"{func}({', '.join(json.dumps(a) for a in args)})")

    def _log_to_protube_log(self, msg):
        """Append a line to data/protube.log so failures are visible after the
        fact. pythonw discards stdout, so plain print() vanishes; this is the
        same file main.py's _log_diag writes to. Best-effort, never raises."""
        try:
            from app_paths import data_dir
            log_path = os.path.join(data_dir(), 'protube.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(msg.rstrip('\n') + '\n')
        except Exception:
            pass
        try:
            print(msg)
        except Exception:
            pass