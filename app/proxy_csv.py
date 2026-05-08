from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProxyRow:
    region: str
    host: str
    port: int
    user: str
    password: str


def load_proxies(csv_path: str) -> list[ProxyRow]:
    """
    Load proxy rows from CSV (Region,Host,Port,User,Pass).
    Missing file, empty file, or no valid rows returns [].
    """
    path = Path(csv_path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return []
    rows: list[ProxyRow] = []
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        return []
    for raw in reader:
        host = (raw.get("Host") or "").strip()
        port_s = (raw.get("Port") or "").strip()
        if not host or not port_s:
            continue
        try:
            port = int(port_s)
        except ValueError:
            continue
        region = (raw.get("Region") or "").strip()
        user = (raw.get("User") or "").strip()
        password = (raw.get("Pass") or "").strip()
        rows.append(
            ProxyRow(region=region, host=host, port=port, user=user, password=password)
        )
    return rows


def pick_balanced_proxy_index(usage_per_index: list[int]) -> int:
    """
    Pick an index with minimum current usage; break ties uniformly at random.
    """
    if not usage_per_index:
        raise ValueError("usage_per_index must be non-empty")
    m = min(usage_per_index)
    candidates = [i for i, c in enumerate(usage_per_index) if c == m]
    return random.choice(candidates)
