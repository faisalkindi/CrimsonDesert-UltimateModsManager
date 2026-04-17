# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Crimson Desert Ultimate Mods Manager

import importlib.util
import os

from PyInstaller.utils.hooks import collect_data_files, collect_all

_xxhash_spec = importlib.util.find_spec('xxhash._xxhash')
_xxhash_binaries = [(_xxhash_spec.origin, 'xxhash')] if _xxhash_spec else []

# cdumm_native Rust extension (.pyd)
_native_spec = importlib.util.find_spec('cdumm_native')
_native_binaries = []
if _native_spec and _native_spec.submodule_search_locations and len(_native_spec.submodule_search_locations) > 0:
    _native_dir = _native_spec.submodule_search_locations[0]
    for f in os.listdir(_native_dir):
        if f.endswith('.pyd') or f.endswith('.so'):
            _native_binaries.append((os.path.join(_native_dir, f), 'cdumm_native'))
elif _native_spec and _native_spec.origin:
    _native_binaries.append((_native_spec.origin, '.'))

# qfluentwidgets resources (icons, stylesheets, compiled Qt resources)
_qfw_datas = collect_data_files('qfluentwidgets')

# qframelesswindow — collect everything (binaries, datas, hidden imports)
_qflw_datas, _qflw_binaries, _qflw_hiddenimports = collect_all('qframelesswindow')


a = Analysis(
    ['src/cdumm/main.py'],
    pathex=['src'],
    binaries=_xxhash_binaries + _native_binaries + _qflw_binaries,
    datas=[('cdumm.ico', '.'), ('asi_loader/winmm.dll', 'asi_loader'),
           ('src/cdumm/translations', 'cdumm/translations'),
           ('schemas/pabgb_complete_schema.json', 'schemas'),
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
        'cdumm.worker_process',
        'cdumm.gui.main_window',
        'cdumm.gui.setup_dialog',
        'cdumm.gui.import_widget',
        'cdumm.gui.conflict_view',
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

# Strip large unused DLLs from binaries
_dll_excludes = {
    'opengl32sw.dll',        # ~20 MB software OpenGL (not needed)
    'Qt6Network.dll',        # ~3 MB
    'Qt6Pdf.dll',            # ~4 MB
    'Qt6Designer.dll',       # ~5 MB
    'Qt6Quick.dll',          # ~6 MB
    'Qt6Qml.dll',            # ~5 MB
    'Qt6ShaderTools.dll',    # ~4 MB
    'Qt6Quick3DRuntimeRender.dll',
    'Qt6OpenGL.dll',         # ~1.9 MB (not used — no OpenGL rendering)
    'Qt6QmlModels.dll',      # ~0.95 MB
    'Qt6QmlMeta.dll',        # ~0.15 MB
    'Qt6QmlWorkerScript.dll',  # ~0.08 MB
    'Qt6VirtualKeyboard.dll',  # ~0.4 MB (desktop app, no touch keyboard)
    'qdirect2d.dll',         # ~1 MB (qwindows.dll is sufficient)
    'avcodec-61.dll',        # ~13 MB multimedia codec
    'avformat-61.dll',
    'avutil-59.dll',
    'swresample-5.dll',
    'swscale-8.dll',
    # libcrypto + libssl kept for HTTPS (NexusMods API, update checker)
    # Image format plugins CDUMM doesn't use (keep qico, qsvg, qgif)
    'qtiff.dll',             # ~0.43 MB
    'qwebp.dll',             # ~0.55 MB
    'qjpeg.dll',             # ~0.56 MB
    'qpdf.dll',              # ~0.04 MB
    'qicns.dll',             # ~0.05 MB (Apple icon format)
    'qtga.dll',              # ~0.04 MB
    'qwbmp.dll',             # ~0.04 MB
}
# Also filter out PIL/brotli/cryptography binary extensions
_binary_name_excludes = {
    '_avif', '_imaging', '_webp', '_imagingcms', '_brotli',
    # '_rust' kept — cryptography uses it for AES-GCM in privatebin uploads
    '_ec_ws',      # Cryptodome elliptic curve (not used by CDUMM)
    '_ed448',      # Cryptodome Ed448
    '_curve448',   # Cryptodome Curve448
}

def _should_exclude_bin(name):
    basename = name.split('/')[-1].split('\\')[-1]
    if basename in _dll_excludes:
        return True
    stem = basename.rsplit('.', 1)[0]
    for excl in _binary_name_excludes:
        if stem.startswith(excl):
            return True
    return False

a.binaries = [b for b in a.binaries if not _should_exclude_bin(b[0])]

# Strip Qt translations for languages CDUMM doesn't support (save ~4.4 MB)
_keep_langs = {'en', 'de', 'es', 'fr', 'ko', 'pt', 'zh', 'ar', 'it', 'pl', 'ru', 'tr', 'ja', 'uk', 'id'}
def _should_keep_data(name):
    if name.endswith('.qm') and 'translations' in name:
        import os
        basename = os.path.basename(name).replace('.qm', '')
        parts = basename.split('_')
        lang = parts[-1] if len(parts) > 1 else ''
        return lang in _keep_langs
    return True

a.datas = [d for d in a.datas if _should_keep_data(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CDUMM',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='cdumm.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
