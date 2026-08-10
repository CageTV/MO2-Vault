# Build with: pyinstaller build_exe.spec
#
# Produces a onedir build (dist/mo2-modlist-vault/mo2-modlist-vault.exe plus
# its dependencies) rather than onefile - matches the ESLifier.exe
# distribution shape the MO2 plugin project models itself on, and avoids
# onefile's self-extraction-on-every-launch cost, which matters for the
# headless `vault-snapshot` invocations the MO2 plugin makes.

# py7zr dispatches to these compression/crypto codec modules dynamically
# depending on what a given .7z archive actually uses - PyInstaller's static
# import scan can miss them, so they're listed explicitly. Note the PyPI
# package name isn't always the importable module name (verified by
# grepping py7zr's own source): pybcj's module is "bcj", pycryptodomex's is
# "Cryptodome".
PY7ZR_HIDDEN_IMPORTS = [
    "brotli", "inflate64", "multivolumefile", "psutil",
    "bcj", "Cryptodome", "pyppmd", "texttable",
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("modlist_vault/gui/assets", "modlist_vault/gui/assets"),
    ],
    hiddenimports=PY7ZR_HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mo2-modlist-vault",
    debug=False,
    strip=False,
    upx=False,
    # True, not False: this exe is dual-mode (GUI when double-clicked, CLI
    # when given arguments - see launcher.py). console=False would leave
    # sys.stdout/stderr unusable for the CLI path (no console attached,
    # print()/logging silently fail) - the MO2 plugin's subprocess capture
    # and any terminal use of `vault-snapshot`/etc. both need a real console.
    # Trade-off: double-clicking for the GUI briefly shows a console window.
    console=True,
    icon="modlist_vault/gui/assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="mo2-modlist-vault",
)
