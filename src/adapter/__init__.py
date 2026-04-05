from __future__ import annotations

from pathlib import Path

import yaml

from .base import BaseAdapter


def load_adapter(project_dir: Path) -> BaseAdapter:
    """Read config/adapter.yaml and return the configured adapter."""
    config_path = project_dir / "config" / "adapter.yaml"
    cfg: dict = {}
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

    provider = cfg.get("provider", "claude")

    if provider == "claude":
        from .claude import ClaudeAdapter
        return ClaudeAdapter(
            model=cfg.get("model", "claude-opus-4-6"),
            api_key_env=cfg.get("api_key_env", "ANTHROPIC_API_KEY"),
        )

    raise ValueError(f"Unknown adapter provider: {provider!r}")
