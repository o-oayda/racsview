"""Local HTTP server for racsview with catalogue query API."""

import http.server
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

SUPPORTED_CATALOGUE_EXTENSIONS = (".fits", ".csv", ".dat")

# Catalogue definitions (mirrors SHORTHAND_CATALOGUES from strykowski-lab/dipoletools)
CATALOGUES = {
    "racs-low1": {
        "basename": "RACS-low1_sources_25arcsec.csv",
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
        "ra": "ra", "dec": "dec", "flux": "total_flux", "id": "id",
    },
    "racs-mid1-45": {
        "basename": "RACS-mid_sources_45arcsec.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-high": {
        "basename": "RACS-high_sources.fits",
        "ra": "ra", "dec": "dec", "flux": "total_flux", "id": "id",
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
                    # whitespace delimited - use split
                    f.seek(0)
                    header_line = f.readline().strip()
                    headers = header_line.split()
                    reader = []
                    for line in f:
                        vals = line.strip().split()
                        if len(vals) == len(headers):
                            reader.append(dict(zip(headers, vals)))

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
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/catalogues":
            self._handle_list_catalogues()
        elif parsed.path == "/api/sources":
            self._handle_sources(parsed.query)
        elif parsed.path == "/api/healpix/grid":
            self._handle_healpix_grid(parsed.query)
        elif parsed.path.startswith("/proxy/hips/"):
            self._handle_hips_proxy(parsed.path)
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

    # Allowed remote HiPS base URLs for proxying
    PROXY_ALLOWED = {
        "RACShigh1_I1": "https://www.atnf.csiro.au/research/RACS/RACShigh1_I1",
    }

    def _handle_hips_proxy(self, path):
        """Reverse-proxy HiPS tile requests to bypass CORS restrictions.

        URL pattern: /proxy/hips/<key>/<remainder>
        e.g. /proxy/hips/RACShigh1_I1/properties
             /proxy/hips/RACShigh1_I1/Norder3/Dir0/Npix300.png
        """
        parts = path.split("/", 4)  # ['', 'proxy', 'hips', key, remainder]
        if len(parts) < 5:
            self.send_error(400, "Bad proxy path")
            return
        key = parts[3]
        remainder = parts[4]

        base_url = self.PROXY_ALLOWED.get(key)
        if not base_url:
            self.send_error(403, f"Unknown HiPS key: {key}")
            return

        remote_url = f"{base_url}/{remainder}"
        try:
            req = urllib.request.Request(remote_url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
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
