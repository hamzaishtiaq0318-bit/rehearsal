"""Load rehearsal.toml: which app Rehearsal protects and how to run copies of it."""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Target:
    name: str
    app_dir: Path
    command: list[str]
    health_path: str
    database_env: str
    live_database: Path
    seed_command: list[str] | None
    live_port: int
    volatile_columns: frozenset[str]
    owner_table: str | None
    money: dict[str, str] = field(default_factory=dict)

    def server_file(self) -> Path:
        """The newest Synapse-generated server; a local rebuild writes _2, _3 and so on beside the old one."""
        files = sorted(self.app_dir.glob("synapse_auto_server*.py"), key=lambda p: (p.stat().st_mtime, p.name))
        if not files:
            raise FileNotFoundError(f"No synapse_auto_server*.py in {self.app_dir}. Run synapse build first.")
        return files[-1]

    @staticmethod
    def render(parts: list[str], **values: object) -> list[str]:
        return [p.format(python=sys.executable, **values) for p in parts]


@dataclass(frozen=True)
class RiskConfig:
    single_payment_limit_cents: int = 50000
    bulk_rows: int = 5


@dataclass(frozen=True)
class Config:
    target: Target
    risk: RiskConfig
    runtime_dir: Path
    phrases: dict[str, str] = field(default_factory=dict)  # tool name -> plain sentence template


def load_env(path: Path = ROOT / ".env") -> None:
    """Read KEY=value lines from .env into the environment; variables already set win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load(path: Path | None = None) -> Config:
    path = (path or ROOT / "rehearsal.toml").resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base, t = path.parent, raw["target"]
    target = Target(
        name=t["name"],
        app_dir=(base / t["app_dir"]).resolve(),
        command=list(t["command"]),
        health_path=t.get("health_path", "/"),
        database_env=t["database_env"],
        live_database=(base / t["live_database"]).resolve(),
        seed_command=t.get("seed_command"),
        live_port=int(t.get("live_port", 8100)),
        volatile_columns=frozenset(t.get("volatile_columns", [])),
        owner_table=t.get("owner_table"),
        money=dict(t.get("money", {})),
    )
    return Config(target, RiskConfig(**raw.get("risk", {})), (base / raw.get("runtime_dir", "runtime")).resolve(),
                  dict(raw.get("phrases", {})))
