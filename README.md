# astra-shared
NetSim Astra Shared — domain-agnostic math utilities and user-visible defaults (read-only for all teams)

Consumed by `astra-ui-app` and `astra-radio-engine`. Do not add service-specific logic here.

---

## WorldCover clutter cache

`astra_shared/worldcover.py` maintains a process-global bounded LRU cache of ESA WorldCover
land-cover class lookups. The cache is shared across all users in the same process.

| Variable | Default | Description |
|----------|---------|-------------|
| `CLUTTER_CACHE_MIN_SIZE` | `4688828` (~1.0 GB) | Target size after background eviction |
| `CLUTTER_CACHE_MAX_SIZE` | `7033243` (~1.5 GB) | Inline eviction triggers above this count |
| `CLUTTER_CACHE_EVICT_INTERVAL` | `60` | Seconds between background eviction sweeps |
| `WORLDCOVER_DIR` | `data/worldcover` | Local directory for ESA WorldCover GeoTIFF tiles |

Cache item size is ~229 bytes per entry (64-byte key tuple + 165-byte OrderedDict overhead).

### Tile download

`fetch_worldcover_class` downloads missing tiles from ESA S3 by default
(`download_if_missing=True`). Pass `download_if_missing=False` for offline-only mode.
