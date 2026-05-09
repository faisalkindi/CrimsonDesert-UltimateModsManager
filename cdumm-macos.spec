# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the macOS build of Crimson Desert Ultimate Mods
# Manager. Parallel to ``cdumm.spec`` (Windows) — the Windows spec stays
# untouched. Differences from the Windows build:
#
#   * Drops ``cdumm.ico`` (Windows tray icon, replaced by ``cdumm.icns``).
#   * Drops the ASI loader payload (``asi_loader/winmm.dll``) — ASI is
#     a Win32-only mod format and the page is hidden from the macOS
#     navigation at runtime.
#   * Drops the vendored ``crimson_rs`` Windows ``.pyd`` and the
#     NattKh skill-info parser sidecar — the loader at
#     ``engine/crimson_rs_loader.py`` returns ``None`` gracefully when
#     the binary fails to import, so iteminfo / skill list-of-dict
#     writers become unavailable on macOS but everything else works.
#   * Includes ``cdumm_native`` as a ``.so`` (Apple Silicon ARM64).
#     Built separately by ``scripts/build-macos.sh`` via maturin.
#   * Wraps the EXE in a ``BUNDLE(...)`` so PyInstaller emits a
#     ``CDUMM.app`` directory rather than a bare binary.
#   * Pins ``target_arch='arm64'`` because Crimson Desert macOS is
#     Apple-Silicon-only and shipping a universal2 binary would just
#     bloat the download for users who can't run the game anyway.
#
# The icon (`cdumm.icns`) is generated at build time by
# ``scripts/build-macos.sh`` from ``assets/cdumm-logo.png`` (1024x1024).
# The script must run before this spec.

import importlib.util
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files

# Read CDUMM's version so the same string lands in CFBundleVersion +
# CFBundleShortVersionString. Keeps the `.app`'s "About this Mac" /
# Spotlight metadata in sync with the in-app About page.
_src_root = os.path.join(os.path.abspath(os.path.dirname(SPEC)), 'src')
sys.path.insert(0, _src_root)
from cdumm import __version__ as VERSION  # noqa: E402

# cdumm_native Rust extension. On macOS the artifact is a ``.so``
# (despite the name — Python uses .so on POSIX regardless of dlopen
# semantics). maturin build --release puts it at
# native/target/wheels/, but `pip install` of that wheel into the
# build environment lands the .so under
# site-packages/cdumm_native/, which find_spec resolves below.
_native_spec = importlib.util.find_spec('cdumm_native')
_native_binaries = []
if _native_spec and _native_spec.submodule_search_locations:
    _native_dir = _native_spec.submodule_search_locations[0]
    for f in os.listdir(_native_dir):
        if f.endswith('.so'):
            _native_binaries.append(
                (os.path.join(_native_dir, f), 'cdumm_native'))
elif _native_spec and _native_spec.origin:
    _native_binaries.append((_native_spec.origin, '.'))

# qfluentwidgets resources (icons, stylesheets, compiled Qt resources)
_qfw_datas = collect_data_files('qfluentwidgets')

# qframelesswindow — collect everything (binaries, datas, hidden imports)
_qflw_datas, _qflw_binaries, _qflw_hiddenimports = collect_all(
    'qframelesswindow')


a = Analysis(
    ['src/cdumm/main.py'],
    pathex=['src'],
    binaries=_native_binaries + _qflw_binaries,
    datas=[
        ('src/cdumm/translations', 'cdumm/translations'),
        ('schemas/pabgb_complete_schema.json', 'schemas'),
        ('schemas/pabgb_type_overrides.json', 'schemas'),
        ('field_schema/README.md', 'field_schema'),
        ('assets/fonts/Oxanium-VariableFont_wght.ttf', 'assets/fonts'),
        ('assets/cdumm-logo.png', 'assets'),
        ('assets/cdumm-logo-light.png', 'assets'),
        ('assets/cdumm-logo-dark.png', 'assets'),
        ('assets/store-steam.svg', 'assets'),
        ('assets/store-xbox.svg', 'assets'),
        ('assets/store-epic.svg', 'assets'),
        ('assets/store-steam-white.svg', 'assets'),
        ('assets/store-xbox-white.svg', 'assets'),
        ('assets/store-epic-white.svg', 'assets'),
    ] + _qfw_datas + _qflw_datas,
    hiddenimports=[
        'cdumm.cli',
        'cdumm.platform',
        'cdumm.worker_process',
        'cdumm.gui.main_window',
        'cdumm.gui.setup_dialog',
        'cdumm.gui.import_widget',
        'cdumm.gui.conflict_view',
        'cdumm.gui.conflicts_dialog',
        'cdumm.gui.mod_list_model',
        'cdumm.gui.asi_panel',
        'cdumm.gui.test_mod_dialog',
        'cdumm.gui.workers',
        'cdumm.gui.bug_report',
        # v3 Fluent UI
        'cdumm.gui.fluent_window',
        'cdumm.gui.pages.mods_page',
        'cdumm.gui.pages.asi_page',
        'cdumm.gui.pages.activity_page',
        'cdumm.gui.pages.about_page',
        'cdumm.gui.pages.settings_page',
        'cdumm.gui.pages.tools_page',
        'cdumm.gui.pages.tool_page',
        'cdumm.gui.components.mod_card',
        'cdumm.gui.components.summary_bar',
        'cdumm.gui.components.config_panel',
        'cdumm.gui.components.conflict_card',
        'cdumm.gui.components.drop_overlay',
        'cdumm.gui.import_context',
        'cdumm.gui.recovery_flow',
        'cdumm.engine.recovery_candidates',
        'cdumm.engine.compiled_merge',
        'cdumm.engine.mod_dedup',
        # PySide6-Fluent-Widgets + dependencies
        'qfluentwidgets',
        'qfluentwidgets._rc',
        'qfluentwidgets._rc.resource',
        'qfluentwidgets.common',
        'qfluentwidgets.components',
        'qfluentwidgets.window',
        'qframelesswindow',
        'qframelesswindow._rc',
        'qframelesswindow._rc.resource',
        'darkdetect',
        'xxhash', 'xxhash._xxhash',
        'cdumm_native',
        'cdumm.engine.snapshot_manager',
        'cdumm.engine.delta_engine',
        'cdumm.engine.import_handler',
        'cdumm.engine.conflict_detector',
        'cdumm.engine.apply_engine',
        'cdumm.engine.mod_manager',
        'cdumm.engine.test_mod_checker',
        'cdumm.archive.transactional_io',
        'cdumm.archive.hashlittle',
        'cdumm.archive.papgt_manager',
        'cdumm.archive.format_parsers.base',
        'cdumm.archive.format_parsers.pabgb_parser',
        'cdumm.archive.format_parsers.paac_parser',
        'cdumm.archive.format_parsers.pamt_parser',
        'cdumm.archive.format_parsers.characterinfo_full_parser',
        'cdumm.semantic',
        'cdumm.semantic.changeset',
        'cdumm.semantic.parser',
        'cdumm.semantic.differ',
        'cdumm.semantic.merger',
        'cdumm.semantic.engine',
        'cdumm.engine.offset_collision',
        'cdumm.archive.paz_parse',
        'cdumm.archive.paz_crypto',
        'cdumm.archive.paz_repack',
        'cdumm.engine.crimson_browser_handler',
        'cdumm.engine.json_patch_handler',
        'cdumm.engine.xml_patch_handler',
        'cdumm.engine.variant_handler',
        'cdumm.engine.language',
        'cdumm.engine.compiled_merge',
        'cdumm.engine.texture_mod_handler',
        'cdumm.archive.pathc_handler',
        'cdumm.engine.mod_health_check',
        'cdumm.gui.health_check_dialog',
        # Imported by the GUI even though the page is hidden on macOS —
        # the AsiManager class is still referenced at module load time
        # via cdumm.gui.fluent_window. Excluding it crashes startup.
        'cdumm.asi.asi_manager',
        'cdumm.storage.database',
        'cdumm.storage.config',
        'cdumm.storage.game_finder',
        'cdumm.gui.splash',
        'cdumm.gui.mod_contents_dialog',
        'cdumm.gui.profile_dialog',
        'cdumm.gui.welcome_wizard',
        'cdumm.engine.update_checker',
        'cdumm.engine.version_detector',
        'cdumm.engine.profile_manager',
        'cdumm.engine.mod_list_io',
        'cdumm.gui.update_overlay',
        'cdumm.gui.changelog',
        'cdumm.gui.preset_picker',
        'cdumm.gui.verify_dialog',
        'cdumm.gui.activity_panel',
        'cdumm.engine.activity_log',
        'cdumm.engine.binary_search',
        'cdumm.engine.nexus_api',
        'cdumm.engine.game_monitor',
        'cdumm.engine.launcher',
        'cdumm.gui.binary_search_dialog',
        'cdumm.gui.patch_toggle_dialog',
        'py7zr',
        # XML XPath patch support (JMM XmlPatchApplier parity)
        'lxml',
        'lxml.etree',
        # PrivateBin upload for bug reports
        'privatebin',
        'privatebin._core',
        'privatebin._crypto',
        'privatebin._enums',
        'privatebin._errors',
        'privatebin._models',
        'privatebin._utils',
        'privatebin._version',
        'privatebin._wrapper',
        'base58',
        'msgspec',
        'msgspec.json',
        'httpx',
        'cryptography.hazmat.primitives.ciphers.aead',
        'cryptography.hazmat.primitives.kdf.pbkdf2',
        'cryptography.hazmat.backends',
        # macOS uses psutil for the cross-platform _check_game_running
        # branch (the Windows build uses ctypes.windll.psapi instead).
        # Listing it here matches the Windows runtime dep — psutil is
        # already in pyproject.toml so PyInstaller picks it up
        # automatically, but the explicit hidden-import is defensive.
        'psutil',
    ] + _qflw_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PySide6 modules not used by CDUMM (only QtCore/QtGui/QtWidgets/QtSvg needed)
        'PySide6.QtWebEngine', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput', 'PySide6.Qt3DExtras',
        'PySide6.QtCharts', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
        'PySide6.QtQuick', 'PySide6.QtQml', 'PySide6.QtBluetooth',
        'PySide6.QtPositioning', 'PySide6.QtSensors', 'PySide6.QtSerialPort',
        'PySide6.QtRemoteObjects', 'PySide6.QtNfc',
        'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
        'PySide6.QtNetwork',
        'PySide6.QtDataVisualization', 'PySide6.QtGraphs',
        'PySide6.QtAxContainer', 'PySide6.QtDesigner',
        'PySide6.QtHelp', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'PySide6.QtQuick3D', 'PySide6.QtShaderTools',
        'PySide6.QtSpatialAudio', 'PySide6.QtHttpServer',
        'PySide6.QtTest', 'PySide6.QtDBus', 'PySide6.QtConcurrent',
        # scipy/numpy — only needed for acrylic blur (disabled)
        'scipy', 'numpy', 'numpy.core', 'numpy.linalg',
        # PIL/Pillow — not imported by CDUMM (colorthief dep, unused)
        'PIL', 'PIL._imaging', 'PIL._avif', 'PIL._webp', 'PIL.Image',
        'Pillow', 'colorthief',
        # brotli — not used by CDUMM (transitive dep from py7zr)
        'brotli', '_brotli', 'brotlicffi',
        # cryptography used by privatebin for AES-GCM + PBKDF2. Keep minimal subset.
        'cryptography.x509', 'cryptography.fernet',
        # setuptools/pkg_resources not needed at runtime
        'setuptools', 'pkg_resources',
    ],
    noarchive=False,
)

# Strip Qt translations for languages CDUMM doesn't support (~4.4 MB win on
# Windows; on macOS Qt translations live as ``.qm`` inside Frameworks/QtCore
# Resources but the same filter pattern catches them).
_keep_langs = {
    'en', 'de', 'es', 'fr', 'ko', 'pt', 'zh', 'ar', 'it', 'pl',
    'ru', 'tr', 'ja', 'uk', 'id',
}
def _should_keep_data(name):
    if name.endswith('.qm') and 'translations' in name:
        basename = os.path.basename(name).replace('.qm', '')
        parts = basename.split('_')
        lang = parts[-1] if len(parts) > 1 else ''
        return lang in _keep_langs
    return True
a.datas = [d for d in a.datas if _should_keep_data(d[0])]


pyz = PYZ(a.pure)

# ── Onedir mode + BUNDLE = .app the right way ─────────────────────
# The Windows spec is onefile (single CDUMM.exe). For macOS, onefile
# inside a .app is deprecated by PyInstaller (clashes with Gatekeeper
# notarisation requirements and will become an error in PyInstaller
# v7.0). Onedir is the canonical macOS pattern: PyInstaller emits a
# directory of files, BUNDLE wraps it in a .app. The end-user
# experience is unchanged — they still see a single ``CDUMM.app`` —
# but the internals match what Apple's tooling expects.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,    # ← onedir: COLLECT below gathers binaries/datas
    name='CDUMM',
    debug=False,
    bootloader_ignore_signals=False,
    # ``strip`` removes debug symbols from binaries; PyInstaller delegates
    # to /usr/bin/strip on macOS. Saves ~10-20 MB on the .app.
    strip=True,
    # UPX disabled — same heuristic-AV concern as Windows. macOS Gatekeeper
    # also flags UPX-packed binaries. Trade ~50% size for distribution
    # cleanliness.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='cdumm.icns',
    disable_windowed_traceback=False,
    argv_emulation=False,
    # ARM64 only. Crimson Desert macOS is Apple-Silicon-exclusive; an
    # Intel build would just bloat downloads for users who can't run
    # the game anyway. Setting target_arch is what makes maturin's
    # arm64 .so the only architecture in the .app — a universal build
    # would need both targets pre-built and ``target_arch='universal2'``.
    target_arch='arm64',
    codesign_identity=None,    # ad-hoc sign happens after PyInstaller
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=True,
    upx=False,
    upx_exclude=[],
    name='CDUMM',
)


# Wrap the COLLECT output in a macOS .app bundle. CFBundleIdentifier
# matches the repo owner's namespace (faisalkindi on GitHub). The
# Info.plist keys below are the minimum needed for a
# Gatekeeper-acceptable ad-hoc-signed .app on macOS 11+ — no
# NSDocumentClass, no URL schemes, no file-type associations yet
# (those come later when nxm:// support lands).
app = BUNDLE(
    coll,
    name='CDUMM.app',
    icon='cdumm.icns',
    bundle_identifier='com.faisalkindi.cdumm',
    version=VERSION,
    info_plist={
        'CFBundleName': 'CDUMM',
        'CFBundleDisplayName': 'Crimson Desert Ultimate Mods Manager',
        'CFBundleIdentifier': 'com.faisalkindi.cdumm',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'CFBundleExecutable': 'CDUMM',
        'CFBundleIconFile': 'cdumm.icns',
        # macOS 15 (Sequoia) is what Crimson Desert itself requires —
        # advertising any lower in our Info.plist would be a lie and
        # also wouldn't help, since the game can't run on older macOS.
        # Matches the Nexus page's "macOS 15 Sequoia or later" line.
        # The build script (scripts/build-macos.sh) post-processes
        # every bundled Mach-O with vtool to enforce this — without
        # that, setup-python on macos-26-arm64 ships a Python with
        # minos=26 which dyld refuses to load on Sequoia.
        'LSMinimumSystemVersion': '15.0',
        # Retina rendering — without this, Qt apps look fuzzy on HiDPI.
        'NSHighResolutionCapable': True,
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSPrincipalClass': 'NSApplication',
        # No NSDocumentTypes / CFBundleURLTypes yet — nxm:// URL handler
        # registration is on the macOS roadmap (see MACOS.md) but needs
        # the .app to exist (which it now does) before we can register
        # via LSSetDefaultHandlerForURLScheme.
    },
)
