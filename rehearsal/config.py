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
    outbound: dict[str, str] = field(default_factory=dict)  # table -> column of text sent outside the business
    private: dict[str, list[str]] = field(default_factory=dict)  # table -> columns holding personal data

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
class RulesConfig:
    """Business rules written in rehearsal.toml. Breaking any of them raises a high flag."""
    never_change: tuple[str, ...] = ()  # "table" or "table.column" that no task may change
    protected: tuple = ()  # owner ids no task may touch, such as VIP customers
    max_money_cents: int | None = None  # the most one task may pay out in total

    def __post_init__(self) -> None:
        for item in self.never_change:
            if not isinstance(item, str) or not item or item.count(".") > 1:
                raise ValueError(f'[rules] never_change takes "table" or "table.column", not {item!r}')
        if self.max_money_cents is not None and (not isinstance(self.max_money_cents, int) or self.max_money_cents < 0):
            raise ValueError("[rules] max_money_cents must be a whole number of cents, 0 or more")


@dataclass(frozen=True)
class TrustConfig:
    # "low": a rehearsal with no flags at all is applied without asking, still verified and undoable.
    # "none": a person approves every change.
    auto_apply: str = "none"
    # "high": high risk work needs two different people to approve it before anything is applied.
    second_approval: str = "none"

    def __post_init__(self) -> None:
        if self.auto_apply not in ("none", "low"):
            raise ValueError(f'[trust] auto_apply must be "none" or "low", not {self.auto_apply!r}')
        if self.second_approval not in ("none", "high"):
            raise ValueError(f'[trust] second_approval must be "none" or "high", not {self.second_approval!r}')


@dataclass(frozen=True)
class Config:
    target: Target
    risk: RiskConfig
    runtime_dir: Path
    phrases: dict[str, str] = field(default_factory=dict)  # tool name -> plain sentence template
    trust: TrustConfig = field(default_factory=TrustConfig)
    agent_prompt: str | None = None  # the built in agent's instructions for this app; None uses the demo store's
    rules: RulesConfig = field(default_factory=RulesConfig)


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
    """The config file: the path given, else REHEARSAL_CONFIG, else rehearsal.toml in the repo."""
    path = Path(path or os.environ.get("REHEARSAL_CONFIG") or ROOT / "rehearsal.toml").resolve()
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
        outbound=dict(t.get("outbound", {})),
        private={table: list(cols) for table, cols in t.get("private", {}).items()},
    )
    return Config(target, RiskConfig(**raw.get("risk", {})), (base / raw.get("runtime_dir", "runtime")).resolve(),
                  dict(raw.get("phrases", {})), TrustConfig(**raw.get("trust", {})),
                  raw.get("agent", {}).get("prompt"), _rules(raw.get("rules", {})))


def _rules(raw: dict) -> RulesConfig:
    unknown = set(raw) - {"never_change", "protected", "max_money_cents"}
    if unknown:  # a misspelt rule must not quietly stop protecting anything
        raise ValueError(f"[rules] does not know {', '.join(sorted(unknown))}")
    return RulesConfig(never_change=tuple(raw.get("never_change", ())), protected=tuple(raw.get("protected", ())),
                       max_money_cents=raw.get("max_money_cents"))
