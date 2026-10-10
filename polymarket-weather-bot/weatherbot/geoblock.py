"""Polymarket's own check of whether trading is allowed from this connection."""
from __future__ import annotations

import requests

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


def check_geoblock(session: requests.Session, timeout: float = 10) -> dict:
    """Returns e.g. {"blocked": false, "country": "..."}; treats errors as blocked."""
    try:
        r = session.get(GEOBLOCK_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        data["blocked"] = bool(data.get("blocked", True))
        return data
    except Exception as exc:  # noqa: BLE001 - any failure means we can't confirm access
        return {"blocked": True, "error": str(exc)}
