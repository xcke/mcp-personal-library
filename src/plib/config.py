from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

MIN_TOKEN_LENGTH = 32
INDEX_DIRNAME = ".plib"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    root: Path
    token: str

    @property
    def index_dir(self) -> Path:
        return self.root / INDEX_DIRNAME

    @property
    def db_path(self) -> Path:
        return self.index_dir / "index.sqlite"


def load_config(root: str | Path, env: dict[str, str]) -> Config:
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ConfigError(f"Library root is not a directory: {root_path}")
    token = env.get("PLIB_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        problem = "is not set" if not token else f"is too short ({len(token)} chars)"
        raise ConfigError(
            f"PLIB_TOKEN {problem}; it must be at least {MIN_TOKEN_LENGTH} characters.\n"
            f"Suggested value: PLIB_TOKEN={secrets.token_urlsafe(32)}"
        )
    return Config(root=root_path, token=token)
