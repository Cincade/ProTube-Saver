"""Measure each stage of ProTube Saver's cold start in dev mode.

Run from project root: `python scripts/measure_startup.py`

Reports the wall-clock cost of every import + API() init step that runs
before the window appears. Doesn't open a window — purely measures the
backend startup cost. Numbers from the bundled exe will be HIGHER than
what this script reports because PyInstaller onefile mode also unpacks
~210MB to %TEMP% before any Python runs.
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

stages = []
def mark(label):
    stages.append((label, time.perf_counter()))

mark('process_start')

# Stage 1: app_paths + migration check
from app_paths import ytdlp_runtime_dir, migrate_legacy
mark('after_app_paths_import')

migrate_legacy()
mark('after_migrate_legacy')

# Stage 2: yt-dlp updater bootstrap (sys.path manipulation only — fast)
from updater import YtDlpUpdater
YtDlpUpdater.bootstrap_sys_path(ytdlp_runtime_dir())
mark('after_updater_bootstrap')

# Stage 3: webview import (pulls in pythonnet → CLR → System.Windows.Forms)
import webview
mark('after_webview_import')

# Stage 4: logic import (pulls in yt_dlp — ~1800 extractor module references)
from logic import API
mark('after_logic_import')

# Stage 5: API() __init__ — loads settings.json, starts video server, kicks
# yt-dlp updater check (silent, in background)
api = API()
mark('after_API_init')

# Pretty-print the deltas
print()
print(f'{"Stage":<32} {"Δ (ms)":>10} {"cumulative (ms)":>18}')
print('-' * 64)
t0 = stages[0][1]
prev = t0
for label, t in stages[1:]:
    dt = (t - prev) * 1000
    cum = (t - t0) * 1000
    print(f'{label:<32} {dt:>10.1f} {cum:>18.1f}')
    prev = t
print('-' * 64)
total_ms = (stages[-1][1] - t0) * 1000
print(f'TOTAL backend startup:           {total_ms:.1f} ms')
print()
print('Note: bundled exe adds PyInstaller onefile unpack overhead on top of this')
print('(typically 2-6 seconds the FIRST launch after a build, faster on repeat')
print('launches because the unpack dir gets cached in %TEMP%).')
