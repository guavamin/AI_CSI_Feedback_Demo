"""Empirical distribution helpers (CDF, running mean)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def empirical_cdf(samples: list[float], n_points: int = 64) -> tuple[list[float], list[float]]:
    """Return (x, F(x)) for an empirical CDF over ``samples``.

    Constant (or single-value) distributions return a vertical step at that
    value so the curve stays visible on a wide axis (important for pegged CQI).
    """
    if not samples:
        return [], []
    n_points = max(2, int(n_points))
    arr = np.asarray(samples, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return [], []
    arr.sort()
    lo = float(arr[0])
    hi = float(arr[-1])
    # Degenerate: all samples equal (common for CQI=15). linspace(lo, lo, N)
    # collapses to points at (x, 1) with no 0→1 riser → invisible on the plot.
    if arr.size == 1 or hi <= lo:
        return [lo, lo], [0.0, 1.0]

    # Prefer a true step CDF when the support is small (discrete CQI / PMI-like).
    uniq, counts = np.unique(arr, return_counts=True)
    if uniq.size <= max(8, n_points // 4):
        cum = np.cumsum(counts) / float(arr.size)
        xs: list[float] = []
        ys: list[float] = []
        prev_y = 0.0
        for u, c in zip(uniq.astype(float), cum.astype(float)):
            uf = float(u)
            cf = float(c)
            xs.extend([uf, uf])
            ys.extend([prev_y, cf])
            prev_y = cf
        return xs, ys

    xs_arr = np.linspace(lo, hi, n_points)
    # F(x) = P(X <= x)
    ys_arr = np.searchsorted(arr, xs_arr, side="right") / float(arr.size)
    return xs_arr.tolist(), ys_arr.tolist()


@dataclass
class ModeDistribution:
    """Per-mode sample bank + frozen/live CDF + running average trajectory."""

    n: int = 0
    bler_samples: list[float] = field(default_factory=list)
    tput_samples: list[float] = field(default_factory=list)
    cqi_samples: list[float] = field(default_factory=list)
    # CDF curves (updated only while this mode is live)
    bler_cdf_x: list[float] = field(default_factory=list)
    bler_cdf_y: list[float] = field(default_factory=list)
    tput_cdf_x: list[float] = field(default_factory=list)
    tput_cdf_y: list[float] = field(default_factory=list)
    cqi_cdf_x: list[float] = field(default_factory=list)
    cqi_cdf_y: list[float] = field(default_factory=list)
    # Running average vs time (grows only while live; freezes when inactive)
    avg_t_rel: list[float] = field(default_factory=list)
    avg_bler: list[float] = field(default_factory=list)
    avg_tput: list[float] = field(default_factory=list)
    avg_cqi: list[float] = field(default_factory=list)
    mean_bler: float = float("nan")
    mean_tput: float = float("nan")
    mean_cqi: float = float("nan")

    def clear(self) -> None:
        self.n = 0
        self.bler_samples.clear()
        self.tput_samples.clear()
        self.cqi_samples.clear()
        self.bler_cdf_x.clear()
        self.bler_cdf_y.clear()
        self.tput_cdf_x.clear()
        self.tput_cdf_y.clear()
        self.cqi_cdf_x.clear()
        self.cqi_cdf_y.clear()
        self.avg_t_rel.clear()
        self.avg_bler.clear()
        self.avg_tput.clear()
        self.avg_cqi.clear()
        self.mean_bler = float("nan")
        self.mean_tput = float("nan")
        self.mean_cqi = float("nan")

    def add_sample(
        self,
        *,
        t_rel: float,
        bler: float,
        tput: float,
        cqi: float,
        max_samples: int,
        cdf_points: int,
        max_avg_points: int,
    ) -> None:
        """Append a live sample and refresh CDF + running means."""
        max_samples = max(32, int(max_samples))
        self.bler_samples.append(float(bler))
        self.tput_samples.append(float(tput))
        self.cqi_samples.append(float(cqi))
        if len(self.bler_samples) > max_samples:
            self.bler_samples = self.bler_samples[-max_samples:]
            self.tput_samples = self.tput_samples[-max_samples:]
            self.cqi_samples = self.cqi_samples[-max_samples:]

        self.n = len(self.tput_samples)
        self.mean_bler = float(np.mean(self.bler_samples))
        self.mean_tput = float(np.mean(self.tput_samples))
        self.mean_cqi = float(np.mean(self.cqi_samples))

        self.bler_cdf_x, self.bler_cdf_y = empirical_cdf(self.bler_samples, cdf_points)
        self.tput_cdf_x, self.tput_cdf_y = empirical_cdf(self.tput_samples, cdf_points)
        self.cqi_cdf_x, self.cqi_cdf_y = empirical_cdf(self.cqi_samples, cdf_points)

        self.avg_t_rel.append(float(t_rel))
        self.avg_bler.append(self.mean_bler)
        self.avg_tput.append(self.mean_tput)
        self.avg_cqi.append(self.mean_cqi)
        max_avg_points = max(32, int(max_avg_points))
        if len(self.avg_t_rel) > max_avg_points:
            self.avg_t_rel = self.avg_t_rel[-max_avg_points:]
            self.avg_bler = self.avg_bler[-max_avg_points:]
            self.avg_tput = self.avg_tput[-max_avg_points:]
            self.avg_cqi = self.avg_cqi[-max_avg_points:]
