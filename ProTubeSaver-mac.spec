# -*- mode: python ; coding: utf-8 -*-
# macOS PyInstaller spec — parallel to ProTubeSaver.spec.
# Differences: assets/mac/ binaries, .icns icon, BUNDLE step.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

ytdlp_hidden = collect_submodules('yt_dlp')
ytdlp_data = collect_data_files('yt_dlp')

a = Analysis(
    ['src/main.py'],
    pathex=['src'],
    binaries=[
        ('assets/mac/ffmpeg', '.'),
        ('assets/mac/ffprobe', '.'),
    ],
    datas=[
        ('src/index.html', '.'),
        ('assets/icon.icns', '.'),
        ('src/logic.py', '.'),
        ('src/updater.py', '.'),
        ('src/app_paths.py', '.'),
        ('src/groq_client.py', '.'),
        ('assets/mac/protube_update_helper.sh', '.'),
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
        *ytdlp_hidden,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ProTube Saver',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ProTube Saver',
)

app = BUNDLE(
    coll,
    name='ProTube Saver.app',
    icon='assets/icon.icns',
    bundle_identifier='com.cincade.protubesaver',
    info_plist={
        'CFBundleShortVersionString': '1.4.5',
        'CFBundleVersion': '1.4.5',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
        'NSHumanReadableCopyright': 'Copyright (c) Cincade',
    },
)
