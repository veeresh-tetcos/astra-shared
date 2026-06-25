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

import requests
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

# Shared tile dataset cache: {tile_name: (dataset, transformer, timestamp)}
# One DatasetReader per tile (minimal handles). Protected by _TILE_CACHE_LOCK for
# open/close; protected by per-tile _TILE_READ_LOCKS for ds.read() calls.
# rasterio DatasetReader.read() is NOT thread-safe — concurrent reads on the
# same object corrupt GDAL's internal ZIP decoder. The per-tile read lock
# serializes ds.read() calls so GDAL state is never touched by two threads at once.
_TILE_CACHE: dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 600  # 10 minutes
_TILE_CACHE_LOCK = threading.Lock()
_TILE_REFS: dict[str, int] = {}          # ref-count: tile not evicted while > 0
_TILE_READ_LOCKS: dict[str, threading.Lock] = {}  # per-tile read serialiser

# Per-tile download locks: prevents two threads from downloading the same tile
# simultaneously. Lock is acquired for the duration of the download only.
_TILE_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
_TILE_DOWNLOAD_LOCKS_LOCK = threading.Lock()

# Tiles confirmed absent on S3 (HTTP 404). Avoids re-requesting ocean tiles or
# tiles outside the WorldCover coverage area on every coverage computation.
# Persisted to WORLDCOVER_DIR/tiles_not_on_s3.json so ocean-tile probes are
# skipped across service restarts (previously 51 wasted probes per Japan run).
_TILES_NOT_ON_S3: set[str] = set()
_TILES_NOT_ON_S3_PATH = WORLDCOVER_DIR / "tiles_not_on_s3.json"
_TILES_NOT_ON_S3_LOCK = threading.Lock()


def _load_tiles_not_on_s3() -> None:
    with _TILES_NOT_ON_S3_LOCK:
        try:
            if _TILES_NOT_ON_S3_PATH.exists():
                data = json.loads(_TILES_NOT_ON_S3_PATH.read_text())
                if isinstance(data, list):
                    _TILES_NOT_ON_S3.update(data)
                    logger.debug("Loaded %d known-absent S3 tiles from cache", len(data))
        except Exception as exc:
            logger.warning("Could not load tiles_not_on_s3 cache: %s", exc)


def _save_tiles_not_on_s3() -> None:
    try:
        tmp = _TILES_NOT_ON_S3_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(_TILES_NOT_ON_S3)))
        tmp.replace(_TILES_NOT_ON_S3_PATH)
    except Exception as exc:
        logger.warning("Could not save tiles_not_on_s3 cache: %s", exc)

# Tiles confirmed present on local disk.  Once a tile is in this set, the hot
# path in fetch_worldcover_class skips the 337K pathlib.exists() syscalls
# (each costs ~0.35 ms on Windows) that were the #1 bottleneck.
_TILES_KNOWN_LOCAL: set[str] = set()
_TILES_KNOWN_LOCAL_LOCK = threading.Lock()


def _is_tile_known_local(tile_name: str) -> bool:
    with _TILES_KNOWN_LOCAL_LOCK:
        return tile_name in _TILES_KNOWN_LOCAL


def _mark_tile_known_local(tile_name: str) -> None:
    with _TILES_KNOWN_LOCAL_LOCK:
        _TILES_KNOWN_LOCAL.add(tile_name)

# Bboxes that have already been fully prefetched this process lifetime.
# Prevents the 30 parallel worker threads from each triggering a redundant prefetch.
_PREFETCHED_BBOXES: set[tuple[float, float, float, float]] = set()
_PREFETCHED_BBOXES_LOCK = threading.Lock()

# Create WORLDCOVER_DIR once at import time (single-threaded) so no thread ever
# needs to call mkdir during a live request — concurrent pathlib.mkdir on Windows
# (Python 3.14) causes an access violation under heavy thread load.
WORLDCOVER_DIR.mkdir(parents=True, exist_ok=True)
_load_tiles_not_on_s3()

# Persistent HTTPS session for S3 tile downloads.
# Reusing one session across all downloads avoids per-connection TLS handshake
# overhead (~0.5 s each): load_verify_locations + do_handshake + TCP connect
# accounted for 42 s in a 213-tile download run.
_S3_SESSION = requests.Session()
_S3_ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=4,
    pool_maxsize=8,
    max_retries=0,
)
_S3_SESSION.mount("https://", _S3_ADAPTER)
_S3_SESSION.mount("http://", _S3_ADAPTER)

def clear_clutter_cache() -> None:
    """Clear the clutter loss cache."""
    with _CLUTTER_CACHE_LOCK:
        _CLUTTER_CACHE.clear()


def clear_tile_cache() -> None:
    """Close cached tile datasets that have no active readers."""
    with _TILE_CACHE_LOCK:
        evicted: list[str] = []
        for tile_name in list(_TILE_CACHE):
            if _TILE_REFS.get(tile_name, 0) == 0:
                ds, _, _ = _TILE_CACHE.pop(tile_name)
                try:
                    ds.close()
                except Exception:
                    pass
                evicted.append(tile_name)
        for tile_name in evicted:
            _TILE_REFS.pop(tile_name, None)
            _TILE_READ_LOCKS.pop(tile_name, None)

    with _TILE_DOWNLOAD_LOCKS_LOCK:
        for tile_name in evicted:
            _TILE_DOWNLOAD_LOCKS.pop(tile_name, None)


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


@contextmanager
def tile_reader(tile_path: Path) -> Generator[tuple, None, None]:
    """
    Thread-safe tile access: one shared DatasetReader per tile, serialised reads.

    Strategy:
    - _TILE_CACHE holds one rasterio.DatasetReader per tile (minimises OS handles).
    - _TILE_READ_LOCKS[tile] serialises ds.read() calls for that tile, because
      rasterio/GDAL's internal ZIP decoder is NOT thread-safe for concurrent reads
      on the same object.  Reads are microseconds, so lock contention is negligible.
    - _TILE_REFS prevents eviction of a tile while any reader holds it.
    """
    tile_name = tile_path.name
    now = time.time()

    with _TILE_CACHE_LOCK:
        entry = _TILE_CACHE.get(tile_name)
        if entry:
            ds, transformer, ts = entry
            if now - ts >= _CACHE_TTL_SECONDS and _TILE_REFS.get(tile_name, 0) == 0:
                # Expired and no active readers — reopen
                try:
                    ds.close()
                except Exception:
                    pass
                ds = rasterio.open(tile_path)
                transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
                _TILE_CACHE[tile_name] = (ds, transformer, now)
        else:
            ds = rasterio.open(tile_path)
            transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            _TILE_CACHE[tile_name] = (ds, transformer, now)

        _TILE_REFS[tile_name] = _TILE_REFS.get(tile_name, 0) + 1

        if tile_name not in _TILE_READ_LOCKS:
            _TILE_READ_LOCKS[tile_name] = threading.Lock()
        read_lock = _TILE_READ_LOCKS[tile_name]

    read_lock.acquire()
    try:
        yield ds, transformer
    finally:
        read_lock.release()
        with _TILE_CACHE_LOCK:
            _TILE_REFS[tile_name] = max(0, _TILE_REFS.get(tile_name, 1) - 1)


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
    lat: float,
    lon: float,
    worldcover_dir: Path,
    timeout_sec: int = 300,
    bbox: tuple[float, float, float, float] | None = None,
) -> Path | None:
    """
    Ensure ESA WorldCover 3x3 degree tile exists locally.

    Downloads the tile from S3 if not present. Per-tile locking prevents
    concurrent threads from downloading the same tile simultaneously.
    When bbox is supplied, tiles outside it are skipped.

    Args:
        lat: Latitude in degrees
        lon: Longitude in degrees
        worldcover_dir: Directory for storing WorldCover tiles
        timeout_sec: Total download timeout in seconds (default 300)
        bbox: Optional (min_lon, min_lat, max_lon, max_lat) tile filter

    Returns:
        Path to the local tile file, or None if tile is outside bbox

    Raises:
        RuntimeError: If download fails or times out
    """
    tile_lat = int(math.floor(lat / 3.0) * 3)
    tile_lon = int(math.floor(lon / 3.0) * 3)

    if bbox is not None:
        if not tile_overlaps_bbox(tile_lat, tile_lon, bbox):
            return None

    tile_name = get_tile_name(lat, lon)

    # Skip tiles confirmed absent on S3 (ocean tiles, tiles outside coverage area)
    if tile_name in _TILES_NOT_ON_S3:
        return None

    tile_path = worldcover_dir / tile_name

    if tile_path.exists() and tile_path.stat().st_size > 0:
        _mark_tile_known_local(tile_name)
        return tile_path

    # Per-tile lock: only one thread downloads a given tile at a time.
    # Other threads block here and find the file ready when the lock is released.
    with _TILE_DOWNLOAD_LOCKS_LOCK:
        if tile_name not in _TILE_DOWNLOAD_LOCKS:
            _TILE_DOWNLOAD_LOCKS[tile_name] = threading.Lock()
        tile_lock = _TILE_DOWNLOAD_LOCKS[tile_name]

    with tile_lock:
        # Re-check after acquiring lock — another thread may have downloaded it
        # or confirmed it absent while we were waiting.
        if tile_name in _TILES_NOT_ON_S3:
            return None
        if tile_path.exists() and tile_path.stat().st_size > 0:
            _mark_tile_known_local(tile_name)
            return tile_path

        url = f"{WORLDCOVER_S3_BASE}/{tile_name}"
        tmp_path = tile_path.with_suffix(".part")

        # Create directory before opening the temp file
        worldcover_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading WorldCover tile %s from S3...", tile_name)
        start_time = time.time()
        try:
            # connect_timeout=30, read_timeout=60 per chunk
            with _S3_SESSION.get(url, stream=True, timeout=(30, 60)) as r:
                if r.status_code == 404:
                    # Tile doesn't exist on S3 (ocean or outside coverage area).
                    # Record it so future calls skip the S3 round-trip entirely,
                    # and persist so the skip survives service restarts.
                    with _TILES_NOT_ON_S3_LOCK:
                        _TILES_NOT_ON_S3.add(tile_name)
                        _save_tiles_not_on_s3()
                    logger.debug("WorldCover tile not on S3 (ocean/uncovered): %s", tile_name)
                    return None
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
            _mark_tile_known_local(tile_name)
            return tile_path
        except RuntimeError:
            tmp_path.unlink(missing_ok=True)
            raise
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

    tile_name = get_tile_name(lat, lon)
    tile_path = worldcover_dir / tile_name

    if not _is_tile_known_local(tile_name):
        if tile_name in _TILES_NOT_ON_S3:
            return None
        if tile_path.exists():
            _mark_tile_known_local(tile_name)
        elif download_if_missing:
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
                return None
            tile_path = result
            _mark_tile_known_local(tile_name)
        else:
            return None

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
        loss_table: Custom clutter loss values per class ID (None = use defaults)
        fallback_db: Custom fallback loss for unknown classes (None = use default)

    Returns:
        Clutter loss in dB
    """
    use_default_table = loss_table is None and fallback_db is None
    table = loss_table if loss_table is not None else CLUTTER_LOSS_DB
    fb = fallback_db if fallback_db is not None else CLUTTER_FALLBACK_DB

    # Cache key: 3 decimal places ≈ 111 m granularity. Points within 111 m
    # share an entry; this is intentional for coverage grids (step_km >= 5).
    # Use 10000 (≈ 11 m) here only when sub-100 m accuracy is required.
    key = (int(round(lat * 1000)), int(round(lon * 1000)))

    # Only use the cache when the default table is active. Custom loss_table
    # values differ per-run and must not bleed into runs using different tables.
    if use_default_table:
        with _CLUTTER_CACHE_LOCK:
            if key in _CLUTTER_CACHE:
                _CLUTTER_CACHE.move_to_end(key)
                return _CLUTTER_CACHE[key]

    if worldcover_dir is None:
        worldcover_dir = WORLDCOVER_DIR

    cl = fetch_worldcover_class(lat, lon, worldcover_dir)
    if cl is None:
        # Tile unavailable (download failed, ocean, bbox-skipped) — return fallback
        # but do NOT cache it so a later successful download gives the real value.
        return fb

    val = table.get(cl, fb)

    if use_default_table:
        with _CLUTTER_CACHE_LOCK:
            _CLUTTER_CACHE[key] = val
            _CLUTTER_CACHE.move_to_end(key)

    return val


def prefetch_tiles_for_bbox(
    bbox: tuple[float, float, float, float],
    worldcover_dir: Path,
    num_workers: int = 8,
) -> None:
    """Download all WorldCover tiles overlapping bbox in parallel.

    Enumerate every 3°×3° tile that intersects the bounding box and download
    any that are not already present on disk. Uses a thread pool so multiple
    tiles are fetched concurrently. Per-tile locks in ensure_worldcover_tile
    prevent duplicate downloads if called from multiple threads simultaneously.

    Args:
        bbox: (min_lon, min_lat, max_lon, max_lat)
        worldcover_dir: Local directory for storing tiles
        num_workers: Number of concurrent download threads
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    min_lon, min_lat, max_lon, max_lat = bbox

    # Enumerate 3°×3° tile origins that overlap the bbox
    tile_points: list[tuple[float, float]] = []
    lat = int(math.floor(min_lat / 3.0) * 3)
    while lat <= max_lat:
        lon = int(math.floor(min_lon / 3.0) * 3)
        while lon <= max_lon:
            # Pass a point inside the tile; ensure_worldcover_tile derives the
            # tile name from floor(lat/3)*3 and floor(lon/3)*3 internally.
            tile_points.append((lat + 1.5, lon + 1.5))
            lon += 3
        lat += 3

    if not tile_points:
        return

    # Round bbox to 3° grid to make cache key stable across equivalent bboxes
    bbox_key = (
        math.floor(bbox[0] / 3) * 3,
        math.floor(bbox[1] / 3) * 3,
        math.ceil(bbox[2] / 3) * 3,
        math.ceil(bbox[3] / 3) * 3,
    )
    with _PREFETCHED_BBOXES_LOCK:
        if bbox_key in _PREFETCHED_BBOXES:
            return
        _PREFETCHED_BBOXES.add(bbox_key)

    already_local = sum(
        1 for tlat, tlon in tile_points
        if (worldcover_dir / get_tile_name(tlat, tlon)).exists()
           or get_tile_name(tlat, tlon) in _TILES_NOT_ON_S3
    )
    to_download = len(tile_points) - already_local

    logger.info(
        "WorldCover prefetch: %d tiles in bbox, %d already cached, %d to download "
        "(%d workers)",
        len(tile_points),
        already_local,
        to_download,
        num_workers,
    )

    if to_download == 0:
        return

    with ThreadPoolExecutor(
        max_workers=num_workers, thread_name_prefix="wc-prefetch"
    ) as pool:
        futures = {
            pool.submit(ensure_worldcover_tile, tlat, tlon, worldcover_dir, bbox=bbox): (tlat, tlon)
            for tlat, tlon in tile_points
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                tlat, tlon = futures[future]
                logger.warning(
                    "WorldCover tile prefetch failed at (%.1f, %.1f): %s",
                    tlat,
                    tlon,
                    exc,
                )


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
