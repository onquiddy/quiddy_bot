from pathlib import Path

import pytest

from importlib.util import module_from_spec, spec_from_file_location


def load_store():
    path = Path(__file__).parents[1] / "plugins" / "music" / "playlist_store.py"
    spec = spec_from_file_location("playlist_store_test", path)
    module = module_from_spec(spec)
    assert spec and spec.loader
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.PlaylistStore, module.SavedTrack


@pytest.mark.asyncio
async def test_playlist_scope_limits_and_persistence(tmp_path):
    PlaylistStore, SavedTrack = load_store()
    path = tmp_path / "playlists.json"
    store = PlaylistStore(path, max_playlists=2, max_tracks=2)
    await store.load()
    await store.create(1, 10, "Road")
    await store.create(1, 10, "Night")
    with pytest.raises(OverflowError):
        await store.create(1, 10, "Third")
    # Same user in another guild gets a separate allowance.
    await store.create(2, 10, "Third")
    await store.add_track(1, 10, "Road", SavedTrack("https://youtu.be/a", "A", "Artist"))
    await store.add_track(1, 10, "Road", SavedTrack("https://youtu.be/b", "B", "Artist"))
    with pytest.raises(OverflowError):
        await store.add_track(1, 10, "Road", SavedTrack("https://youtu.be/c", "C", "Artist"))

    reloaded = PlaylistStore(path, max_playlists=2, max_tracks=2)
    await reloaded.load()
    item = await reloaded.get(1, 10, "road")
    assert item is not None
    assert [track["title"] for track in item["tracks"]] == ["A", "B"]


@pytest.mark.asyncio
async def test_playlist_atomic_mutations(tmp_path):
    PlaylistStore, SavedTrack = load_store()
    store = PlaylistStore(tmp_path / "playlists.json")
    await store.load()
    await store.create(7, 9, "Mix")
    await store.add_track(7, 9, "Mix", SavedTrack("q", "One", "Artist"))
    await store.rename(7, 9, "Mix", "Mix 2")
    removed = await store.remove_track(7, 9, "Mix 2", 1)
    assert removed["title"] == "One"
    assert await store.clear(7, 9, "Mix 2") == 0
    assert await store.delete(7, 9, "Mix 2") is True
    assert await store.get(7, 9, "Mix 2") is None

@pytest.mark.asyncio
async def test_bulk_add_dedupe_and_move(tmp_path):
    PlaylistStore, SavedTrack = load_store()
    store = PlaylistStore(tmp_path / "playlists.json", max_playlists=5, max_tracks=10)
    await store.load()
    await store.create(1, 2, "mix")
    a = SavedTrack(query="a", title="A", author="Artist", uri="https://x/a")
    b = SavedTrack(query="b", title="B", author="Artist", uri="https://x/b")
    added, duplicates = await store.add_tracks(1, 2, "mix", [a, b, a])
    assert (added, duplicates) == (2, 1)
    await store.move_track(1, 2, "mix", 2, 1)
    item = await store.get(1, 2, "mix")
    assert [x["title"] for x in item["tracks"]] == ["B", "A"]
    assert await store.dedupe(1, 2, "mix") == 0
