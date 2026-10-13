"""CSV experiment logger."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

CSV_HEADER = [
    "timestamp",
    "t_rel_s",
    "csi_mode",
    "ri",
    "pmi_x1",
    "pmi_x2",
    "cqi",
    "bler",
    "throughput_mbps",
    "ue_attached",
]


class ExperimentLogger:
    def __init__(self, root_dir: str, stamp: str | None = None):
        root = Path(root_dir)
        root.mkdir(parents=True, exist_ok=True)
        if stamp is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.csv_path = root / f"run_{stamp}.csv"
        self._fh = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_HEADER)
        self._writer.writeheader()
        self._fh.flush()

    def write(self, row: dict) -> None:
        self._writer.writerow({k: row.get(k, "") for k in CSV_HEADER})
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
