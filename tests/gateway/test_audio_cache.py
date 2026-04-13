import os
import time

import pytest

from gateway.platforms.base import cleanup_audio_cache, get_audio_cache_dir


@pytest.fixture(autouse=True)
def _redirect_audio_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.base.AUDIO_CACHE_DIR", tmp_path / "audio_cache"
    )


def test_cleanup_audio_cache_removes_old_files():
    cache_dir = get_audio_cache_dir()
    old_file = cache_dir / "old.ogg"
    old_file.write_bytes(b"old")
    old_mtime = time.time() - 48 * 3600
    os.utime(old_file, (old_mtime, old_mtime))

    removed = cleanup_audio_cache(max_age_hours=24)

    assert removed == 1
    assert not old_file.exists()


def test_cleanup_audio_cache_keeps_recent_files():
    cache_dir = get_audio_cache_dir()
    recent = cache_dir / "recent.ogg"
    recent.write_bytes(b"fresh")

    removed = cleanup_audio_cache(max_age_hours=24)

    assert removed == 0
    assert recent.exists()
