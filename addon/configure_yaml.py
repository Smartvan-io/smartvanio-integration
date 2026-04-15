#!/usr/bin/env python3
"""Safely merge SmartVan.io lovelace config into configuration.yaml.

Adds lovelace resources and dashboard registration without duplicating
entries or clobbering existing user config.

Usage:
    python3 configure_yaml.py /config/configuration.yaml
"""

import sys
import yaml


# ── Handle HA custom YAML tags (!include, !secret, etc.) ──

class _IncludeTag:
    """Wrapper for !include and similar HA YAML tags."""
    def __init__(self, tag, value):
        self.tag = tag
        self.value = value

def _ha_tag_constructor(loader, tag_suffix, node):
    return _IncludeTag("!" + tag_suffix, loader.construct_scalar(node))

def _ha_tag_representer(dumper, data):
    return dumper.represent_scalar(data.tag, data.value)

# Register multi-constructor for all !-prefixed tags
yaml.add_multi_constructor("!", _ha_tag_constructor, Loader=yaml.SafeLoader)
yaml.add_representer(_IncludeTag, _ha_tag_representer, Dumper=yaml.SafeDumper)


SMARTVANIO_RESOURCES = [
    {"url": "/local/smartvanio/kiosk-mode.js", "type": "module"},
    {"url": "/local/smartvanio/smartvanio-main-card.js", "type": "module"},
]

SMARTVANIO_DASHBOARD = {
    "mode": "yaml",
    "title": "SmartVan.io",
    "icon": "mdi:van-utility",
    "show_in_sidebar": True,
    "filename": "dashboards/smartvanio.yaml",
}


def merge_lovelace(config: dict) -> dict:
    """Merge SmartVan.io lovelace config into existing config dict."""
    lovelace = config.setdefault("lovelace", {})

    # Set mode to yaml if not already set
    lovelace.setdefault("mode", "yaml")

    # Merge resources
    resources = lovelace.setdefault("resources", [])
    existing_urls = {r.get("url", "") for r in resources if isinstance(r, dict)}
    for res in SMARTVANIO_RESOURCES:
        if res["url"] not in existing_urls:
            resources.append(res)

    # Merge dashboard
    dashboards = lovelace.setdefault("dashboards", {})
    if "smartvan-io" not in dashboards:
        dashboards["smartvan-io"] = SMARTVANIO_DASHBOARD

    return config


def main():
    if len(sys.argv) < 2:
        print("Usage: configure_yaml.py <configuration.yaml>", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[1]

    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    config = merge_lovelace(config)

    with open(config_path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)


if __name__ == "__main__":
    main()
