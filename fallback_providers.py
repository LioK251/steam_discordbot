from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ManifestProvider:
    name: str
    url_template: str
    success_code: int = 200
    unavailable_code: int = 404
    enabled: bool = True

    def build_url(self, *, app_id: str, moapikey: Optional[str]) -> str:
        url = self.url_template.replace("<appid>", str(app_id))
        if "<moapikey>" in url:
            # If required but missing, keep placeholder so caller can skip.
            url = url.replace("<moapikey>", moapikey or "")
        return url


def load_providers_from_api_json(path: str) -> list[ManifestProvider]:
    """
    Loads providers from ltsteamplugin-style api.json.
    Returns enabled providers only.
    """
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    api_list = data.get("api_list")
    if not isinstance(api_list, list):
        return []

    providers: list[ManifestProvider] = []
    for item in api_list:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", False):
            continue
        name = item.get("name")
        url = item.get("url")
        success_code = item.get("success_code", 200)
        unavailable_code = item.get("unavailable_code", 404)
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        if not isinstance(success_code, int) or not isinstance(unavailable_code, int):
            continue
        providers.append(
            ManifestProvider(
                name=name,
                url_template=url,
                success_code=success_code,
                unavailable_code=unavailable_code,
                enabled=True,
            )
        )

    return providers

