import os
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _resolve_path(base_dir: Path, value: str) -> str:
    expanded = Path(os.path.expanduser(value))
    if expanded.is_absolute():
        return str(expanded)
    return str((base_dir / expanded).resolve())


def load_config(path: str) -> Dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", override=False)
    legacy_env = config_path.parent.parent / "course-buddy" / ".env"
    if legacy_env.exists():
        load_dotenv(legacy_env, override=False)
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg = _expand_env(cfg)
    cfg["config_path"] = str(config_path)
    cfg["config_dir"] = str(config_path.parent)
    cfg["root_dir"] = _resolve_path(config_path.parent, cfg.get("root_dir", "data"))

    cookies_path = cfg.get("cookies_path")
    if cookies_path:
        cfg["cookies_path"] = _resolve_path(config_path.parent, cookies_path)
    return cfg
