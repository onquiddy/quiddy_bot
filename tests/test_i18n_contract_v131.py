from pathlib import Path
import re

def parse(path):
    rows=[]
    for raw in path.read_text('utf-8').splitlines():
        line=raw.strip()
        if not line or line.startswith(('#','!')): continue
        sep='=' if '=' in line else (':' if ':' in line else None)
        if sep: rows.append(line.split(sep,1)[0].strip())
    return rows

def test_russian_only_translation_contract():
    bundles={p.stem:parse(p) for p in Path('translations').glob('*.properties')}
    assert set(bundles)=={'ru'}
    assert bundles['ru']
