# PyInstaller spec — one file for macOS (.app, onedir) and Windows (.exe folder).
#   pyinstaller --noconfirm Anonymizer.spec
import re
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH)
VERSION = re.search(r'__version__ = "(.+?)"', (ROOT / 'core' / 'version.py').read_text()).group(1)
MAC = sys.platform == 'darwin'

datas = [(str(ROOT / 'static'), 'static')]
binaries, hiddenimports = [], collect_submodules('core')
packages = ['spacy', 'thinc', 'ru_core_news_lg', 'pymorphy3', 'pymorphy3_dicts_ru',
            'webview', 'pymupdf', 'lxml', 'docx', 'openpyxl', 'PIL', 'pillow_heif']
for pkg in packages:
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d
    binaries += b
    hiddenimports += h
if MAC:
    hiddenimports += ['Vision', 'Quartz', 'Foundation', 'AppKit', 'WebKit', 'objc']

a = Analysis(
    [str(ROOT / 'app.py')],
    pathex=[str(ROOT)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=['torch', 'tensorflow', 'matplotlib', 'pytest', 'playwright', 'tkinter', 'IPython'],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = str(ROOT / 'assets' / ('Anonymizer.icns' if MAC else 'Anonymizer.ico'))
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Anonymizer', console=False,
          icon=icon, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name='Anonymizer')
if MAC:
    app = BUNDLE(
        coll,
        name='Anonymizer.app',
        icon=icon,
        bundle_identifier='local.anonymizer.app',
        info_plist={
            'CFBundleName': 'Anonymizer',
            'CFBundleDisplayName': 'Anonymizer',
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '12.0',
            'NSHumanReadableCopyright': 'Локальная обработка, данные не покидают компьютер',
        },
    )
