"""Local HTTP server for racsview with catalogue query API."""

import http.server
import json
import math
import os
import sys
import urllib.parse
import urllib.request

# Config: path to catalogue files (set this env var to point to your local copy of the data)
DATASTORE = os.environ.get(
    "RACSVIEW_DATASTORE", "/Users/mali/repos/datastore"
)

# Catalogue definitions (mirrors SHORTHAND_CATALOGUES from strykowski-lab/dipoletools)
CATALOGUES = {
    "racs-low1": {
        "file": "RACS-low1_sources_25arcsec.csv",
        "ra": "ra", "dec": "dec", "flux": "total_flux_source", "id": "source_id",
    },
    "racs-low2-25": {
        "file": "RACS-low2_sources_25arcsec_patched.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-low2-45": {
        "file": "RACS-low2_sources_45arcsec_patched.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-low3": {
        "file": "RACS-low3_sources.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-low3-scaled": {
        "file": "RACS-low3_sources_scaled.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-mid1-25": {
        "file": "RACS-mid_sources_25arcsec.fits",
        "ra": "ra", "dec": "dec", "flux": "total_flux", "id": "id",
    },
    "racs-mid1-45": {
        "file": "RACS-mid_sources_45arcsec.fits",
        "ra": "RA", "dec": "Dec", "flux": "Total_flux", "id": "Source_ID",
    },
    "racs-high": {
        "file": "RACS-high_sources.fits",
        "ra": "ra", "dec": "dec", "flux": "total_flux", "id": "id",
    },
    "nvss": {
        "file": "full_NVSS_combined_named.dat",
        "ra": "ra", "dec": "dec", "flux": "integrated_flux", "id": "source_name",
    },
    "catwise": {
        "file": "catwise_agns.fits",
        "ra": "ra", "dec": "dec", "flux": "w1", "id": "source_id",
    },
    "local": {
        "file": "local_sources_ned_2mrs.csv",
        "ra": "ra", "dec": "dec", "flux": None, "id": "LS_id",
    },
}

# In-memory cache: catalogue name -> list of dicts
_cache = {}


def _load_catalogue(name):
    """Load a catalogue file into a list of dicts with ra, dec, flux, id."""
    if name in _cache:
        return _cache[name]

    cfg = CATALOGUES[name]
    path = os.path.join(DATASTORE, cfg["file"])

    if not os.path.isfile(path):
        raise FileNotFoundError(f"Catalogue file not found: {path}")

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

    print(f"  Loaded {name}: {len(rows)} sources from {cfg['file']}")
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

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle_list_catalogues(self):
        cats = []
        for name, cfg in CATALOGUES.items():
            path = os.path.join(DATASTORE, cfg["file"])
            cats.append({
                "name": name,
                "file": cfg["file"],
                "available": os.path.isfile(path),
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

        try:
            rows = _load_catalogue(cat_name)
        except FileNotFoundError as e:
            self._send_json({"error": str(e)}, 404)
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = http.server.HTTPServer(("", port), Handler)
    print(f"racsview server on http://localhost:{port}")
    print(f"Datastore: {DATASTORE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
