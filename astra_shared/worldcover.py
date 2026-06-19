#!/usr/bin/env python3
"""
worldcover.py

ESA WorldCover clutter data management.

Functions for downloading, caching, and querying ESA WorldCover 10m land cover tiles
to compute clutter loss for satellite link budget calculations.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import os
import platform
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .defaults import (
    CLUTTER_FALLBACK_DB,
    CLUTTER_LOSS_DB,
    WORLDCOVER_DIR,
    WORLDCOVER_S3_BASE,
)

# Rasterio for reading GeoTIFF tiles (optional)
try:
    import rasterio
    from pyproj import Transformer
    from rasterio.windows import Window

    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounded LRU clutter cache
# Stays between CLUTTER_CACHE_MIN_SIZE and CLUTTER_CACHE_MAX_SIZE entries.
# A background daemon thread evicts the oldest (LRU) entries whenever the
# cache exceeds the max, trimming it back down to the min.
# Configurable via environment variables.
# ---------------------------------------------------------------------------
_CACHE_MIN_SIZE: int = int(
    os.environ.get("CLUTTER_CACHE_MIN_SIZE", "4688828")
)  # ~1.0 GB
_CACHE_MAX_SIZE: int = int(
    os.environ.get("CLUTTER_CACHE_MAX_SIZE", "7033243")
)  # ~1.5 GB
_CACHE_EVICT_INTERVAL: float = float(
    os.environ.get("CLUTTER_CACHE_EVICT_INTERVAL", "60")
)

# OrderedDict gives O(1) LRU via move_to_end / popitem(last=False)
_CLUTTER_CACHE: collections.OrderedDict[tuple[int, int], float] = (
    collections.OrderedDict()
)
_CLUTTER_CACHE_LOCK = threading.Lock()


def _evict_clutter_cache() -> int:
    """Evict oldest entries until cache is at min size. Returns number evicted."""
    evicted = 0
    with _CLUTTER_CACHE_LOCK:
        while len(_CLUTTER_CACHE) > _CACHE_MIN_SIZE:
            _CLUTTER_CACHE.popitem(last=False)
            evicted += 1
    return evicted


def _clutter_cache_eviction_loop() -> None:
    """Background daemon: evict stale entries when cache exceeds max size."""
    while True:
        time.sleep(_CACHE_EVICT_INTERVAL)
        if len(_CLUTTER_CACHE) > _CACHE_MAX_SIZE:
            _evict_clutter_cache()


threading.Thread(
    target=_clutter_cache_eviction_loop, daemon=True, name="clutter-cache-evictor"
).start()

# Tile dataset cache: {tile_name: (dataset, transformer, timestamp)}
# Keeps rasterio file handles open for 10 minutes to avoid repeated file I/O
_TILE_CACHE: dict[
    str, tuple
] = {}  # Type: Dict[str, Tuple[rasterio.DatasetReader, Transformer, float]]
_CACHE_TTL_SECONDS = 600  # 10 minutes

# Thread synchronization for tile cache access
_TILE_CACHE_LOCK = threading.Lock()
_TILE_REFS: dict[str, int] = {}  # Reference count per tile

# Per-tile download locks: prevents two threads from downloading the same tile
# simultaneously. Lock is acquired for the duration of the download only.
_TILE_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
_TILE_DOWNLOAD_LOCKS_LOCK = threading.Lock()

# Active bounding box for tile filtering optimization
# Format: (min_lon, min_lat, max_lon, max_lat) or None
_ACTIVE_BBOX: tuple[float, float, float, float] | None = None


def clear_clutter_cache() -> None:
    """Clear the clutter loss cache."""
    with _CLUTTER_CACHE_LOCK:
        _CLUTTER_CACHE.clear()


def clear_tile_cache() -> None:
    """Close cached tile datasets with zero references."""
    with _TILE_CACHE_LOCK:
        to_remove = []
        for tile_name, (ds, _, _) in list(_TILE_CACHE.items()):
            if _TILE_REFS.get(tile_name, 0) == 0:
                try:
                    ds.close()
                except Exception:
                    pass
                to_remove.append(tile_name)
        for tile_name in to_remove:
            del _TILE_CACHE[tile_name]
        # Small delay on Windows to ensure file handles are released
        if to_remove and platform.system() == "Windows":
            time.sleep(0.1)


def set_active_bbox(bbox: tuple[float, float, float, float] | None) -> None:
    """
    Set the active bounding box for tile filtering optimization.

    When set, tiles outside this bbox will be skipped during download.

    Args:
        bbox: (min_lon, min_lat, max_lon, max_lat) or None to disable
    """
    global _ACTIVE_BBOX
    _ACTIVE_BBOX = bbox


def clear_active_bbox() -> None:
    """Clear the active bounding box filter."""
    global _ACTIVE_BBOX
    _ACTIVE_BBOX = None


def get_active_bbox() -> tuple[float, float, float, float] | None:
    """Get the current active bounding box."""
    return _ACTIVE_BBOX


def tile_overlaps_bbox(
    tile_lat: int, tile_lon: int, bbox: tuple[float, float, float, float]
) -> bool:
    """
    Check if a 3x3 degree WorldCover tile overlaps the given bounding box.

    Args:
        tile_lat: Tile south edge latitude (multiple of 3)
        tile_lon: Tile west edge longitude (multiple of 3)
        bbox: (min_lon, min_lat, max_lon, max_lat)

    Returns:
        True if tile overlaps bbox
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    tile_north = tile_lat + 3
    tile_east = tile_lon + 3

    # Check for non-overlap conditions
    if tile_lon > max_lon or tile_east < min_lon:
        return False
    if tile_lat > max_lat or tile_north < min_lat:
        return False
    return True


def get_geojson_bounding_box(geojson: dict) -> tuple[float, float, float, float]:
    """
    Extract bounding box from GeoJSON.

    Handles FeatureCollection, Feature, and direct geometry types.

    Args:
        geojson: GeoJSON dictionary

    Returns:
        Tuple of (min_lon, min_lat, max_lon, max_lat)
    """
    min_lon = float("inf")
    min_lat = float("inf")
    max_lon = float("-inf")
    max_lat = float("-inf")

    def process_coords(coords):
        nonlocal min_lon, min_lat, max_lon, max_lat
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            # Single coordinate [lon, lat]
            min_lon = min(min_lon, coords[0])
            max_lon = max(max_lon, coords[0])
            min_lat = min(min_lat, coords[1])
            max_lat = max(max_lat, coords[1])
        else:
            # Nested coordinates
            for c in coords:
                process_coords(c)

    geojson_type = geojson.get("type", "")

    if geojson_type == "FeatureCollection":
        for feature in geojson.get("features", []):
            geometry = feature.get("geometry", {})
            process_coords(geometry.get("coordinates", []))
    elif geojson_type == "Feature":
        geometry = geojson.get("geometry", {})
        process_coords(geometry.get("coordinates", []))
    else:
        # Direct geometry (Polygon, MultiPolygon, etc.)
        process_coords(geojson.get("coordinates", []))

    return (min_lon, min_lat, max_lon, max_lat)


def _get_cached_tile(tile_path: Path) -> tuple | None:
    """Get cached tile dataset and transformer, or None if expired/missing."""
    tile_name = tile_path.name
    now = time.time()

    if tile_name in _TILE_CACHE:
        ds, transformer, timestamp = _TILE_CACHE[tile_name]
        if now - timestamp < _CACHE_TTL_SECONDS:
            return ds, transformer
        # Expired - close and remove
        try:
            ds.close()
        except Exception:
            pass
        del _TILE_CACHE[tile_name]
    return None


def _cache_tile(tile_path: Path) -> tuple:
    """Open tile and cache dataset + transformer for reuse."""
    ds = rasterio.open(tile_path)
    transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
    _TILE_CACHE[tile_path.name] = (ds, transformer, time.time())
    return ds, transformer


def _get_cached_tile_unlocked(tile_path: Path) -> tuple | None:
    """Get cached tile without acquiring lock (caller must hold lock)."""
    tile_name = tile_path.name
    now = time.time()

    if tile_name in _TILE_CACHE:
        ds, transformer, timestamp = _TILE_CACHE[tile_name]
        if now - timestamp < _CACHE_TTL_SECONDS:
            return ds, transformer
        # Expired but has references - keep it
        if _TILE_REFS.get(tile_name, 0) > 0:
            return ds, transformer
        # Expired and no references - close it
        try:
            ds.close()
        except Exception:
            pass
        del _TILE_CACHE[tile_name]
    return None


def _cache_tile_unlocked(tile_path: Path) -> tuple:
    """Open tile and cache (caller must hold lock)."""
    ds = rasterio.open(tile_path)
    transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
    _TILE_CACHE[tile_path.name] = (ds, transformer, time.time())
    return ds, transformer


@contextmanager
def tile_reader(tile_path: Path) -> Generator[tuple, None, None]:
    """Context manager for safe tile access with reference counting."""
    tile_name = tile_path.name

    with _TILE_CACHE_LOCK:
        # Get or create cached tile
        cached = _get_cached_tile_unlocked(tile_path)
        if cached:
            ds, transformer = cached
        else:
            ds, transformer = _cache_tile_unlocked(tile_path)

        # Increment reference count
        _TILE_REFS[tile_name] = _TILE_REFS.get(tile_name, 0) + 1

    try:
        yield ds, transformer
    finally:
        with _TILE_CACHE_LOCK:
            if tile_name in _TILE_REFS:
                _TILE_REFS[tile_name] -= 1
                if _TILE_REFS[tile_name] <= 0:
                    del _TILE_REFS[tile_name]


def get_tile_name(lat: float, lon: float) -> str:
    """
    Get the WorldCover tile filename for a given coordinate.

    Args:
        lat: Latitude in degrees
        lon: Longitude in degrees

    Returns:
        Tile filename string
    """
    lat0 = int(math.floor(lat / 3.0) * 3)
    lon0 = int(math.floor(lon / 3.0) * 3)
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return (
        f"ESA_WorldCover_10m_2021_v200_{ns}{abs(lat0):02d}{ew}{abs(lon0):03d}_Map.tif"
    )


def ensure_worldcover_tile(
    lat: float, lon: float, worldcover_dir: Path, timeout_sec: int = 300
) -> Path | None:
    """
    Ensure ESA WorldCover 3x3 degree tile exists locally.

    Downloads the tile from S3 if not present. Per-tile locking prevents
    concurrent threads from downloading the same tile simultaneously.
    If an active bounding box is set, tiles outside the bbox are skipped.

    Args:
        lat: Latitude in degrees
        lon: Longitude in degrees
        worldcover_dir: Directory for storing WorldCover tiles
        timeout_sec: Total download timeout in seconds (default 300)

    Returns:
        Path to the local tile file, or None if tile is outside active bbox

    Raises:
        RuntimeError: If download fails or times out
    """
    tile_lat = int(math.floor(lat / 3.0) * 3)
    tile_lon = int(math.floor(lon / 3.0) * 3)

    if _ACTIVE_BBOX is not None:
        if not tile_overlaps_bbox(tile_lat, tile_lon, _ACTIVE_BBOX):
            return None

    tile_name = get_tile_name(lat, lon)
    tile_path = worldcover_dir / tile_name

    if tile_path.exists() and tile_path.stat().st_size > 0:
        return tile_path

    # Per-tile lock: only one thread downloads a given tile at a time.
    # Other threads block here and find the file ready when the lock is released.
    with _TILE_DOWNLOAD_LOCKS_LOCK:
        if tile_name not in _TILE_DOWNLOAD_LOCKS:
            _TILE_DOWNLOAD_LOCKS[tile_name] = threading.Lock()
        tile_lock = _TILE_DOWNLOAD_LOCKS[tile_name]

    with tile_lock:
        # Re-check after acquiring lock — another thread may have downloaded it
        if tile_path.exists() and tile_path.stat().st_size > 0:
            return tile_path

        import requests

        url = f"{WORLDCOVER_S3_BASE}/{tile_name}"
        tmp_path = tile_path.with_suffix(".part")

        # Create directory before opening the temp file
        worldcover_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading WorldCover tile %s from S3...", tile_name)
        start_time = time.time()
        try:
            # connect_timeout=30, read_timeout=60 per chunk
            with requests.get(url, stream=True, timeout=(30, 60)) as r:
                if r.status_code == 404:
                    raise RuntimeError(
                        f"WorldCover tile not found on S3 (HTTP 404): {tile_name}"
                    )
                if r.status_code != 200:
                    raise RuntimeError(
                        f"WorldCover download failed (HTTP {r.status_code}): {tile_name}"
                    )
                total = int(r.headers.get("Content-Length", 0))
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if time.time() - start_time > timeout_sec:
                                raise RuntimeError(
                                    f"Download timed out after {timeout_sec}s "
                                    f"({downloaded} bytes received)"
                                )
                if total and downloaded < total:
                    raise RuntimeError(
                        f"Incomplete download: got {downloaded}/{total} bytes"
                    )
            tmp_path.replace(tile_path)
            elapsed = time.time() - start_time
            logger.info(
                "WorldCover tile %s downloaded in %.1fs (%d bytes)",
                tile_name,
                elapsed,
                downloaded,
            )
            return tile_path
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to download {tile_name}: {exc}") from exc


def fetch_worldcover_class(
    lat: float,
    lon: float,
    worldcover_dir: Path | None = None,
    download_if_missing: bool = True,
) -> int | None:
    """
    Fetch ESA WorldCover land cover class from local tile.

    Uses cached tile datasets and transformers for 10-minute TTL to avoid
    repeated file I/O during grid computations.

    Args:
        lat: Latitude in degrees
        lon: Longitude in degrees
        worldcover_dir: Directory containing WorldCover tiles (defaults to WORLDCOVER_DIR)
        download_if_missing: If True, download tile if not present

    Returns:
        Land cover class ID (10-100) or None if unavailable
    """
    if not HAS_RASTERIO:
        return None

    if worldcover_dir is None:
        worldcover_dir = WORLDCOVER_DIR
        worldcover_dir.mkdir(parents=True, exist_ok=True)

    tile_name = get_tile_name(lat, lon)
    tile_path = worldcover_dir / tile_name

    if not tile_path.exists():
        if not download_if_missing:
            return None
        try:
            result = ensure_worldcover_tile(lat, lon, worldcover_dir)
        except Exception as exc:
            logger.warning(
                "WorldCover tile download failed for (%.4f, %.4f): %s — using fallback",
                lat,
                lon,
                exc,
            )
            return None
        if result is None:
            # Tile skipped (outside active bbox)
            return None
        tile_path = result

    try:
        # Use context manager for thread-safe tile access with reference counting
        with tile_reader(tile_path) as (ds, transformer):
            x, y = transformer.transform(lon, lat)
            row, col = ds.index(x, y)
            window = Window(col, row, 1, 1)
            data = ds.read(1, window=window)
            val = int(data[0, 0])
            return None if val == 0 else val
    except Exception:
        return None


def clutter_loss_db(
    lat: float,
    lon: float,
    point_num: int = 0,
    worldcover_dir: Path | None = None,
    verbose: bool = True,
    loss_table: dict[int, float] | None = None,
    fallback_db: float | None = None,
) -> float:
    """
    Get clutter loss in dB for a location using WorldCover data.

    Uses caching to avoid repeated lookups for nearby coordinates.
    Tile datasets are cached for 10 minutes to avoid repeated file I/O.

    Args:
        lat: Latitude in degrees
        lon: Longitude in degrees
        point_num: Point number for progress reporting (logs every 50 points)
        worldcover_dir: Directory containing WorldCover tiles (defaults to WORLDCOVER_DIR)
        verbose: If True, print progress messages
        loss_table: Custom clutter loss values per class ID (None = use defaults)
        fallback_db: Custom fallback loss for unknown classes (None = use default)

    Returns:
        Clutter loss in dB
    """
    key = (int(round(lat * 1000)), int(round(lon * 1000)))

    with _CLUTTER_CACHE_LOCK:
        if key in _CLUTTER_CACHE:
            _CLUTTER_CACHE.move_to_end(key)  # mark as recently used
            return _CLUTTER_CACHE[key]

    table = loss_table if loss_table is not None else CLUTTER_LOSS_DB
    fb = fallback_db if fallback_db is not None else CLUTTER_FALLBACK_DB

    if worldcover_dir is None:
        worldcover_dir = WORLDCOVER_DIR
        worldcover_dir.mkdir(parents=True, exist_ok=True)

    cl = fetch_worldcover_class(lat, lon, worldcover_dir)
    if cl is None:
        # Tile unavailable (download failed, ocean, bbox-skipped) — return fallback
        # but do NOT cache it so a later successful download gives the real value.
        return fb

    if verbose and (point_num == 0 or point_num % 50 == 0):
        print(f"[Clutter] WorldCover class at lat={lat:.2f}, lon={lon:.2f} is {cl}")
    val = table.get(cl, fb)

    with _CLUTTER_CACHE_LOCK:
        _CLUTTER_CACHE[key] = val
        _CLUTTER_CACHE.move_to_end(key)
        # Inline eviction: if we just crossed the max, trim to min immediately
        if len(_CLUTTER_CACHE) > _CACHE_MAX_SIZE:
            while len(_CLUTTER_CACHE) > _CACHE_MIN_SIZE:
                _CLUTTER_CACHE.popitem(last=False)

    return val


def load_country_boundary(
    country_code: str,
    boundaries_dir: Path,
    progress_callback: Callable[[str], None] | None = None,
    state_code: str | None = None,
) -> dict | None:
    """
    Load country or state boundary GeoJSON from local file.

    For state-level boundaries, files are stored in a subdirectory named after
    the country (e.g., data/boundaries/india/karnataka.geojson).

    Args:
        country_code: Country code (e.g., 'japan', 'india')
        boundaries_dir: Directory containing boundary GeoJSON files
        progress_callback: Optional callback for progress messages
        state_code: Optional state code for state-level boundaries

    Returns:
        GeoJSON dict or None if not found
    """

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)
        else:
            print(msg)

    # Determine path: state is under country subdirectory
    if state_code:
        boundary_path = boundaries_dir / country_code / f"{state_code}.geojson"
        label = f"{state_code.replace('_', ' ').title()}, {country_code.title()}"
    else:
        boundary_path = boundaries_dir / f"{country_code}.geojson"
        label = country_code.title()

    if not boundary_path.exists():
        log(f"Boundary file not found: {boundary_path}")
        return None

    try:
        with open(boundary_path, encoding="utf-8") as f:
            data = json.load(f)
        # Normalize: some boundary files are a single Feature or raw geometry.
        # Convert to FeatureCollection so downstream code (JS mask) can assume .features
        if isinstance(data, dict):
            t = data.get("type", "")
            if t == "FeatureCollection":
                norm = data
            elif t == "Feature":
                norm = {"type": "FeatureCollection", "features": [data]}
            elif "geometry" in data:
                # Raw object with geometry property
                feat = {
                    "type": "Feature",
                    "properties": {},
                    "geometry": data.get("geometry"),
                }
                norm = {"type": "FeatureCollection", "features": [feat]}
            elif "coordinates" in data:
                # Raw geometry dict
                geom = {
                    "type": data.get("geometry", {}).get("type", "Polygon"),
                    "coordinates": data.get("coordinates"),
                }
                feat = {"type": "Feature", "properties": {}, "geometry": geom}
                norm = {"type": "FeatureCollection", "features": [feat]}
            else:
                norm = data
        else:
            norm = data

        log(f"Loaded {label} boundary for masking")
        return norm
    except Exception as e:
        log(f"Failed to load boundary: {e}")
        return None
