from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("bitsbots")

a = Analysis(
    ["bundle_entry.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BitsBots",
    icon="../bitsbots.ico",
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="BitsBots",
)
