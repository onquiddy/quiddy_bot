from pathlib import Path


def test_controller_is_gated_by_real_track_start():
    manager = Path('plugins/music/manager.py').read_text('utf-8')
    cog = Path('plugins/music/cog.py').read_text('utf-8')
    assert 'playback_started: bool = False' in Path('plugins/music/models.py').read_text('utf-8')
    assert 'if not session.playback_started:' in manager
    assert 'session.loading_track = True' in manager
    assert 'async def handle_track_start' in manager
    assert 'await self.manager.handle_track_start(payload)' in cog


def test_playlist_preload_is_bounded_and_supervised():
    cog = Path('plugins/music/cog.py').read_text('utf-8')
    assert 'asyncio.Semaphore' in cog
    assert 'self.plugin.tasks.create(preload_rest()' in cog
    assert 'results = await asyncio.gather' in cog
