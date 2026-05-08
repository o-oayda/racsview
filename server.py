"""Local HTTP server for racsview with catalogue query API."""

import http.server
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

SUPPORTED_CATALOGUE_EXTENSIONS = (".fits", ".csv", ".dat")

# Catalogue definitions (mirrors SHORTHAND_CATALOGUES from strykowski-lab/dipoletools)
CATALOGUES = {
    "racs-low1": {
        "basename": "RACS-low1_sources_25arcsec_allsources.fits",
        "ra": "ra", "dec": "dec", "flux": "total_flux_source", "id": "source_id",
    },
    "racs-low2-25": {
        "basename": "RACS-low2_sources_25arcsec_patched.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-low2-45": {
        "basename": "RACS-low2_sources_45arcsec_patched.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-low3": {
        "basename": "RACS-low3_sources.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-low3-scaled": {
        "basename": "RACS-low3_sources_scaled.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-mid1-25": {
        "basename": "RACS-mid_sources_25arcsec.fits",
        "ra": "ra", "dec": "dec", "flux": "total_flux", "id": "source_id",
    },
    "racs-mid1-45": {
        "basename": "RACS-mid_sources_45arcsec.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-high": {
        "basename": "RACS-high_sources.fits",
        "ra": "ra", "dec": "dec", "flux": "total_flux", "id": "source_id",
    },
    "nvss": {
        "basename": "full_NVSS_combined_named.dat",
        "ra": "ra", "dec": "dec", "flux": "integrated_flux", "id": "source_name",
    },
    "catwise": {
        "basename": "catwise_agns.fits",
        "ra": "ra", "dec": "dec", "flux": "w1", "id": "source_id",
    },
    "local": {
        "basename": "local_sources_ned_2mrs.csv",
        "ra": "ra", "dec": "dec", "flux": None, "id": "LS_id",
    },
}

# In-memory cache: catalogue name -> list of dicts
_cache = {}
_datastore_root = None
_datastore_error = None
_catalogue_resolution = {}
_overlay_state = None

OVERLAY_TILE_WIDTH = 512
OVERLAY_TILE_FORMAT = "png"
OVERLAY_LAYER_NAME = "hpmap-overlay"
OVERLAY_DEFAULT_OPACITY = 0.65
OVERLAY_DEFAULT_COLORMAP = "viridis"
OVERLAY_MIN_RENDER_ORDER = int(math.log2(OVERLAY_TILE_WIDTH))
OVERLAY_COLORMAPS = {
    "grayscale": [
        (0.0, (0, 0, 0)),
        (1.0, (255, 255, 255)),
    ],
    "viridis": [
        (0.0, (68, 1, 84)),
        (0.25, (59, 82, 139)),
        (0.5, (33, 145, 140)),
        (0.75, (94, 201, 98)),
        (1.0, (253, 231, 37)),
    ],
    "plasma": [
        (0.0, (13, 8, 135)),
        (0.25, (126, 3, 168)),
        (0.5, (203, 71, 119)),
        (0.75, (248, 149, 64)),
        (1.0, (240, 249, 33)),
    ],
    "magma": [
        (0.0, (0, 0, 4)),
        (0.25, (80, 18, 123)),
        (0.5, (182, 55, 121)),
        (0.75, (251, 140, 60)),
        (1.0, (252, 253, 191)),
    ],
    "rainbow": [
        (0.0, (150, 0, 90)),
        (0.2, (0, 0, 200)),
        (0.4, (0, 150, 255)),
        (0.6, (0, 200, 0)),
        (0.8, (255, 220, 0)),
        (1.0, (220, 50, 32)),
    ],
}


class OverlayValidationError(Exception):
    """Raised when an overlay map cannot be loaded or rendered."""


def _guess_frame_from_text(text):
    value = str(text or "").lower()
    if any(token in value for token in ("galactic", "_gal", "-gal", " gal", "coordsys-g")):
        return "galactic"
    if any(token in value for token in ("equatorial", "icrs", "_eq", "-eq", " eq", "fk5")):
        return "equatorial"
    return None


def _guess_ordering_from_text(text):
    value = str(text or "").lower()
    if "nested" in value or "nest" in value:
        return "nested"
    if "ring" in value:
        return "ring"
    return None


def _guess_nside_from_text(text):
    match = re.search(r"nside[_-]?(\d+)", str(text or "").lower())
    if match:
        return int(match.group(1))
    return None


def _normalise_frame(value):
    frame = _guess_frame_from_text(value)
    if frame:
        return frame
    if value in ("C", "c"):
        return "equatorial"
    if value in ("G", "g"):
        return "galactic"
    return None


def _normalise_ordering(value):
    ordering = _guess_ordering_from_text(value)
    if ordering:
        return ordering
    return None


def _extract_scalar(value):
    try:
        import numpy as np

        if isinstance(value, np.ndarray):
            if value.shape == ():
                return value.item()
            return None
        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    return value


def _parse_npz_metadata(npz):
    meta = {"frame": None, "ordering": None, "nside": None, "title": None}
    for key in npz.files:
        scalar = _extract_scalar(npz[key])
        if scalar is None:
            continue
        key_lower = key.lower()
        if meta["frame"] is None and key_lower in ("frame", "coordsys", "coord_system", "coord", "csys"):
            meta["frame"] = _normalise_frame(scalar)
        if meta["ordering"] is None and key_lower in ("ordering", "order", "healpix_ordering", "scheme", "nest"):
            if key_lower == "nest" and isinstance(scalar, (bool, int)):
                meta["ordering"] = "nested" if bool(scalar) else "ring"
            else:
                meta["ordering"] = _normalise_ordering(scalar)
        if meta["nside"] is None and key_lower == "nside":
            try:
                meta["nside"] = int(scalar)
            except (TypeError, ValueError):
                pass
        if meta["title"] is None and key_lower in ("title", "name", "label"):
            meta["title"] = str(scalar)
    return meta


def _find_npz_arrays(npz):
    arrays = []
    for key in npz.files:
        value = npz[key]
        if getattr(value, "ndim", None) == 1 and str(getattr(value, "dtype", "")).startswith(
            ("float", "int", "uint")
        ):
            arrays.append(key)
    return arrays


def _select_default_array_key(keys):
    if not keys:
        return None
    preferred = ("map", "hpmap", "healpix_map", "data", "values")
    lower_map = {key.lower(): key for key in keys}
    for name in preferred:
        if name in lower_map:
            return lower_map[name]
    return keys[0]


def _guess_overlay_defaults(path, npz_meta):
    basename = os.path.basename(path)
    stem = os.path.splitext(basename)[0]
    return {
        "frame": npz_meta.get("frame") or _guess_frame_from_text(basename),
        "ordering": npz_meta.get("ordering") or _guess_ordering_from_text(basename),
        "nside": npz_meta.get("nside") or _guess_nside_from_text(basename),
        "title": npz_meta.get("title") or stem,
    }


def _inspect_overlay_map(path, array_key=None):
    import healpy as hp
    import numpy as np

    if not path:
        raise OverlayValidationError("Map path is required")

    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(resolved):
        raise OverlayValidationError(f"Map file does not exist: {resolved}")
    if not os.path.isfile(resolved):
        raise OverlayValidationError(f"Map path is not a file: {resolved}")

    ext = os.path.splitext(resolved)[1].lower()
    if ext not in (".npy", ".npz"):
        raise OverlayValidationError("Overlay map must be a .npy or .npz file")

    arrays = []
    npz_meta = {"frame": None, "ordering": None, "nside": None, "title": None}
    selected_key = None
    if ext == ".npy":
        array = np.load(resolved, allow_pickle=False)
    else:
        with np.load(resolved, allow_pickle=False) as npz:
            arrays = _find_npz_arrays(npz)
            selected_key = array_key or _select_default_array_key(arrays)
            if not selected_key:
                raise OverlayValidationError("No 1D numeric HEALPix array found in NPZ file")
            if selected_key not in npz.files:
                raise OverlayValidationError(f"Array key not found in NPZ file: {selected_key}")
            array = np.array(npz[selected_key], copy=False)
            npz_meta = _parse_npz_metadata(npz)

    if getattr(array, "ndim", None) != 1:
        raise OverlayValidationError("Overlay map must be a 1D array")
    if not str(getattr(array, "dtype", "")).startswith(("float", "int", "uint")):
        raise OverlayValidationError("Overlay map array must be numeric")

    npix = int(array.shape[0])
    try:
        inferred_nside = int(hp.npix2nside(npix))
    except Exception as exc:
        raise OverlayValidationError(f"Array length is not a valid HEALPix npix: {npix}") from exc

    defaults = _guess_overlay_defaults(resolved, npz_meta)
    if defaults["nside"] is not None and int(defaults["nside"]) != inferred_nside:
        raise OverlayValidationError(
            f"Inferred nside {defaults['nside']} does not match array length npix={npix}"
        )

    valid_mask = np.isfinite(array) & (array != hp.UNSEEN)
    if np.any(valid_mask):
        valid_values = np.asarray(array[valid_mask], dtype=np.float32)
        data_min = float(np.min(valid_values))
        data_max = float(np.max(valid_values))
        suggested_min = float(np.percentile(valid_values, 1))
        suggested_max = float(np.percentile(valid_values, 99))
    else:
        data_min = 0.0
        data_max = 0.0
        suggested_min = 0.0
        suggested_max = 1.0

    return {
        "path": resolved,
        "format": ext[1:],
        "array_key": selected_key,
        "array_keys": arrays,
        "npix": npix,
        "nside": inferred_nside,
        "frame": defaults["frame"],
        "ordering": defaults["ordering"],
        "title": defaults["title"],
        "data_min": data_min,
        "data_max": data_max,
        "suggested_min": suggested_min,
        "suggested_max": suggested_max,
    }


def _load_overlay_map(path, array_key, frame, ordering, title, colormap, vmin, vmax, opacity):
    import healpy as hp
    import numpy as np

    inspect = _inspect_overlay_map(path, array_key)
    frame = _normalise_frame(frame) or inspect["frame"]
    ordering = _normalise_ordering(ordering) or inspect["ordering"]
    title = str(title or inspect["title"] or "HEALPix overlay").strip()
    if not frame:
        raise OverlayValidationError("Coordinate frame is required (equatorial or galactic)")
    if not ordering:
        raise OverlayValidationError("HEALPix ordering is required (RING or NESTED)")
    if colormap not in OVERLAY_COLORMAPS:
        raise OverlayValidationError(f"Unknown colormap: {colormap}")

    ext = os.path.splitext(inspect["path"])[1].lower()
    if ext == ".npy":
        raw = np.load(inspect["path"], allow_pickle=False)
    else:
        with np.load(inspect["path"], allow_pickle=False) as npz:
            raw = np.array(npz[inspect["array_key"]], copy=False)
    values = np.asarray(raw, dtype=np.float32)
    if ordering == "ring":
        values = hp.reorder(values, r2n=True)

    valid_mask = np.isfinite(values) & (values != hp.UNSEEN)
    if not np.any(valid_mask):
        raise OverlayValidationError("Overlay map contains no finite HEALPix values")

    if vmin is None:
        vmin = inspect["suggested_min"]
    if vmax is None:
        vmax = inspect["suggested_max"]
    try:
        vmin = float(vmin)
        vmax = float(vmax)
        opacity = float(opacity)
    except (TypeError, ValueError) as exc:
        raise OverlayValidationError("Stretch and opacity values must be numeric") from exc
    if not math.isfinite(vmin) or not math.isfinite(vmax):
        raise OverlayValidationError("Stretch values must be finite numbers")
    if vmax <= vmin:
        raise OverlayValidationError("Max stretch must be greater than min stretch")
    if opacity < 0 or opacity > 1:
        raise OverlayValidationError("Opacity must be between 0 and 1")

    map_order = int(round(math.log2(inspect["nside"])))
    max_tile_order = max(0, map_order - OVERLAY_MIN_RENDER_ORDER)
    return {
        "path": inspect["path"],
        "format": inspect["format"],
        "array_key": inspect["array_key"],
        "array_keys": inspect["array_keys"],
        "map_nested": values,
        "frame": frame,
        "ordering": ordering,
        "title": title,
        "npix": inspect["npix"],
        "nside": inspect["nside"],
        "map_order": map_order,
        "max_tile_order": max_tile_order,
        "colormap": colormap,
        "vmin": vmin,
        "vmax": vmax,
        "opacity": opacity,
        "render_cache": {},
        "data_min": inspect["data_min"],
        "data_max": inspect["data_max"],
        "suggested_min": inspect["suggested_min"],
        "suggested_max": inspect["suggested_max"],
    }


def _overlay_response_payload():
    if _overlay_state is None:
        return None
    return {
        "layer_name": OVERLAY_LAYER_NAME,
        "title": _overlay_state["title"],
        "frame": _overlay_state["frame"],
        "ordering": _overlay_state["ordering"],
        "map_order": _overlay_state["map_order"],
        "max_order": _overlay_state["max_tile_order"],
        "colormap": _overlay_state["colormap"],
        "vmin": _overlay_state["vmin"],
        "vmax": _overlay_state["vmax"],
        "opacity": _overlay_state["opacity"],
        "path": _overlay_state["path"],
        "array_key": _overlay_state["array_key"],
        "hips_url": "/api/overlay/hips/current",
    }


def _get_overlay_values_for_order(order):
    import healpy as hp
    import numpy as np

    if _overlay_state is None:
        raise OverlayValidationError("No overlay is loaded")
    cache = _overlay_state["render_cache"]
    if order in cache:
        return cache[order]
    nside_out = 1 << order
    cache[order] = hp.ud_grade(
        _overlay_state["map_nested"],
        nside_out=nside_out,
        order_in="NESTED",
        order_out="NESTED",
        dtype=np.float32,
    )
    return cache[order]


def _interpolate_colormap(colormap_name, values):
    import numpy as np

    stops = OVERLAY_COLORMAPS[colormap_name]
    positions = np.array([stop[0] for stop in stops], dtype=np.float32)
    colors = np.array([stop[1] for stop in stops], dtype=np.float32)
    flat = values.reshape(-1)
    rgb = np.empty((flat.size, 3), dtype=np.uint8)
    for channel in range(3):
        rgb[:, channel] = np.interp(flat, positions, colors[:, channel]).astype(np.uint8)
    return rgb.reshape(values.shape + (3,))


def _render_overlay_tile(tile_order, tile_pix):
    import healpy as hp
    import numpy as np

    if _overlay_state is None:
        raise OverlayValidationError("No overlay is loaded")
    if tile_order < 0 or tile_order > _overlay_state["max_tile_order"]:
        raise OverlayValidationError(f"Tile order out of range: {tile_order}")

    render_order = tile_order + OVERLAY_MIN_RENDER_ORDER
    render_values = _get_overlay_values_for_order(render_order)
    nside_tile = 1 << tile_order
    nside_render = 1 << render_order

    parent_x, parent_y, face = hp.pix2xyf(nside_tile, tile_pix, nest=True)
    x_local = np.arange(OVERLAY_TILE_WIDTH, dtype=np.int64)
    y_local = np.arange(OVERLAY_TILE_WIDTH - 1, -1, -1, dtype=np.int64)
    x_grid, y_grid = np.meshgrid(
        (parent_x << OVERLAY_MIN_RENDER_ORDER) + x_local,
        (parent_y << OVERLAY_MIN_RENDER_ORDER) + y_local,
        indexing="xy",
    )
    render_pix = hp.xyf2pix(nside_render, x_grid, y_grid, face, nest=True)
    values = render_values[render_pix]

    mask = np.isfinite(values) & (values != hp.UNSEEN)
    if not np.any(mask):
        rgba = np.zeros((OVERLAY_TILE_WIDTH, OVERLAY_TILE_WIDTH, 4), dtype=np.uint8)
        return _encode_png_rgba(rgba)

    span = _overlay_state["vmax"] - _overlay_state["vmin"]
    scaled = np.clip((values - _overlay_state["vmin"]) / span, 0.0, 1.0)
    rgb = _interpolate_colormap(_overlay_state["colormap"], scaled)
    alpha = np.where(mask, 255, 0).astype(np.uint8)
    rgba = np.dstack((rgb, alpha))
    return _encode_png_rgba(rgba)


def _encode_png_chunk(tag, payload):
    return (
        struct.pack("!I", len(payload))
        + tag
        + payload
        + struct.pack("!I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _encode_png_rgba(rgba):
    height, width, depth = rgba.shape
    if depth != 4:
        raise ValueError("Expected RGBA image data")
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = []
    for row in rgba:
        rows.append(b"\x00" + row.tobytes())
    compressed = zlib.compress(b"".join(rows), level=6)
    return signature + _encode_png_chunk(b"IHDR", ihdr) + _encode_png_chunk(
        b"IDAT", compressed
    ) + _encode_png_chunk(b"IEND", b"")


class CatalogueResolutionError(Exception):
    """Raised when a catalogue cannot be resolved to a usable file."""

    def __init__(self, resolution):
        self.resolution = resolution
        super().__init__(resolution["message"])


def _get_datastore_root():
    """Resolve RACSVIEW_DATASTORE to an absolute directory path."""
    raw_path = os.environ.get("RACSVIEW_DATASTORE")
    if not raw_path:
        return None, "RACSVIEW_DATASTORE is not set"

    path = os.path.abspath(os.path.expanduser(raw_path))
    if not os.path.exists(path):
        return None, f"RACSVIEW_DATASTORE does not exist: {path}"
    if not os.path.isdir(path):
        return None, f"RACSVIEW_DATASTORE is not a directory: {path}"
    return path, None


def _set_datastore_root(path):
    """Apply a new datastore root for the current server process."""
    global _datastore_root, _datastore_error, _cache

    raw_path = str(path or "").strip()
    if not raw_path:
        _datastore_root = None
        _datastore_error = "RACSVIEW_DATASTORE is not set"
        _cache = {}
        _resolve_catalogues()
        return False, _datastore_error

    os.environ["RACSVIEW_DATASTORE"] = raw_path
    _datastore_root, _datastore_error = _get_datastore_root()
    _cache = {}
    _resolve_catalogues()
    return _datastore_error is None, _datastore_error


def _pick_directory_dialog():
    """Open a native directory picker on the local machine running the server."""
    if sys.platform == "darwin":
        script = (
            'set selectedFolder to choose folder with prompt "Select the racsview datastore directory"\n'
            'POSIX path of selectedFolder'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip(), None
        return None, "Directory selection cancelled"

    if sys.platform.startswith("linux"):
        candidates = [
            ["zenity", "--file-selection", "--directory", "--title=Select racsview datastore"],
            ["qarma", "--file-selection", "--directory", "--title=Select racsview datastore"],
            ["kdialog", "--getexistingdirectory", os.path.expanduser("~"), "Select racsview datastore"],
        ]
        for command in candidates:
            if shutil.which(command[0]) is None:
                continue
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                return result.stdout.strip(), None
            return None, "Directory selection cancelled"
        return None, "No supported native directory picker found"

    if sys.platform.startswith("win"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
            '$dialog.Description = "Select the racsview datastore directory"; '
            "if ($dialog.ShowDialog() -eq 'OK') { Write-Output $dialog.SelectedPath }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), None
        return None, "Directory selection cancelled"

    return None, f"Native directory picker is not supported on platform: {sys.platform}"


def _scan_datastore(root):
    """Return basename -> matching absolute paths for supported catalogue files."""
    index = {}
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if not filename.lower().endswith(SUPPORTED_CATALOGUE_EXTENSIONS):
                continue
            path = os.path.join(dirpath, filename)
            index.setdefault(filename, []).append(path)
    return index


def _make_resolution(name, cfg, status, *, path=None, matches=None, message=""):
    return {
        "name": name,
        "basename": cfg["basename"],
        "available": status == "ok",
        "status": status,
        "path": path,
        "matches": matches or [],
        "message": message,
    }


def _resolve_catalogues():
    """Build catalogue resolution state from the configured datastore root."""
    global _catalogue_resolution

    if _datastore_error:
        _catalogue_resolution = {
            name: _make_resolution(name, cfg, "misconfigured", message=_datastore_error)
            for name, cfg in CATALOGUES.items()
        }
        return

    index = _scan_datastore(_datastore_root)
    resolution = {}
    for name, cfg in CATALOGUES.items():
        matches = sorted(index.get(cfg["basename"], []))
        if not matches:
            resolution[name] = _make_resolution(
                name,
                cfg,
                "missing",
                message=f"Canonical file not found under datastore root: {cfg['basename']}",
            )
        elif len(matches) > 1:
            resolution[name] = _make_resolution(
                name,
                cfg,
                "ambiguous",
                matches=matches,
                message=f"Multiple matching files found for {cfg['basename']}",
            )
        else:
            resolution[name] = _make_resolution(
                name,
                cfg,
                "ok",
                path=matches[0],
                matches=matches,
                message="resolved",
            )
    _catalogue_resolution = resolution


def _get_catalogue_resolution(name):
    resolution = _catalogue_resolution.get(name)
    if resolution is None:
        raise KeyError(f"Unknown catalogue: {name}")
    return resolution


def _print_catalogue_summary():
    print(f"Datastore: {_datastore_root or '(unconfigured)'}")
    if _datastore_error:
        print(f"Datastore status: {_datastore_error}")
    else:
        indexed_paths = set()
        for resolution in _catalogue_resolution.values():
            indexed_paths.update(resolution["matches"])
        print(
            f"Scanned {len(indexed_paths)} supported files "
            f"({', '.join(SUPPORTED_CATALOGUE_EXTENSIONS)})"
        )

    for name, resolution in _catalogue_resolution.items():
        if resolution["status"] == "ok":
            detail = resolution["path"]
        elif resolution["status"] == "ambiguous":
            detail = f"{len(resolution['matches'])} matches"
        else:
            detail = resolution["message"]
        print(f"  {name}: {resolution['status']} -> {detail}")


def _load_catalogue(name):
    """Load a catalogue file into a list of dicts with ra, dec, flux, id."""
    if name in _cache:
        return _cache[name]

    resolution = _get_catalogue_resolution(name)
    if resolution["status"] != "ok":
        raise CatalogueResolutionError(resolution)

    cfg = CATALOGUES[name]
    path = resolution["path"]

    ext = os.path.splitext(path)[1].lower()
    rows = []

    if ext == ".fits":
        import fitsio
        # Read first binary table HDU
        f = fitsio.FITS(path)
        hdu = None
        for h in f:
            if h.has_data() and h.get_exttype() == 'BINARY_TBL':
                hdu = h
                break
        if hdu is None:
            raise ValueError(f"No binary table HDU in {path}")
        t = hdu.read()
        ra_col = cfg["ra"]
        dec_col = cfg["dec"]
        flux_col = cfg["flux"]
        id_col = cfg["id"]
        ra_arr = t[ra_col]
        dec_arr = t[dec_col]
        flux_arr = t[flux_col] if flux_col else None
        id_arr = t[id_col] if id_col else None
        for i in range(len(ra_arr)):
            try:
                ra = float(ra_arr[i])
                dec = float(dec_arr[i])
            except (ValueError, TypeError):
                continue
            flux = float(flux_arr[i]) if flux_arr is not None else None
            sid = str(id_arr[i]).strip() if id_arr is not None else ""
            rows.append({"ra": ra, "dec": dec, "flux": flux, "id": sid})

    elif ext in (".csv", ".dat"):
        import csv
        delimiter = "," if ext == ".csv" else None  # None = whitespace
        with open(path, "r") as f:
            if delimiter:
                reader = csv.DictReader(f)
            else:
                # space/tab delimited
                first_line = f.readline().strip()
                headers = first_line.split()
                reader = csv.DictReader(f, fieldnames=headers, delimiter="\t")
                # Try to detect delimiter
                f.seek(0)
                sample = f.readline()
                f.seek(0)
                if "\t" in sample:
                    reader = csv.DictReader(f, delimiter="\t")
                else:
                    # whitespace delimited; merge tokens spanning a quoted field
                    f.seek(0)
                    header_line = f.readline().strip()
                    headers = header_line.split()
                    n_expected = len(headers)
                    reader = []
                    for line in f:
                        parts = line.split()
                        if len(parts) > n_expected:
                            # Merge tokens between unbalanced double quotes into one field
                            merged = []
                            buf = None
                            for tok in parts:
                                if buf is not None:
                                    buf.append(tok)
                                    if tok.endswith('"'):
                                        merged.append(" ".join(buf).strip('"'))
                                        buf = None
                                elif tok.startswith('"') and not tok.endswith('"'):
                                    buf = [tok]
                                else:
                                    merged.append(tok.strip('"'))
                            if buf is not None:
                                merged.append(" ".join(buf).strip('"'))
                            parts = merged
                        if len(parts) == n_expected:
                            reader.append(dict(zip(headers, parts)))

            ra_col = cfg["ra"]
            dec_col = cfg["dec"]
            flux_col = cfg["flux"]
            id_col = cfg["id"]
            for row in reader:
                try:
                    ra = float(row[ra_col])
                    dec = float(row[dec_col])
                except (ValueError, TypeError, KeyError):
                    continue
                flux = None
                if flux_col and flux_col in row:
                    try:
                        flux = float(row[flux_col])
                    except (ValueError, TypeError):
                        pass
                sid = str(row.get(id_col, ""))
                rows.append({"ra": ra, "dec": dec, "flux": flux, "id": sid})

    print(f"  Loaded {name}: {len(rows)} sources from {path}")
    _cache[name] = rows
    return rows


def _cone_search(rows, ra_center, dec_center, radius_deg, min_flux=None):
    """Filter rows to those within radius_deg of (ra_center, dec_center)."""
    deg2rad = math.pi / 180.0
    ra0 = ra_center * deg2rad
    dec0 = dec_center * deg2rad
    cos_radius = math.cos(radius_deg * deg2rad)
    sin_dec0 = math.sin(dec0)
    cos_dec0 = math.cos(dec0)

    results = []
    for row in rows:
        if min_flux is not None and row["flux"] is not None:
            if row["flux"] < min_flux:
                continue
        ra_r = row["ra"] * deg2rad
        dec_r = row["dec"] * deg2rad
        # Spherical law of cosines
        cos_sep = (sin_dec0 * math.sin(dec_r) +
                   cos_dec0 * math.cos(dec_r) * math.cos(ra0 - ra_r))
        if cos_sep >= cos_radius:
            results.append(row)
    return results


class Handler(http.server.SimpleHTTPRequestHandler):
    # Allowed remote HiPS base URLs for proxying.
    PROXY_ALLOWED = {
        "RACShigh1_I1": "https://www.atnf.csiro.au/research/RACS/RACShigh1_I1",
        "RACSlow3_I1": "https://www.atnf.csiro.au/research/RACS/RACSlow3_I1",
    }

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/catalogues":
            self._handle_list_catalogues()
        elif parsed.path == "/api/overlay/status":
            self._handle_overlay_status()
        elif parsed.path == "/api/sources":
            self._handle_sources(parsed.query)
        elif parsed.path == "/api/healpix/grid":
            self._handle_healpix_grid(parsed.query)
        elif parsed.path == "/api/overlay/hips/current/properties":
            self._handle_overlay_properties()
        elif parsed.path.startswith("/api/overlay/hips/current/"):
            self._handle_overlay_tile(parsed.path)
        elif parsed.path.startswith("/proxy/hips/"):
            self._handle_hips_proxy(parsed)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/config/datastore":
            self._handle_set_datastore()
            return
        if parsed.path == "/api/config/datastore/pick":
            self._handle_pick_datastore()
            return
        if parsed.path == "/api/overlay/inspect":
            self._handle_overlay_inspect()
            return
        if parsed.path == "/api/overlay/load":
            self._handle_overlay_load()
            return
        if parsed.path == "/api/overlay/clear":
            self._handle_overlay_clear()
            return
        self.send_error(404)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _handle_list_catalogues(self):
        cats = []
        for name, cfg in CATALOGUES.items():
            resolution = _get_catalogue_resolution(name)
            cats.append({
                "name": name,
                "basename": cfg["basename"],
                "available": resolution["available"],
                "status": resolution["status"],
                "path": resolution["path"],
                "matches": resolution["matches"],
                "message": resolution["message"],
            })
        self._send_json(cats)

    def _handle_overlay_status(self):
        self._send_json({"overlay": _overlay_response_payload()})

    def _handle_overlay_inspect(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json({"error": "Request body must be valid JSON"}, 400)
            return

        try:
            info = _inspect_overlay_map(body.get("path"), body.get("array_key"))
        except OverlayValidationError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        self._send_json({"ok": True, "overlay": info})

    def _handle_overlay_load(self):
        global _overlay_state

        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json({"error": "Request body must be valid JSON"}, 400)
            return

        try:
            _overlay_state = _load_overlay_map(
                body.get("path"),
                body.get("array_key"),
                body.get("frame"),
                body.get("ordering"),
                body.get("title"),
                body.get("colormap") or OVERLAY_DEFAULT_COLORMAP,
                body.get("vmin"),
                body.get("vmax"),
                body.get("opacity", OVERLAY_DEFAULT_OPACITY),
            )
        except OverlayValidationError as exc:
            self._send_json({"error": str(exc)}, 400)
            return

        self._send_json({"ok": True, "overlay": _overlay_response_payload()})

    def _handle_overlay_clear(self):
        global _overlay_state

        _overlay_state = None
        self._send_json({"ok": True})

    def _handle_sources(self, query_string):
        params = urllib.parse.parse_qs(query_string)
        cat_name = params.get("catalogue", [None])[0]
        ra = params.get("ra", [None])[0]
        dec = params.get("dec", [None])[0]
        radius = params.get("radius", [None])[0]
        min_flux = params.get("min_flux", [None])[0]

        if not cat_name or cat_name not in CATALOGUES:
            self._send_json({"error": f"Unknown catalogue: {cat_name}"}, 400)
            return

        try:
            ra = float(ra)
            dec = float(dec)
            radius = float(radius)
        except (TypeError, ValueError):
            self._send_json({"error": "ra, dec, radius must be numbers"}, 400)
            return

        if min_flux is not None:
            try:
                min_flux = float(min_flux)
                if min_flux <= 0:
                    min_flux = None
            except (ValueError, TypeError):
                min_flux = None

        resolution = _get_catalogue_resolution(cat_name)
        if resolution["status"] == "missing":
            self._send_json({"error": resolution["message"]}, 404)
            return
        if resolution["status"] == "ambiguous":
            self._send_json(
                {
                    "error": resolution["message"],
                    "matches": resolution["matches"],
                },
                409,
            )
            return
        if resolution["status"] == "misconfigured":
            self._send_json({"error": resolution["message"]}, 500)
            return

        try:
            rows = _load_catalogue(cat_name)
        except CatalogueResolutionError as e:
            status = {
                "missing": 404,
                "ambiguous": 409,
                "misconfigured": 500,
            }.get(e.resolution["status"], 500)
            payload = {"error": e.resolution["message"]}
            if e.resolution["matches"]:
                payload["matches"] = e.resolution["matches"]
            self._send_json(payload, status)
            return
        except Exception as e:
            self._send_json({"error": f"Failed to load catalogue: {e}"}, 500)
            return

        results = _cone_search(rows, ra, dec, radius, min_flux)
        self._send_json({
            "catalogue": cat_name,
            "ra": ra, "dec": dec, "radius": radius,
            "count": len(results),
            "sources": results,
        })

    def _handle_set_datastore(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json({"error": "Request body must be valid JSON"}, 400)
            return

        path = body.get("path")
        ok, error = _set_datastore_root(path)
        if not ok:
            self._send_json(
                {
                    "ok": False,
                    "error": error,
                    "datastore": _datastore_root,
                },
                400,
            )
            return

        print("Datastore updated via UI.")
        _print_catalogue_summary()
        self._send_json(
            {
                "ok": True,
                "datastore": _datastore_root,
            }
        )

    def _handle_pick_datastore(self):
        path, error = _pick_directory_dialog()
        if path is None:
            self._send_json({"ok": False, "error": error}, 400)
            return

        ok, config_error = _set_datastore_root(path)
        if not ok:
            self._send_json(
                {
                    "ok": False,
                    "error": config_error,
                    "datastore": _datastore_root,
                },
                400,
            )
            return

        print("Datastore updated via native picker.")
        _print_catalogue_summary()
        self._send_json(
            {
                "ok": True,
                "datastore": _datastore_root,
            }
        )

    def _handle_healpix_grid(self, query_string):
        import healpy as hp
        import numpy as np
        from astropy.coordinates import SkyCoord
        import astropy.units as u

        params = urllib.parse.parse_qs(query_string)
        ra = float(params.get("ra", [0])[0])
        dec = float(params.get("dec", [0])[0])
        fov = float(params.get("fov", [10])[0])
        nside = int(params.get("nside", [64])[0])
        frame = params.get("frame", ["G"])[0]  # G or C

        # Convert view center to the grid's coordinate frame
        if frame == "G":
            c = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
            center_theta = np.radians(90 - c.galactic.b.deg)
            center_phi = np.radians(c.galactic.l.deg)
        else:
            center_theta = np.radians(90 - dec)
            center_phi = np.radians(ra)

        # Query pixels in visible cone (pad fov a bit)
        vec = hp.ang2vec(center_theta, center_phi)
        radius_rad = np.radians(fov * 0.75)
        pixels = hp.query_disc(nside, vec, radius_rad)

        result = []
        for pix in pixels:
            # Get boundary vertices (step=4 = 4 pts per edge = 16 total)
            vecs = hp.boundaries(nside, int(pix), step=4)
            thetas, phis = hp.vec2ang(vecs.T)
            lons = np.degrees(phis)
            lats = 90.0 - np.degrees(thetas)

            # Convert boundary to ra,dec for Aladin drawing
            if frame == "G":
                sc = SkyCoord(l=lons * u.deg, b=lats * u.deg, frame="galactic")
                vra = sc.icrs.ra.deg.tolist()
                vdec = sc.icrs.dec.deg.tolist()
            else:
                vra = lons.tolist()
                vdec = lats.tolist()

            # Pixel center in grid frame
            ct, cp = hp.pix2ang(nside, int(pix))
            clon = float(np.degrees(cp))
            clat = float(90.0 - np.degrees(ct))

            result.append({
                "pix": int(pix),
                "vra": vra,
                "vdec": vdec,
                "clon": clon,
                "clat": clat,
            })

        self._send_json(result)

    def _handle_overlay_properties(self):
        if _overlay_state is None:
            self.send_error(404, "No overlay is loaded")
            return

        initial_ra = 180 if _overlay_state["frame"] == "equatorial" else 0
        initial_dec = 0
        frame = _overlay_state["frame"]
        properties = "\n".join(
            [
                f"creator_did = ivo://local/racsview/{OVERLAY_LAYER_NAME}",
                f"obs_title = {_overlay_state['title']}",
                "hips_version = 1.4",
                f"hips_frame = {frame}",
                f"hips_order = {_overlay_state['max_tile_order']}",
                "hips_order_min = 0",
                f"hips_tile_width = {OVERLAY_TILE_WIDTH}",
                f"hips_tile_format = {OVERLAY_TILE_FORMAT}",
                "dataproduct_type = image",
                "hips_status = public master clonableOnce",
                f"hips_initial_ra = {initial_ra}",
                f"hips_initial_dec = {initial_dec}",
                "hips_initial_fov = 90",
                "",
            ]
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(properties)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(properties)

    def _handle_overlay_tile(self, path):
        if _overlay_state is None:
            self.send_error(404, "No overlay is loaded")
            return

        path = re.sub(r"/+", "/", path)

        match = re.fullmatch(
            r"/api/overlay/hips/current/Norder(\d+)/Dir\d+/Npix(\d+)\.png",
            path,
        )
        if not match:
            self.send_error(404, "Unknown overlay tile path")
            return

        tile_order = int(match.group(1))
        tile_pix = int(match.group(2))
        try:
            body = _render_overlay_tile(tile_order, tile_pix)
        except OverlayValidationError as exc:
            self.send_error(400, str(exc))
            return
        except Exception as exc:
            self.send_error(500, f"Overlay tile render failed: {exc}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_hips_proxy(self, parsed):
        """Reverse-proxy HiPS tile requests to bypass CORS restrictions.

        URL pattern: /proxy/hips/<key>/<remainder>
        e.g. /proxy/hips/RACShigh1_I1/properties
             /proxy/hips/RACShigh1_I1/Norder3/Dir0/Npix300.png
        """
        parts = parsed.path.split("/", 4)  # ['', 'proxy', 'hips', key, remainder]
        if len(parts) < 5:
            self.send_error(400, "Bad proxy path")
            return
        key = parts[3]
        remainder = parts[4].lstrip("/")

        base_url = self.PROXY_ALLOWED.get(key)
        if not base_url:
            self.send_error(403, f"Unknown HiPS key: {key}")
            return

        remote_url = f"{base_url}/{remainder}" if remainder else base_url
        if parsed.query:
            remote_url = f"{remote_url}?{parsed.query}"
        try:
            req = urllib.request.Request(
                remote_url,
                headers={"User-Agent": "racsview/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "application/octet-stream")
                self.send_response(resp.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                cache_control = resp.headers.get("Cache-Control")
                if cache_control:
                    self.send_header("Cache-Control", cache_control)
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_error(e.code, str(e.reason))
        except Exception as e:
            self.send_error(502, f"Proxy error: {e}")

    def log_message(self, format, *args):
        # Quieter logging
        sys.stderr.write(f"  {self.address_string()} - {format % args}\n")


def main():
    global _datastore_root, _datastore_error

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    _datastore_root, _datastore_error = _get_datastore_root()
    _resolve_catalogues()
    server = http.server.HTTPServer(("", port), Handler)
    print(f"racsview server on http://localhost:{port}")
    _print_catalogue_summary()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
