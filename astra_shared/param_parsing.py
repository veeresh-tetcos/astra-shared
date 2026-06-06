"""Shared RF parameter parsing helpers used by all Astra services."""

import json
import logging
import math

from .defaults import (
    ADDITIONAL_LOSSES_DB_MAX,
    ADDITIONAL_LOSSES_DB_MIN,
    CLUTTER_LOSS_DB_MAX,
    CLUTTER_LOSS_DB_MIN,
    DEFAULT_BANDWIDTH_HZ,
    DEFAULT_EIRP_DBW,
    DEFAULT_RX_GAIN_DBI,
    DEFAULT_SYSTEM_NOISE_TEMP_K,
    POLARIZATION_LOSS_DB_MAX,
    POLARIZATION_LOSS_DB_MIN,
    VALID_CLUTTER_CLASS_IDS,
)

logger = logging.getLogger(__name__)


def _get_float(
    params: dict,
    key: str,
    default: float,
    min_val: float | None = None,
    max_val: float | None = None,
) -> float:
    """Parse a float parameter with optional clamping."""
    raw_value = params.get(key, default)
    if raw_value in (None, ""):
        raw_value = default
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = default
    if min_val is not None:
        value = max(min_val, value)
    if max_val is not None:
        value = min(max_val, value)
    return value


def _get_int(
    params: dict,
    key: str,
    default: int,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """Parse an integer parameter with optional clamping."""
    raw_value = params.get(key, default)
    if raw_value in (None, ""):
        raw_value = default
    try:
        value = int(float(raw_value))
    except (TypeError, ValueError):
        value = default
    if min_val is not None:
        value = max(min_val, value)
    if max_val is not None:
        value = min(max_val, value)
    return value


def _get_str(params: dict, key: str, default: str) -> str:
    """Parse a string parameter."""
    raw_value = params.get(key, default)
    if raw_value is None:
        raw_value = default
    return str(raw_value).strip()


def _parse_clutter_values(params: dict) -> dict[int, float] | None:
    """Parse user-supplied clutter loss overrides per WorldCover class."""
    raw = params.get("clutter_values")
    if raw is None or raw == "" or raw == "null":
        return None

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    if not isinstance(raw, dict):
        return None

    if len(raw) > 15:
        return None

    result: dict[int, float] = {}
    for key, val in raw.items():
        try:
            class_id = int(key)
        except (TypeError, ValueError):
            continue
        if class_id not in VALID_CLUTTER_CLASS_IDS:
            continue
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(fval):
            continue
        result[class_id] = max(CLUTTER_LOSS_DB_MIN, min(CLUTTER_LOSS_DB_MAX, fval))

    return result if result else None


def _parse_clutter_fallback(params: dict) -> float | None:
    """Parse user-supplied clutter fallback value (dB), clamped to CLUTTER_LOSS_DB_MIN..MAX."""
    raw = params.get("clutter_fallback")
    if raw is None or raw == "" or raw == "null":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    return max(CLUTTER_LOSS_DB_MIN, min(CLUTTER_LOSS_DB_MAX, val))


def _parse_clutter_enable(params: dict) -> bool:
    """Parse clutter enable from 'clutter_mode' (string) or 'clutter_enable' (bool)."""
    if "clutter_mode" in params:
        return _get_str(params, "clutter_mode", "disable").lower() == "enable"
    if "clutter_enable" in params:
        val = params["clutter_enable"]
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("true", "1", "enable")
    return False


def _parse_eirp(params: dict) -> float:
    """Parse EIRP, supporting both new (eirp_dbw) and legacy (tx_power + tx_gain) formats."""
    if "eirp" in params:
        return _get_float(params, "eirp", DEFAULT_EIRP_DBW)
    if "eirp_dbw" in params:
        return _get_float(params, "eirp_dbw", DEFAULT_EIRP_DBW)
    if "tx_power" in params and "tx_gain" in params:
        return _get_float(params, "tx_power", 40.0) + _get_float(params, "tx_gain", 30.0)
    if "tx_power_dbw" in params and "tx_gain_dbi" in params:
        return _get_float(params, "tx_power_dbw", 40.0) + _get_float(params, "tx_gain_dbi", 30.0)
    return DEFAULT_EIRP_DBW


# =============================================================================
# Unified RF Parameter Parsing
# =============================================================================


def parse_rf_params(params: dict) -> dict:
    """Parse RF parameters from any source into a canonical dict.

    Accepts form args, config.json, project files, or HTTP request bodies.
    Callers use the subset they need � unused keys are harmless.
    """
    freq_ghz = _get_float(
        params, "frequency_ghz", _get_float(params, "frequency", 12.0), min_val=0.001
    )
    freq_hz = freq_ghz * 1e9
    aperture_radius_wl = _get_float(params, "aperture_radius_wl", 10.0, min_val=1.0)
    wavelength_m = 3.0e8 / freq_hz
    aperture_radius_m = aperture_radius_wl * wavelength_m
    system_noise_temp_k = _get_float(
        params,
        "system_noise_temp_k",
        DEFAULT_SYSTEM_NOISE_TEMP_K,
        min_val=10.0,
        max_val=10000.0,
    )

    bw_mhz_raw = params.get("bandwidth_mhz")
    bw_hz_raw = params.get("bandwidth_hz")
    bandwidth_hz = None
    explicit_bw = False
    if bw_mhz_raw not in (None, "", "null"):
        explicit_bw = True
        try:
            bandwidth_hz = float(bw_mhz_raw) * 1e6
        except (TypeError, ValueError):
            bandwidth_hz = None
    elif bw_hz_raw not in (None, "", "null"):
        explicit_bw = True
        try:
            bandwidth_hz = float(bw_hz_raw)
        except (TypeError, ValueError):
            bandwidth_hz = None

    if bandwidth_hz is not None and bandwidth_hz <= 0:
        bandwidth_hz = None

    if bandwidth_hz is None and not explicit_bw:
        bandwidth_hz = DEFAULT_BANDWIDTH_HZ

    logger.debug(
        "[RF] noise_temp_k=%s bandwidth_hz=%s cn_enabled=%s",
        system_noise_temp_k,
        bandwidth_hz,
        bandwidth_hz is not None,
    )

    return {
        "eirp_dbw":            _parse_eirp(params),
        "rx_gain_dbi":         _get_float(params, "rx_gain_dbi", _get_float(params, "rx_gain", DEFAULT_RX_GAIN_DBI)),
        "freq_hz":             freq_hz,
        "antenna_model":       _get_str(params, "antenna_model", "gaussian").lower(),
        "beamwidth_deg":       _get_float(params, "beamwidth_deg", _get_float(params, "beamwidth", 4.5), min_val=0.1),
        "aperture_radius_wl":  aperture_radius_wl,
        "aperture_radius_m":   aperture_radius_m,
        "max_gain_dbi":        _get_float(params, "max_gain_dbi", 30.0, min_val=10.0, max_val=60.0),
        "ln_db":               _get_float(params, "ln_db", -20.0),
        "ellipticity_ratio":   _get_float(params, "ellipticity_ratio", 1.0, min_val=1.0, max_val=3.0),
        "num_elements_x":      _get_int(params, "num_elements_x", 8, min_val=1, max_val=64),
        "num_elements_y":      _get_int(params, "num_elements_y", 8, min_val=1, max_val=64),
        "spacing_wl":          _get_float(params, "spacing_wl", 0.5, min_val=0.1, max_val=2.0),
        "element_exponent":    _get_float(params, "element_exponent", 1.3, min_val=0.0, max_val=3.0),
        "clutter_enable":      _parse_clutter_enable(params),
        "clutter_values":      _parse_clutter_values(params),
        "clutter_fallback":    _parse_clutter_fallback(params),
        "atmospheric_mode":    _get_str(params, "atmospheric_mode", "disable").lower(),
        "availability_percent": _get_float(params, "availability_percent", 99.0, min_val=90.0, max_val=99.999),
        "additional_losses_db": _get_float(
            params, "additional_losses_db", 2.0, min_val=ADDITIONAL_LOSSES_DB_MIN, max_val=ADDITIONAL_LOSSES_DB_MAX
        ),
        "polarization_loss_db": _get_float(
            params, "polarization_loss_db", 0.0, min_val=POLARIZATION_LOSS_DB_MIN, max_val=POLARIZATION_LOSS_DB_MAX
        ),
        "system_noise_temp_k": system_noise_temp_k,
        "bandwidth_hz": bandwidth_hz,
    }
