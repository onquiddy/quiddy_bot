from pathlib import Path


def test_core_api_version_present():
    ns = {}
    exec(Path('quiddy/__init__.py').read_text('utf-8'), ns)
    assert ns['CORE_API_VERSION'] == '1.0'
    assert ns['__version__'] == '1.6.3'


def test_plugin_import_contract():
    text = Path('quiddy/core/plugin.py').read_text('utf-8')
    assert 'from quiddy import CORE_API_VERSION' in text
