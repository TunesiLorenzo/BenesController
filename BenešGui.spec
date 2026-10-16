# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['benes_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('switch_track_icon_154772.ico', '_rebuilder_assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['C:/Users/lorenzo/Desktop/Python/BenesController/rebuilder_tk_icon_hook.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BenešGui',
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
    icon=['switch_track_icon_154772.ico'],
)
