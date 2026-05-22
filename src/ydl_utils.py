"""Shared low-level utilities used by logic.py and its domain mixins.

Kept separate to break potential circular imports:  mixin files can
`from ydl_utils import YoutubeDL, ...` without importing logic.py.
"""

import os
import sys


# Lazy-import wrapper for yt_dlp.YoutubeDL.
#
# yt_dlp's __init__ eagerly imports ~1800 extractor modules at module load,
# costing ~4.5 s on cold start. Deferring until first actual use turns "open
# the app" into a near-instant operation. Call sites keep the plain
# `with YoutubeDL(opts) as ydl:` syntax unchanged.
_YoutubeDL_class = None

def YoutubeDL(*args, **kwargs):
    global _YoutubeDL_class
    if _YoutubeDL_class is None:
        from yt_dlp import YoutubeDL as _Y
        _YoutubeDL_class = _Y
    return _YoutubeDL_class(*args, **kwargs)


def _resolve_ffmpeg_location():
    """Locate ffmpeg for yt-dlp.

    When running as a PyInstaller bundle ffmpeg(.exe) is in sys._MEIPASS.
    In Mac dev mode, look under assets/mac/.  Windows dev falls through to
    PATH (yt-dlp handles that).
    """
    _exe = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
    if hasattr(sys, '_MEIPASS'):
        bundled = os.path.join(sys._MEIPASS, _exe)
        if os.path.exists(bundled):
            return sys._MEIPASS  # yt-dlp wants the directory, not the exe
    if sys.platform == 'darwin':
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _mac_dir = os.path.join(_root, 'assets', 'mac')
        if os.path.isfile(os.path.join(_mac_dir, 'ffmpeg')):
            return _mac_dir
    return None  # dev mode — let yt-dlp use PATH


class _MusicDownloadCancelled(Exception):
    """Raised inside the music download progress hook when the user cancels a
    queue item mid-download.  Caught by _music_download_worker so it can mark
    the entry as 'cancelled' and clean up the partial file."""
    pass


def _richness(video):
    """Score how 'rich' a library entry is — more metadata = higher score.
    Used to pick the better of two duplicate entries when deduping."""
    score = 0
    for key in ('url', 'thumbnail', 'uploader', 'duration_string', 'filepath'):
        if video.get(key):
            score += 1
    if video.get('formats'):
        score += 1
    return score
