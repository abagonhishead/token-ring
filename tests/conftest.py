import os

import pytest

from token_ring import config


@pytest.fixture
def conf(tmp_path, monkeypatch):
    """Write a config file and point token-ring at it: conf("toml text")."""
    def write(text):
        p = tmp_path / "config.toml"
        p.write_text(text)
        os.utime(p, None)
        monkeypatch.setenv("TOKEN_RING_CONFIG", str(p))
        config._cache.update(path=None, mtime=None, config=None)
        return config.get()
    write("")
    return write
