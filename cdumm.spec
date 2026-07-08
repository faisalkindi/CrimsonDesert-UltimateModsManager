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

# Vendored crimson_rs Rust extension (NattKh, MPL-2.0).
# Lives at src/cdumm/_vendor/crimson_rs/ and is loaded lazily by
# cdumm.engine.crimson_rs_loader at runtime. PyInstaller needs the
# .pyd binary AND the Python wrapper files bundled in their original
# layout so `sys.path.insert(_vendor)` + `import crimson_rs` works
# inside the frozen exe.
_crimson_rs_dir = os.path.join('src', 'cdumm', '_vendor', 'crimson_rs')
_crimson_rs_binaries = []
_crimson_rs_datas = []
if os.path.isdir(_crimson_rs_dir):
    for f in os.listdir(_crimson_rs_dir):
        full = os.path.join(_crimson_rs_dir, f)
        if not os.path.isfile(full):
            continue
        if f.endswith('.pyd') or f.endswith('.so') or f.endswith('.dll'):
            _crimson_rs_binaries.append((full, 'cdumm/_vendor/crimson_rs'))
        else:
            _crimson_rs_datas.append((full, 'cdumm/_vendor/crimson_rs'))

# Vendored skillinfo_parser.py (MPL-2.0). Pure Python, just bundle
# the .py file plus its license alongside crimson_rs. Loaded lazily
# by cdumm.engine.skill_writer.
_skill_parser_py = os.path.join('src', 'cdumm', '_vendor',
                                'skillinfo_parser.py')
if os.path.isfile(_skill_parser_py):
    _crimson_rs_datas.append((_skill_parser_py, 'cdumm/_vendor'))
_skill_parser_lic = os.path.join('src', 'cdumm', '_vendor',
                                 'skillinfo_parser_LICENSE_MPL2')
if os.path.isfile(_skill_parser_lic):
    _crimson_rs_datas.append((_skill_parser_lic, 'cdumm/_vendor'))

# qfluentwidgets resources (icons, stylesheets, compiled Qt resources)
_qfw_datas = collect_data_files('qfluentwidgets')

# qframelesswindow — collect everything (binaries, datas, hidden imports)
_qflw_datas, _qflw_binaries, _qflw_hiddenimports = collect_all('qframelesswindow')

# certifi cacert.pem — bundled so cdumm.engine.ssl_ctx can build a fresh
# SSL context at runtime instead of trusting Python's frozen CA store
# (GitHub #175 / #178 / #179: CERTIFICATE_VERIFY_FAILED).
_certifi_datas = collect_data_files('certifi')


a = Analysis(
    ['src/cdumm/main.py'],
    pathex=['src'],
    binaries=_xxhash_binaries + _native_binaries + _crimson_rs_binaries + _qflw_binaries,
    datas=[('cdumm.ico', '.'), ('asi_loader/winmm.dll', 'asi_loader'),
           ('src/cdumm/translations', 'cdumm/translations'),
           ('schemas/pabgb_complete_schema.json', 'schemas'),
           ('schemas/pabgb_type_overrides.json', 'schemas'),
           ('schemas/NOTICE', 'schemas'),
           ('field_schema/README.md', 'field_schema'),
           ('field_schema/skill.json', 'field_schema'),
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
           ] + _crimson_rs_datas + _qfw_datas + _qflw_datas + _certifi_datas,
    hiddenimports=[
        'cdumm.cli',
        'cdumm.worker_process',
        'cdumm.gui.setup_dialog',
        'cdumm.gui.conflict_view',
        'cdumm.gui.conflicts_dialog',
        'cdumm.gui.bug_report',
        # v3 Fluent UI
        'cdumm.gui.fluent_window',
        'cdumm.gui.pages.mods_page',
        'cdumm.gui.pages.asi_page',
        'cdumm.gui.pages.activity_page',
        'cdumm.gui.pages.about_page',
        'cdumm.gui.pages.settings_page',
        'cdumm.gui.pages.tool_page',
        'cdumm.gui.components.mod_card',
        'cdumm.gui.components.summary_bar',
        'cdumm.gui.components.config_panel',
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
        # PIL DDS texture decode for the Game Data preview — name the core
        # codec + DDS plugin so the onefile build actually bundles them
        # (Pillow registers plugins lazily, which PyInstaller can miss).
        'PIL', 'PIL.Image', 'PIL.ImageFile', 'PIL.DdsImagePlugin',
        # Qt3D for the Game Data texture 3D preview (sphere/cube). The
        # PySide6 hook bundles the Qt3D DLLs + plugins once these are imported
        # (and not excluded); QtOpenGL is what Qt3DRender renders through.
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DExtras',
        'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation',
        'PySide6.QtOpenGL',
        'cdumm.archive.pathc_handler',
        'cdumm.engine.mod_health_check',
        'cdumm.asi.asi_manager',
        'cdumm.storage.database',
        'cdumm.storage.config',
        'cdumm.storage.game_finder',
        'cdumm.gui.splash',
        'cdumm.gui.profile_dialog',
        'cdumm.gui.welcome_wizard',
        'cdumm.engine.update_checker',
        'cdumm.engine.version_detector',
        'cdumm.engine.profile_manager',
        'cdumm.engine.mod_list_io',
        'cdumm.gui.update_overlay',
        'cdumm.gui.changelog',
        'cdumm.gui.preset_picker',
        'cdumm.engine.activity_log',
        'cdumm.engine.binary_search',
        'cdumm.engine.nexus_api',
        'cdumm.engine.ssl_ctx',
        'cdumm.engine.game_monitor',
        'certifi',
        # GitHub #63: launch-game CLI subcommand. Lazy-imported in
        # cli.cmd_launch_game so static analysis may miss it; list
        # defensively the same way the others are.
        'cdumm.engine.launcher',
        'cdumm.gui.binary_search_dialog',
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
        # Faisal 2026-05-12 GitHub #113 (hhkbble): Nexus SSO login uses
        # the websocket-client package (imported as ``websocket``) inside
        # cdumm.engine.nexus_sso. PyInstaller's static analyser missed it
        # because the import is wrapped in a try/except and only fires on
        # the SSO code path, not at app startup. Add it explicitly so the
        # frozen exe ships the package and the SSO flow does not fail
        # with "The 'websocket-client' Python package is required".
        'websocket',
        'websocket._app',
        'websocket._core',
        'websocket._exceptions',
        'websocket._handshake',
        'websocket._http',
        'websocket._socket',
        'websocket._url',
        'websocket._utils',
    ] + _qflw_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PySide6 modules not used by CDUMM (only QtCore/QtGui/QtWidgets/QtSvg needed)
        'PySide6.QtWebEngine', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore',
        # Qt3D IS used — the Game Data texture 3D preview (sphere/cube). Do NOT
        # exclude Qt3DCore/Render/Input/Extras or the preview reports
        # "3D preview unavailable: No module named 'PySide6.Qt3DExtras'".
        'PySide6.QtCharts', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
        'PySide6.QtQuick', 'PySide6.QtQml', 'PySide6.QtBluetooth',
        'PySide6.QtPositioning', 'PySide6.QtSensors', 'PySide6.QtSerialPort',
        'PySide6.QtRemoteObjects', 'PySide6.QtNfc',
        # QtOpenGL kept (Qt3DRender renders through it); OpenGLWidgets unused.
        'PySide6.QtOpenGLWidgets',
        'PySide6.QtNetwork',
        'PySide6.QtDataVisualization', 'PySide6.QtGraphs',
        'PySide6.QtAxContainer', 'PySide6.QtDesigner',
        'PySide6.QtHelp', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'PySide6.QtQuick3D', 'PySide6.QtShaderTools',
        'PySide6.QtSpatialAudio', 'PySide6.QtHttpServer',
        'PySide6.QtTest', 'PySide6.QtDBus', 'PySide6.QtConcurrent',
        # scipy/numpy — only needed for acrylic blur (disabled)
        'scipy', 'numpy', 'numpy.core', 'numpy.linalg',
        # PIL/Pillow IS used — it decodes DDS textures for the Game Data
        # preview, so it must be bundled (do NOT exclude it). Only colorthief
        # (which merely pulls PIL in transitively) is unused.
        'colorthief',
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
    # opengl32sw.dll kept — Qt3D's texture 3D preview software-renders
    # through it when the host has no usable GPU OpenGL driver.
    # Qt6Network.dll kept — Qt63DCore.dll links it, so stripping it broke the
    # 3D preview with "DLL load failed while importing Qt3DExtras".
    'Qt6Pdf.dll',            # ~4 MB
    'Qt6Designer.dll',       # ~5 MB
    'Qt6Quick.dll',          # ~6 MB
    'Qt6Qml.dll',            # ~5 MB
    'Qt6ShaderTools.dll',    # ~4 MB
    'Qt6Quick3DRuntimeRender.dll',
    # Qt6OpenGL.dll kept — Qt3DRender (texture 3D preview) renders through it.
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
# Also filter out brotli/cryptography binary extensions. Keep PIL's core
# '_imaging' (DDS/BCn texture decode); only the AVIF/WebP/CMS codecs — which
# CDUMM's DDS preview never touches — are dropped.
_binary_name_excludes = {
    '_avif', '_webp', '_imagingcms', '_brotli',
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
    # GNU strip on the windows-latest CI runner corrupts python313.dll (the exe
    # then fails at launch: "Failed to load Python DLL ... Invalid access to
    # memory location"). Strip only on posix, never on Windows.
    strip=(os.name != 'nt'),
    # UPX disabled — heuristic AV engines (Bkav, CrowdStrike Falcon,
    # DeepInstinct, Fortinet) flag UPX-packed PyInstaller binaries
    # because real malware uses UPX too. Defender is fine either way,
    # but enterprise scanners aren't. Trade-off: exe grows ~50% (48 MB
    # to ~70 MB) but the AV-clean profile is worth it for users.
    upx=False,
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
