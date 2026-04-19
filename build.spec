# -*- mode: python ; coding: utf-8 -*-
# Build: pyinstaller build.spec
# Output: dist/ArtifactEraser.exe  (single file, requests UAC elevation on launch)

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('templates', 'templates'),
    ],
    hiddenimports=[
        'winreg',
        'ctypes',
        'ctypes.wintypes',
        'sqlite3',
        'Registry',
        'Registry.Registry',
        'olefile',
        'concurrent.futures',
        'concurrent.futures.thread',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'email', 'xml', 'pydoc'],
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
    name='ArtifactEraser',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    uac_admin=True,
    icon=None,
)
