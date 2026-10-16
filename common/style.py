"""Shared look: palette, scenario identities, bundled fonts and logos. Used by every GUI in this repo."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FONTS = REPO / "assets/fonts"
LOGOS = REPO / "assets/logos"
ICON_DIR = REPO / "assets/icon"  # window / taskbar icon, built by tools/make_app_icon.py
ICON_STYLE = "light"  # light (ivory tile, as the GUI header) | dark (the GUI's background)


def viewport_icons() -> dict:
    """create_viewport() keywords for the window / taskbar icon (empty if the files are missing)."""
    tag = "" if ICON_STYLE == "light" else "_dark"
    small, large = ICON_DIR / f"app_icon{tag}_64.png", ICON_DIR / f"app_icon{tag}.png"
    if not (small.exists() and large.exists()):
        return {}
    return {"small_icon": str(small), "large_icon": str(large)}
MOCKUP_DATA = REPO / "data/mockups"

# Trailer palette (brooklyn_6g_video/trailer/scripts: render_trailer.py, render_story_reference*.py)
BG, SURFACE, LINE = "#1B1B1A", "#20201F", "#3A3936"
TEXT, MUTED, DIM = "#EDEBE4", "#B9B7AF", "#8E8C85"
PALE, INK = "#FAF9F5", "#10243E"
V1, V1_LIGHT = "#7746A3", "#C9A8E6"  # vendor 1 (purple)
V2, V2_LIGHT = "#008E3A", "#8FE0B0"  # vendor 2 (green)
ORANGE, BLUE, BLUE_2, GRAY = "#F28C28", "#08A9E8", "#1687E5", "#7F8B99"
GREEN = "#2FB866"
BOX_EDGE = "#6E8BAA"

# Scenario identities: the same colours on every screen (cards, plots, CDFs, pop-ups)
SCENARIOS = [
    dict(key="legacy", n=1, title="Legacy CSI Feedback", short="Legacy", color=GRAY, text="#AEB7C2",
         run="01_legacy_start_r1", sub="3GPP Type-I codebook: the UE reports a beam index"),
    dict(key="same", n=2, title="AI Feedback · Same Vendor", short="Same vendor", color=V1, text=V1_LIGHT,
         run="02_packed_1to1_subband_r1", sub="UE and gNB from the same vendor, trained end to end"),
    dict(key="cross", n=3, title="AI Feedback · Cross Vendor", short="Cross vendor", color=BLUE_2, text=BLUE,
         run="06_interop_1to2_gnb_subband_r1", sub="Vendor 1 UE served by a Vendor 2 gNB, trained separately"),
]
SCENARIO = {s["key"]: s for s in SCENARIOS}

# Logos in display order with their header height (px)
LOGO_FILES = (("logo_asu.png", 46), ("logo_lab.png", 30), ("logo_id.png", 32))
TITLE = "From Conventional CSI to Interoperable AI-Native Latent Feedback: A live OpenAirInterface prototype"


def rgb(h: str, a: int = 255) -> tuple[int, int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a


def mix(h: str, base: str, w: float) -> tuple[int, int, int, int]:
    a, b = rgb(h), rgb(base)
    return tuple(int(a[i] * w + b[i] * (1 - w)) for i in range(3)) + (255,)


def roll(v, n=8):
    """Trailing moving average (same length as v)."""
    return [sum(v[max(0, i - n + 1): i + 1]) / len(v[max(0, i - n + 1): i + 1]) for i in range(len(v))]
