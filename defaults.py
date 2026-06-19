"""
defaults.py

User-visible RF default values shared across Astra services.
Import these instead of hard-coding magic numbers in individual services.
"""
base = Path(__file__).parent.parent
sys.path.insert(0, str(base / "astra-shared"))
WORLDCOVER_DIR = Path(__file__).parent.parent / "astra-data" / "data" / "worldcover"

DEFAULT_EIRP_DBW: float = 70.0
DEFAULT_RX_GAIN_DBI: float = 0.0
DEFAULT_BANDWIDTH_HZ: float = 10