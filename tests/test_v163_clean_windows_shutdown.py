from pathlib import Path


def test_lavalink_does_not_use_asyncio_stdout_pipe():
    src = Path("plugins/music/runtime.py").read_text("utf-8")
    start = src.index("class EmbeddedLavalinkRuntime:")
    section = src[start:]
    spawn = section[section.index("async def _spawn"):section.index("async def _pump_stdout")]
    assert "stdout=self._log_file" in spawn
    assert "stdout=asyncio.subprocess.PIPE" not in spawn
    assert 'lavalink-runtime.log' in spawn


def test_no_leftover_ukrainian_user_error():
    src = Path("quiddy/core/bot.py").read_text("utf-8")
    assert "помилкою" not in src
    assert "Інцидент" not in src
