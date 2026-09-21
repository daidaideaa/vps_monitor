"""Rebuild bundled UI fonts. Dev dependency: pip install fonttools brotli."""
from pathlib import Path
import hashlib
import json
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
# Optional local build dependencies; never needed by the website or VPS monitor.
sys.path.insert(0, str(ROOT / '.local/font-build-deps'))
from fontTools import subset
from fontTools.ttLib import TTFont

OUT = ROOT / 'assets/fonts'
CACHE = ROOT / '.local/font-sources'
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)
SOURCES = {
    'inter.woff2': 'https://rsms.me/inter/font-files/InterVariable.woff2?v=4.1',
    'noto-sc.ttf': 'https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf',
    'Inter-OFL.txt': 'https://raw.githubusercontent.com/rsms/inter/v4.1/LICENSE.txt',
    'NotoSansSC-OFL.txt': 'https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/OFL.txt',
    'source-serif.ttf': 'https://raw.githubusercontent.com/google/fonts/main/ofl/sourceserif4/SourceSerif4%5Bopsz,wght%5D.ttf',
    'noto-serif-sc.ttf': 'https://raw.githubusercontent.com/google/fonts/main/ofl/notoserifsc/NotoSerifSC%5Bwght%5D.ttf',
    'SourceSerif4-OFL.txt': 'https://raw.githubusercontent.com/google/fonts/main/ofl/sourceserif4/OFL.txt',
    'NotoSerifSC-OFL.txt': 'https://raw.githubusercontent.com/google/fonts/main/ofl/notoserifsc/OFL.txt',
}
for name, url in SOURCES.items():
    destination = CACHE / name
    if not destination.exists():
        with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=60) as response:
            destination.write_bytes(response.read())

# Keep every non-Latin glyph used in the interface and published explanations.
# Other Chinese characters can still use the system fallback if new API text arrives.
texts = [ROOT / 'index.html', *sorted((ROOT / 'server').glob('*.py'))]
characters = {ord(c) for p in texts for c in p.read_text(encoding='utf-8') if ord(c) > 0x24F}

def write_subset(source, name, unicodes):
    font = TTFont(CACHE / source)
    font.recalcTimestamp = False
    options = subset.Options()
    options.layout_features = ['*']
    cutter = subset.Subsetter(options=options)
    cutter.populate(unicodes=unicodes)
    cutter.subset(font)
    font.flavor = 'woff2'
    font.save(OUT / name)
    return (OUT / name).stat().st_size

latin = set(range(0x20, 0x250)) | set(range(0x2000, 0x2070)) | {0x20AC, 0x2197, 0x2212}
sizes = {
    'inter-latin.woff2': write_subset('inter.woff2', 'inter-latin.woff2', latin),
    'noto-sans-sc-ui.woff2': write_subset('noto-sc.ttf', 'noto-sans-sc-ui.woff2', characters),
    'source-serif-latin.woff2': write_subset('source-serif.ttf', 'source-serif-latin.woff2', set(range(0x20, 0x7F))),
    'noto-serif-sc-title.woff2': write_subset('noto-serif-sc.ttf', 'noto-serif-sc-title.woff2', {ord(c) for c in '东京库存观察'}),
}
for name in ['Inter-OFL.txt', 'NotoSansSC-OFL.txt', 'SourceSerif4-OFL.txt', 'NotoSerifSC-OFL.txt']:
    (OUT / name).write_bytes((CACHE / name).read_bytes())
(OUT / 'sources.json').write_text(json.dumps({name: {'url': url, 'sha256': hashlib.sha256((CACHE/name).read_bytes()).hexdigest()} for name,url in SOURCES.items()}, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'font_bytes': sizes, 'cjk_codepoints': len(characters)}))
