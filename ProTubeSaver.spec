# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# yt-dlp loads ~1800 extractor modules dynamically at runtime.
# collect_submodules walks the package and returns every importable name,
# which PyInstaller then treats as a forced import.
ytdlp_hidden = collect_submodules('yt_dlp')

# yt-dlp also ships some static data files (signature cache templates, etc.)
# that its code reads via importlib.resources — those aren't Python modules
# so collect_submodules doesn't pick them up.
ytdlp_data = collect_data_files('yt_dlp')

a = Analysis(
    ['src/main.py'],
    # Add src/ to module search path so `from logic import API`,
    # `from app_paths import ...`, and `from updater import ...` resolve
    # at analysis time post-reorg (sources moved out of project root into src/).
    pathex=['src'],
    binaries=[
        # ffmpeg + ffprobe live in assets/ alongside icon.ico (build-time
        # prereqs, all in one folder). If you get "FileNotFoundError" at
        # build time, drop fresh copies into assets/. Grab both from gyan.dev.
        ('assets/ffmpeg.exe', '.'),
        ('assets/ffprobe.exe', '.'),
    ],
    datas=[
        # Source paths point into src/ and assets/; destination '.' bundles
        # them at the bundle root so resource_path('index.html') (which joins
        # on sys._MEIPASS in frozen mode) keeps finding them.
        ('src/index.html', '.'),
        ('src/css/fonts.css', 'css'),
        ('src/css/base.css', 'css'),
        ('src/css/rail.css', 'css'),
        ('src/css/library.css', 'css'),
        ('src/css/player.css', 'css'),
        ('src/css/search.css', 'css'),
        ('src/css/music.css', 'css'),
        ('src/css/queue.css', 'css'),
        ('src/css/modals.css', 'css'),
        ('src/js/vendor/sortable.min.js', 'js/vendor'),
        ('src/js/app.js', 'js'),
        ('src/js/utils.js', 'js'),
        ('src/js/player.js', 'js'),
        ('src/js/selection.js', 'js'),
        ('src/js/settings.js', 'js'),
        ('src/js/music.js', 'js'),
        ('src/js/search.js', 'js'),
        ('assets/icon.ico', '.'),
        ('src/logic.py', '.'),
        ('src/updater.py', '.'),
        ('src/app_paths.py', '.'),
        ('src/groq_client.py', '.'),
        *ytdlp_data,
    ],
    hiddenimports=[
        'logic',
        'updater',
        'app_paths',
        'groq_client',
        'requests',
        'packaging',
        'packaging.version',
        'webview',
        # Include every yt-dlp submodule so dynamic imports at runtime
        # actually find the extractor they need.
        *ytdlp_hidden,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ProTube Saver',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)
