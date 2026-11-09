"""The demo database: Chinook, a sample digital-music store (customers, invoices, tracks, staff).

Chinook is MIT-licensed (c) 2008-2024 Luis Rocha, https://github.com/lerocha/chinook-database.
Pinned to a commit and checked by SHA-256. The SQLite file backs local runs and the safety suite; the
Postgres script is loaded by docker/postgres/Dockerfile.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

COMMIT = "7f67772503d71ba90f19283c38e93923addb43fa"
BASE = f"https://raw.githubusercontent.com/lerocha/chinook-database/{COMMIT}/ChinookDatabase/DataSources"
SQLITE_URL = f"{BASE}/Chinook_Sqlite.sqlite"
SQLITE_SHA256 = "7651ba378ac2fcd0dfc3c66fb101f7a7eed3ba39a612ec642b96e20702061f15"
DEMO_PATH = Path("data") / "demo" / "chinook.sqlite"


def download_chinook(path: Path = DEMO_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        print(f"downloading Chinook from {SQLITE_URL}")
        urllib.request.urlretrieve(SQLITE_URL, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != SQLITE_SHA256:
        path.unlink()
        raise SystemExit(f"Chinook hash mismatch ({digest}); file removed")
    print(f"Chinook ok: {path}")
    return path


if __name__ == "__main__":
    download_chinook()
