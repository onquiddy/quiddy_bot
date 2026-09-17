from pathlib import Path



def test_music_search_uses_exact_lavalink_identifiers():
    root = Path(__file__).parents[1]
    source = (root / "plugins" / "music" / "manager.py").read_text("utf-8")
    # Regression contract from the live bug: Wavelink must never get a chance to
    # transform ytsearch: into ytmsearch:ytsearch: (or silently pick ytmsearch:).
    assert 'identifier = f"ytsearch:{query}"' in source
    assert "wavelink.Pool.fetch_tracks(identifier, node=self.node)" in source
    assert "wavelink.Playable.search(normalized" not in source
    assert 'f"ytsearch{limit}:{query}"' in source
