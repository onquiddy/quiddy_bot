from pathlib import Path


def test_live_config_console_contract():
    console = Path('quiddy/core/console.py').read_text('utf-8')
    config = Path('quiddy/core/config.py').read_text('utf-8')
    plugins = Path('quiddy/core/plugin.py').read_text('utf-8')
    assert 'self.register("config"' in console
    assert 'config [show|get|validate|update]' in console
    assert 'def reload(self)' in config
    assert 'apply_config_update' in plugins
    assert 'tree.sync()' in console


def test_network_zip_extraction_is_guarded():
    src = Path('plugins/music/runtime.py').read_text('utf-8')
    assert '_safe_extract_zip' in src
    assert 'relative_to(base)' in src
    assert 'zf.extractall(self.deno_dir)' not in src
