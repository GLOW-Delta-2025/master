"""Configuration loader for the audio processing app.

Loads defaults, merges values from a local `config.json` (if present),
and optionally fetches a remote JSON config when `REMOTE_CONFIG_URL`
is set in the environment or when `remote_config_url` is present in the
local config.

Usage:
    from config import get_config
    cfg = get_config()

The returned object is a dict with keys matching the constructor
arguments used across the project (device, channels, spike_threshold_db,
alpha_rise, alpha_decay, samplerate, names, etc.).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

DEFAULTS: Dict[str, Any] = {
    "device": 1,
    "channels": [6, 7],
    "samplerate": 48000,
    "spike_threshold_db": 6.0,
    "noise_init_db": -50.0,
    "alpha_rise": 0.95,
    "alpha_decay": 0.6,
    "same_source_corr": 0.75,
    "min_dB_delta": 4.0,
    "max_delay_ms": 10.0,
    "poll_interval": 0.5,
    # simple mapping of channel names (list ordered to channels)
    "names": [],
}


def _load_local_config(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        # If the local config is malformed, ignore it and print a warning
        try:
            print(f"Warning: failed to load local config {path}")
        except Exception:
            pass
        return None


def _fetch_remote_config(url: str) -> Optional[Dict[str, Any]]:
    # Try to import requests lazily; if not available, skip remote fetch.
    try:
        import requests
    except Exception:
        print("Note: 'requests' not installed; skipping remote config fetch")
        return None
    try:
        r = requests.get(url, timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        print(f"Warning: failed to fetch remote config from {url}")
        return None


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict with values from b overriding a. Shallow for lists/values.

    We only need a simple merge for this project where nested structures are
    not expected.
    """
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = v
    return out


def save_local_config(patch: Dict[str, Any], local_path: str = 'config.json') -> None:
    """Merge `patch` into existing local config (or defaults) and write to disk.

    This function performs a shallow merge (keys in patch override existing
    values) and writes the resulting JSON to `local_path` atomically.
    """
    # load existing local config (not defaults)
    try:
        existing = _load_local_config(local_path) or {}
    except Exception:
        existing = {}

    merged = _deep_merge(existing, patch or {})

    # ensure channels are simple list
    ch = merged.get('channels')
    if isinstance(ch, (list, tuple)):
        merged['channels'] = [int(x) for x in ch]

    # write atomically
    tmp_path = f"{local_path}.tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp_path, local_path)
    except Exception:
        try:
            # best-effort fallback
            with open(local_path, 'w', encoding='utf-8') as f:
                json.dump(merged, f, indent=2)
        except Exception:
            print(f"Warning: failed to write local config to {local_path}")


def get_config(local_path: str = 'config.json') -> Dict[str, Any]:
    """Load configuration dict.

    Order of precedence (higher overrides lower):
      1. Remote config (if fetched)
      2. Local config.json
      3. DEFAULTS

    Environment variable `REMOTE_CONFIG_URL` may be used to point to a
    remote JSON. The local config can also contain `remote_config_url` to
    specify the remote location.
    """
    cfg = dict(DEFAULTS)
    local = _load_local_config(local_path)
    if local:
        cfg = _deep_merge(cfg, local)

    # determine remote url from env or local config
    remote_url = os.environ.get('REMOTE_CONFIG_URL') or (local or {}).get('remote_config_url')
    if remote_url:
        remote = _fetch_remote_config(remote_url)
        if remote:
            cfg = _deep_merge(cfg, remote)

    # normalize some fields
    # ensure channels is a list of ints
    ch = cfg.get('channels')
    if isinstance(ch, str):
        try:
            cfg['channels'] = [int(x.strip()) for x in ch.split(',') if x.strip()]
        except Exception:
            pass
    elif isinstance(ch, (list, tuple)):
        cfg['channels'] = [int(x) for x in ch]

    # ensure numeric types
    for k in ('device', 'samplerate'):
        if k in cfg:
            try:
                cfg[k] = int(cfg[k])
            except Exception:
                pass
    for k in ('spike_threshold_db', 'noise_init_db', 'alpha_rise', 'alpha_decay',
              'same_source_corr', 'min_dB_delta', 'max_delay_ms', 'poll_interval'):
        if k in cfg:
            try:
                cfg[k] = float(cfg[k])
            except Exception:
                pass

    return cfg


if __name__ == '__main__':
    # quick debug run
    cfg = get_config()
    print(json.dumps(cfg, indent=2))
