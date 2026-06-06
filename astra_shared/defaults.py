"""
defaults.py

Shared configuration constants for all Astra services.
Covers RF defaults, WorldCover clutter configuration, and atmospheric model settings.
"""

from __future__ import annotations

import os
from pathlib import Path

# =============================================================================
# RF Link Budget Defaults
# =============================================================================

DEFAULT_EIRP_DBW: float = 70.0
DEFAULT_RX_GAIN_DBI: float = 0.0
DEFAULT_SYSTEM_NOISE_TEMP_K: float = 290.0
DEFAULT_BANDWIDTH_MHZ: float = 10.0
DEFAULT_BANDWIDTH_HZ: float = DEFAULT_BANDWIDTH_MHZ * 1e6
ADDITIONAL_LOSSES_DB_MIN: float = 0.0
ADDITIONAL_LOSSES_DB_MAX: float = 20.0
POLARIZATION_LOSS_DB_MIN: float = 0.0
POLARIZATION_LOSS_DB_MAX: float = 10.0
CLUTTER_LOSS_DB_MIN: float = 0.0
CLUTTER_LOSS_DB_MAX: float = 30.0
BOLTZMANN_DB: float = 228.6
K_BOLTZMANN_LINEAR: float = 1.380649e-23

# =============================================================================
# ITU-R P.618 Atmospheric Model Defaults
# =============================================================================

DEFAULT_ATMOSPHERIC_LOSS_ENABLED: bool = False
DEFAULT_AVAILABILITY_PERCENT: float = 99.0

ATMOSPHERIC_IMPACT_NOTES: dict[str, str] = {
    "L-band":  "Minimal atmospheric loss (<0.5 dB)",
    "S-band":  "Minimal atmospheric loss (<0.5 dB)",
    "X-band":  "Moderate rain attenuation (1-5 dB)",
    "Ku-band": "Significant rain attenuation (5-15 dB)",
    "Ka-band": "High rain attenuation (10-30 dB)",
    "Q/V-band": "Very high rain attenuation (>20 dB)",
}

# =============================================================================
# WorldCover Clutter Configuration
# =============================================================================

# Tile directory — override via WORLDCOVER_DIR env var in container deployments
WORLDCOVER_DIR: Path = Path(
    os.environ.get("WORLDCOVER_DIR", str(Path(__file__).parent.parent / "data/worldcover"))
)

# S3 base URL for downloading ESA WorldCover tiles on demand
WORLDCOVER_S3_BASE: str = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"
)

# Clutter loss per WorldCover land cover class (dB)
CLUTTER_LOSS_DB: dict[int, float] = {
    10: 3.0,   # Tree cover
    20: 2.0,   # Shrubland
    30: 1.0,   # Grassland
    40: 1.5,   # Cropland
    50: 8.0,   # Built-up
    60: 0.5,   # Bare / sparse vegetation
    70: 0.5,   # Snow & ice
    80: 0.5,   # Permanent water bodies
    90: 2.0,   # Herbaceous wetland
    95: 0.0,   # Mangroves
    100: 0.0,  # Moss & lichen
}

# Fallback clutter loss when land cover class is unknown or tile unavailable
CLUTTER_FALLBACK_DB: float = 3.0

# Human-readable labels for WorldCover land cover classes
CLUTTER_CLASS_LABELS: dict[int, str] = {
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up",
    60: "Bare / sparse vegetation",
    70: "Snow & ice",
    80: "Permanent water bodies",
    90: "Herbaceous wetland",
    95: "Mangroves",
    100: "Moss & lichen",
}

# Valid WorldCover class IDs — derived from CLUTTER_LOSS_DB, used for input validation
VALID_CLUTTER_CLASS_IDS: frozenset[int] = frozenset(CLUTTER_LOSS_DB.keys())
