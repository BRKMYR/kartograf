"""Region presets and dataset folders, shared by the fetch script and the app.

Every fetch writes into its own folder under data/overture/, named after the
preset (or the bounding box), so a small Berlin dataset and the full Germany
dataset can live side by side and never overwrite each other.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

DATA_ROOT = Path(__file__).parent / "data" / "overture"

# xmin, ymin, xmax, ymax in WGS84 degrees.
PRESETS: dict[str, dict] = {
    "germany":            {"label": "Germany",             "bbox": (5.87, 47.27, 15.04, 55.06)},
    "bayern":             {"label": "Bayern",              "bbox": (8.97, 47.27, 13.84, 50.56)},
    "nrw":                {"label": "Nordrhein-Westfalen", "bbox": (5.86, 50.32, 9.46, 52.53)},
    "berlin-brandenburg": {"label": "Berlin-Brandenburg",  "bbox": (11.26, 51.36, 14.77, 53.56)},
    "berlin":             {"label": "Berlin",              "bbox": (13.08, 52.33, 13.77, 52.68)},
    "saarland":           {"label": "Saarland",            "bbox": (6.35, 49.11, 7.40, 49.64)},
}

EXCLUDED_DIRS = {"archive"}


def scope_name(preset: str | None, bbox: tuple[float, float, float, float]) -> str:
    """Folder name for a fetch. A preset keeps its name only if the bbox is
    unchanged; a custom bbox gets a name derived from its coordinates."""
    if preset and tuple(PRESETS[preset]["bbox"]) == tuple(bbox):
        return preset
    for name, p in PRESETS.items():
        if tuple(p["bbox"]) == tuple(bbox):
            return name
    return "bbox_" + "_".join(f"{v:g}".replace("-", "m") for v in bbox)


def label(name: str) -> str:
    """Presets are rectangles, so everything except Germany is an area that
    also clips neighbouring states. The label says so."""
    if name == "germany":
        return PRESETS[name]["label"]
    if name in PRESETS:
        return f"{PRESETS[name]['label']} area"
    m = re.match(r"bbox_(.+)", name)
    return f"Custom area ({m.group(1).replace('_', ', ')})" if m else name


def dataset_dirs(root: Path = DATA_ROOT) -> list[Path]:
    """Folders that hold at least one non-empty release statistics file."""
    if not root.exists():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if d.name in EXCLUDED_DIRS or d.name.startswith("."):
            continue
        if any(f.stat().st_size > 0 for f in d.glob("stats_*.csv")):
            out.append(d)
    # Germany first when present, it is the reference dataset.
    return sorted(out, key=lambda d: (d.name != "germany", d.name))


def roads_path(directory: Path, release: str) -> Path | None:
    """Compressed map file if present, else a legacy uncompressed one."""
    for name in (f"roads_{release}.geojson.gz", f"roads_{release}.geojson"):
        p = directory / name
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def read_bytes_maybe_gzip(path: Path) -> bytes:
    raw = path.read_bytes()
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
