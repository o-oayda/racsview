"use strict";

// CDS MocServer query endpoint (HiPS registry/aggregator)
const MOCSERVER = "https://alasky.cds.unistra.fr/MocServer/query";

// HiPS survey IDs available on CDS/CASDA
const IDS = {
  low: "CSIRO/P/RACS/low/I",
  mid: "CSIRO/P/RACS/mid/I",
  // RACS-high is not in CDS registry; loaded directly from ATNF
};

// Direct HiPS definitions (not in CDS MocServer — proxied through server.py)
const DIRECT_HIPS = {
  high: {
    url: "/proxy/hips/RACShigh1_I1",
    name: "RACS-high (1655.5 MHz)",
    cooFrame: "equatorial",
    maxOrder: 9,
    imgFormat: "png",
  },
};

const CATALOG_MIN_RADIUS_DEG = 0.2;
const CATALOG_MAX_RADIUS_DEG = 4.0;

// Colour per catalogue for visual distinction
const CATALOGUE_COLORS = [
  "#f59e0b", "#22c55e", "#3b82f6", "#ef4444", "#a855f7",
  "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
];

// DOM refs
const statusEl = document.getElementById("status");
const minFluxEl = document.getElementById("minFlux");
const sourceSizeEl = document.getElementById("sourceSize");
const selImageEl = document.getElementById("selImage");
const selCatalogueEl = document.getElementById("selCatalogue");
const btnLoadImgEl = document.getElementById("btnLoadImg");
const btnLoadSrcEl = document.getElementById("btnLoadSrc");
const btnConfigDataEl = document.getElementById("btnConfigData");
const btnCircleEl = document.getElementById("btnCircle");
const circleRadiusEl = document.getElementById("circleRadius");
const btnGridEl = document.getElementById("btnGrid");
const gridNsideEl = document.getElementById("gridNside");

const setStatus = (s) => { statusEl.textContent = s; };

let aladin = null;

// Get HEALPix frame from Aladin's current coordinate frame.
// Try aladin.getFrame() first; fall back to reading the cooFrame option.
function getGridFrame() {
  let f = "";
  try {
    f = aladin.getFrame() || "";
  } catch (_) {
    try { f = aladin.options.cooFrame || ""; } catch (_) {}
  }
  f = String(f).toUpperCase();
  return f.includes("GAL") ? "G" : "C";
}

// ===========================================================================
// Coordinate conversion (equatorial <-> galactic)
// ===========================================================================

const DEG = Math.PI / 180;
const RAD = 180 / Math.PI;

// IAU galactic pole / origin constants
const RA_NGP  = 192.8594813 * DEG;
const DEC_NGP =  27.1282511 * DEG;
const L_NCP   = 122.9319185 * DEG;

const _sinDecNGP = Math.sin(DEC_NGP);
const _cosDecNGP = Math.cos(DEC_NGP);

function radec2gal(ra, dec) {
  const raR = ra * DEG, decR = dec * DEG;
  const dra = raR - RA_NGP;
  const sinDec = Math.sin(decR), cosDec = Math.cos(decR);
  const sinB = sinDec * _sinDecNGP + cosDec * _cosDecNGP * Math.cos(dra);
  const b = Math.asin(sinB);
  const y = cosDec * Math.sin(dra);
  const x = sinDec * _cosDecNGP - cosDec * _sinDecNGP * Math.cos(dra);
  let l = L_NCP - Math.atan2(y, x);
  l = ((l * RAD) % 360 + 360) % 360;
  return [l, b * RAD];
}

function gal2radec(l, b) {
  const lR = l * DEG, bR = b * DEG;
  const dl = lR - L_NCP;
  const sinB = Math.sin(bR), cosB = Math.cos(bR);
  const sinDec = sinB * _sinDecNGP + cosB * _cosDecNGP * Math.cos(dl);
  const decR = Math.asin(sinDec);
  const y = cosB * Math.sin(dl);
  const x = sinB * _cosDecNGP - cosB * _sinDecNGP * Math.cos(dl);
  let ra = Math.atan2(y, x) + RA_NGP;
  ra = ((ra * RAD) % 360 + 360) % 360;
  return [ra, decR * RAD];
}

// ===========================================================================
// HEALPix ang2pix (RING scheme)
// ===========================================================================

function ang2pix_ring(nside, theta, phi) {
  const nside2 = nside * nside;
  const npix = 12 * nside2;
  const z = Math.cos(theta);
  const za = Math.abs(z);
  let tt = ((phi % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
  tt /= (Math.PI / 2);  // in [0, 4)

  if (za <= 2.0 / 3.0) {
    // Equatorial region
    const temp1 = nside * (0.5 + tt);
    const temp2 = nside * z * 0.75;
    const jp = Math.floor(temp1 - temp2);
    const jm = Math.floor(temp1 + temp2);
    const ir = nside + 1 + jp - jm;
    const kshift = (ir & 1) === 0 ? 1 : 0;
    let ip = Math.floor((jp + jm - nside + kshift + 1) / 2);
    ip = ((ip % (4 * nside)) + 4 * nside) % (4 * nside);
    return nside * (nside - 1) * 2 + (ir - 1) * 4 * nside + ip;
  } else {
    const tp = tt - Math.floor(tt);
    const tmp = nside * Math.sqrt(3.0 * (1.0 - za));
    let jp = Math.floor(tp * tmp);
    let jm = Math.floor((1.0 - tp) * tmp);
    if (jp >= nside) jp = nside - 1;
    if (jm >= nside) jm = nside - 1;

    if (z > 0) {
      const ir = jp + jm + 1;
      const ip = Math.floor(tt * ir) % (4 * ir);
      return 2 * ir * (ir - 1) + ip;
    } else {
      const ir = jp + jm + 1;
      const ip = Math.floor(tt * ir) % (4 * ir);
      return npix - 2 * ir * (ir + 1) + ip;
    }
  }
}

function skyToPixelIndex(ra, dec, nside, frame) {
  let lon, lat;
  if (frame === "G") {
    [lon, lat] = radec2gal(ra, dec);
  } else {
    lon = ra; lat = dec;
  }
  const theta = (90 - lat) * DEG;
  const phi = lon * DEG;
  return { pix: ang2pix_ring(nside, theta, phi), lon, lat };
}

// ===========================================================================
// HiPS image helpers
// ===========================================================================

async function fetchHipsRecordById(id) {
  const fields = [
    "ID", "obs_title", "hips_frame", "hips_order",
    "hips_service_url", "hips_service_url_1", "hips_service_url_2",
    "hips_initial_ra", "hips_initial_dec", "hips_initial_fov",
    "hips_tile_format", "dataproduct_type"
  ].join(",");

  const url = new URL(MOCSERVER);
  url.searchParams.set("ID", id);
  url.searchParams.set("get", "record");
  url.searchParams.set("fmt", "json");
  url.searchParams.set("fields", fields);
  url.searchParams.set("MAXREC", "1");

  const resp = await fetch(url.toString());
  if (!resp.ok) throw new Error(`MocServer HTTP ${resp.status}`);
  const json = await resp.json();

  let rec = null;
  if (Array.isArray(json)) rec = json[0];
  else if (json && Array.isArray(json.data)) rec = json.data[0];
  else if (json && Array.isArray(json.records)) rec = json.records[0];
  else if (json && json.ID) rec = json;
  if (!rec) throw new Error("Unexpected MocServer JSON shape");
  return rec;
}

function pickBestHipsUrl(rec) {
  return rec.hips_service_url_1 || rec.hips_service_url_2 || rec.hips_service_url;
}

function toFrame(frameStr) {
  const f = String(frameStr || "").toLowerCase();
  return f.includes("gal") ? "galactic" : "equatorial";
}

function num(x, fallback) {
  const v = Number(x);
  return Number.isFinite(v) ? v : fallback;
}

function makeSurveyFromRecord(rec) {
  if (String(rec.dataproduct_type || "").toLowerCase() !== "image") {
    throw new Error(`Not an image HiPS: dataproduct_type=${rec.dataproduct_type}`);
  }
  const url = pickBestHipsUrl(rec);
  const order = num(rec.hips_order, 8);
  const frame = toFrame(rec.hips_frame);
  const title = rec.obs_title || rec.ID || "HiPS";
  const tileFormats = String(rec.hips_tile_format || "").toLowerCase();
  const preferredFormat = tileFormats.includes("png") ? "png" : "fits";

  return A.HiPS(url, {
    name: title,
    cooFrame: frame,
    maxOrder: order,
    imgFormat: preferredFormat
  });
}

// ===========================================================================
// State
// ===========================================================================

const state = {
  records: {},
  surveys: {},
  currentCat: null,
};

function setSourceControlsEnabled(enabled) {
  btnLoadSrcEl.disabled = !enabled;
  selCatalogueEl.disabled = !enabled;
}

async function pickDatastoreDirectory() {
  btnConfigDataEl.disabled = true;
  setStatus("Waiting for datastore directory selection…");
  try {
    const resp = await fetch("/api/config/datastore/pick", { method: "POST" });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok || !payload.ok) {
      throw new Error(payload.error || `HTTP ${resp.status}`);
    }
    setStatus(`Datastore configured: ${payload.datastore}`);
    await populateCatalogueDropdown();
  } catch (err) {
    setStatus(`Datastore configuration failed: ${err.message || err}`);
  } finally {
    btnConfigDataEl.disabled = false;
  }
}

// ===========================================================================
// Image loading
// ===========================================================================

async function loadImage(which, options = {}) {
  const recenter = Boolean(options.recenter);
  const rec = state.records[which];
  const survey = state.surveys[which];
  if (!rec || !survey) throw new Error("Survey not ready");

  const title = rec.obs_title || rec.ID;
  setStatus(`Loading: ${title}`);
  aladin.setBaseImageLayer(survey);

  if (recenter) {
    const ra = num(rec.hips_initial_ra, 0);
    const dec = num(rec.hips_initial_dec, 0);
    const fov = num(rec.hips_initial_fov, 6);
    aladin.gotoRaDec(ra, dec);
    aladin.setFoV(fov);
  }
  setStatus(`Showing: ${title}`);
}

// ===========================================================================
// Catalogue helpers
// ===========================================================================

function getTapConeFromView() {
  const [ra, dec] = aladin.getRaDec();
  const fov = aladin.getFoV();
  const rawRadius = Math.min(fov[0], fov[1]) / 2;
  const radiusDeg = Math.min(
    CATALOG_MAX_RADIUS_DEG,
    Math.max(CATALOG_MIN_RADIUS_DEG, rawRadius)
  );
  return { ra, dec, radiusDeg };
}

function getMinFluxFilterValue() {
  const value = Number(minFluxEl.value);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}

function getSourceSizeValue() {
  const value = Number(sourceSizeEl.value);
  if (!Number.isFinite(value) || value < 1) return 8;
  return Math.round(value);
}

function getCatalogueColor(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  return CATALOGUE_COLORS[Math.abs(hash) % CATALOGUE_COLORS.length];
}

// ===========================================================================
// Local catalogue loading (via server.py API)
// ===========================================================================

async function loadLocalSources(catalogueName) {
  const cone = getTapConeFromView();
  const minFlux = getMinFluxFilterValue();
  const sourceSize = getSourceSizeValue();
  const color = getCatalogueColor(catalogueName);

  const url = new URL("/api/sources", window.location.origin);
  url.searchParams.set("catalogue", catalogueName);
  url.searchParams.set("ra", cone.ra.toFixed(6));
  url.searchParams.set("dec", cone.dec.toFixed(6));
  url.searchParams.set("radius", cone.radiusDeg.toFixed(6));
  if (minFlux !== null) url.searchParams.set("min_flux", minFlux.toFixed(6));

  const fluxText = minFlux === null ? "no flux cut" : `min flux ${minFlux} mJy`;
  setStatus(`Querying ${catalogueName} (${fluxText}, ${cone.radiusDeg.toFixed(2)}° cone)…`);
  btnLoadSrcEl.disabled = true;

  try {
    const resp = await fetch(url.toString());
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const data = await resp.json();

    if (state.currentCat && typeof state.currentCat.hide === "function") {
      state.currentCat.hide();
    }

    const catalog = A.catalog({
      name: catalogueName,
      color,
      sourceSize,
      hoverColor: "#ff5555",
      onClick: "showTable",
    });

    for (const src of data.sources) {
      const meta = { name: src.id };
      if (src.flux !== null && src.flux !== undefined) meta.flux = src.flux;
      catalog.addSources([A.source(src.ra, src.dec, meta)]);
    }

    state.currentCat = catalog;
    aladin.addCatalog(catalog);
    setStatus(`${catalogueName}: ${data.count} sources (${fluxText}, ${cone.radiusDeg.toFixed(2)}° cone)`);
  } catch (err) {
    setStatus(`Catalogue load failed: ${err.message || err}`);
  } finally {
    btnLoadSrcEl.disabled = false;
  }
}

// ===========================================================================
// Populate catalogue dropdown from server
// ===========================================================================

async function populateCatalogueDropdown() {
  try {
    const resp = await fetch("/api/catalogues");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const cats = await resp.json();
    const misconfigured = cats.every((cat) => cat.status === "misconfigured");
    const availableCats = cats.filter((cat) => cat.available);

    selCatalogueEl.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select catalogue…";
    placeholder.disabled = true;
    placeholder.selected = true;
    selCatalogueEl.appendChild(placeholder);

    for (const cat of availableCats) {
      const opt = document.createElement("option");
      opt.value = cat.name;
      opt.textContent = cat.name;
      selCatalogueEl.appendChild(opt);
    }
    setSourceControlsEnabled(availableCats.length > 0);

    if (misconfigured) {
      const message = cats[0] && cats[0].message
        ? cats[0].message
        : "Datastore is not configured.";
      setStatus(`${message} Click "Set datastore" to choose a directory.`);
    } else if (availableCats.length === 0) {
      setStatus("No source catalogues were discovered under the configured datastore.");
    }
  } catch (err) {
    console.error("Failed to load catalogue list:", err);
    selCatalogueEl.innerHTML = "";
    const ph = document.createElement("option");
    ph.value = ""; ph.textContent = "Select catalogue…";
    ph.disabled = true; ph.selected = true;
    selCatalogueEl.appendChild(ph);
    setSourceControlsEnabled(false);
    setStatus("Failed to load catalogue list.");
  }
}

// ===========================================================================
// Canvas overlay (shared by circle + HEALPix grid)
// ===========================================================================

let overlayCanvas = null;
let overlayCtx = null;
let circleEnabled = false;
let gridEnabled = false;
let gridPixels = [];  // cached grid data from server
let _gridFetchId = 0; // for debounce dedup

function initOverlayCanvas() {
  const aladinDiv = document.getElementById("aladin");
  overlayCanvas = document.createElement("canvas");
  overlayCanvas.style.cssText =
    "position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:10;";
  aladinDiv.appendChild(overlayCanvas);
  overlayCtx = overlayCanvas.getContext("2d");
}

function redrawOverlay() {
  const c = overlayCanvas, ctx = overlayCtx;
  const dpr = window.devicePixelRatio || 1;
  c.width = c.clientWidth * dpr;
  c.height = c.clientHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, c.clientWidth, c.clientHeight);

  if (circleEnabled) drawCircle(ctx, c);
  if (gridEnabled) drawGrid(ctx, c);
}

// ---------------------------------------------------------------------------
// Circle
// ---------------------------------------------------------------------------

function drawCircle(ctx, c) {
  const cx = c.clientWidth / 2;
  const cy = c.clientHeight / 2;
  const radiusDeg = parseFloat(circleRadiusEl.value) || 1.0;
  const fov = aladin.getFoV();
  const pixPerDeg = c.clientWidth / fov[0];
  const radiusPx = radiusDeg * pixPerDeg;

  ctx.beginPath();
  ctx.arc(cx, cy, radiusPx, 0, 2 * Math.PI);
  ctx.strokeStyle = "#00ffff";
  ctx.lineWidth = 2;
  ctx.stroke();
}

// ---------------------------------------------------------------------------
// HEALPix grid drawing
// ---------------------------------------------------------------------------

function drawGrid(ctx) {
  if (!gridPixels.length) return;

  ctx.strokeStyle = "rgba(255,255,0,0.5)";
  ctx.lineWidth = 1;
  ctx.fillStyle = "rgba(255,255,0,0.6)";
  ctx.font = "10px monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";

  for (const px of gridPixels) {
    const pts = [];
    let ok = true;
    for (let i = 0; i < px.vra.length; i++) {
      const xy = aladin.world2pix(px.vra[i], px.vdec[i]);
      if (!xy) { ok = false; break; }
      pts.push(xy);
    }
    if (!ok || pts.length < 3) continue;

    // Check for wrap-around: if any adjacent points are very far apart, skip
    let wrap = false;
    for (let i = 0; i < pts.length; i++) {
      const j = (i + 1) % pts.length;
      const dx = Math.abs(pts[i][0] - pts[j][0]);
      if (dx > overlayCanvas.clientWidth * 0.5) { wrap = true; break; }
    }
    if (wrap) continue;

    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.closePath();
    ctx.stroke();

    // Pixel label at centroid
    let cx = 0, cy = 0;
    for (const p of pts) { cx += p[0]; cy += p[1]; }
    cx /= pts.length; cy /= pts.length;
    // Only label if pixel is large enough
    const size = Math.abs(pts[0][0] - pts[Math.floor(pts.length/2)][0]);
    if (size > 30) {
      ctx.fillText(String(px.pix), cx, cy);
    }
  }
}

let _gridDebounce = null;

function fetchGridPixels() {
  if (!gridEnabled) return;
  clearTimeout(_gridDebounce);
  _gridDebounce = setTimeout(_doFetchGrid, 200);
}

async function _doFetchGrid() {
  const [ra, dec] = aladin.getRaDec();
  const fov = aladin.getFoV();
  const nside = parseInt(gridNsideEl.value) || 64;
  const frame = getGridFrame();
  const fetchId = ++_gridFetchId;

  const url = new URL("/api/healpix/grid", window.location.origin);
  url.searchParams.set("ra", ra.toFixed(4));
  url.searchParams.set("dec", dec.toFixed(4));
  url.searchParams.set("fov", Math.max(fov[0], fov[1]).toFixed(4));
  url.searchParams.set("nside", nside);
  url.searchParams.set("frame", frame);

  try {
    const resp = await fetch(url.toString());
    if (!resp.ok) return;
    const data = await resp.json();
    if (fetchId !== _gridFetchId) return; // stale
    gridPixels = data;
    redrawOverlay();
  } catch (e) {
    console.error("Grid fetch error:", e);
  }
}

// ===========================================================================
// Init
// ===========================================================================

async function init() {
  await A.init;

  aladin = A.aladin("#aladin", {
    target: "279.5 -31.7",
    cooFrame: "GAL",
    fov: 6,
    showGotoControl: true,
    showLayersControl: true
  });

  initOverlayCanvas();

  setStatus("Querying CDS HiPS registry…");

  const [recLow, recMid] = await Promise.all([
    fetchHipsRecordById(IDS.low),
    fetchHipsRecordById(IDS.mid),
  ]);
  state.records.low = recLow;
  state.records.mid = recMid;
  state.surveys.low = makeSurveyFromRecord(recLow);
  state.surveys.mid = makeSurveyFromRecord(recMid);

  // RACS-high: loaded directly from ATNF (not in CDS MocServer)
  state.records.high = {
    obs_title: "RACS-high (1655.5 MHz)",
    ID: "RACS-high",
    dataproduct_type: "image",
    hips_service_url: DIRECT_HIPS.high.url,
    hips_frame: "equatorial",
    hips_order: DIRECT_HIPS.high.maxOrder,
    hips_tile_format: "png",
    hips_initial_ra: 180,
    hips_initial_dec: -30,
    hips_initial_fov: 6,
  };
  state.surveys.high = makeSurveyFromRecord(state.records.high);

  populateCatalogueDropdown();

  // Enable UI
  btnLoadImgEl.disabled = false;
  selImageEl.disabled = false;
  setSourceControlsEnabled(false);

  // --- Image button ---
  btnLoadImgEl.onclick = () => {
    loadImage(selImageEl.value).catch((e) => setStatus(`Error: ${e.message}`));
  };

  // --- Source button ---
  btnLoadSrcEl.onclick = () => {
    const cat = selCatalogueEl.value;
    if (!cat) { setStatus("Please select a catalogue first."); return; }
    loadLocalSources(cat).catch((e) => setStatus(`Error: ${e.message}`));
  };
  btnConfigDataEl.onclick = () => {
    pickDatastoreDirectory().catch((e) => {
      setStatus(`Datastore configuration failed: ${e.message || e}`);
    });
  };

  // --- Circle toggle ---
  btnCircleEl.onclick = () => {
    circleEnabled = !circleEnabled;
    btnCircleEl.textContent = circleEnabled ? "Circle: ON" : "Circle: OFF";
    redrawOverlay();
  };
  circleRadiusEl.addEventListener("input", () => { if (circleEnabled) redrawOverlay(); });

  // --- Grid toggle ---
  btnGridEl.onclick = () => {
    gridEnabled = !gridEnabled;
    btnGridEl.textContent = gridEnabled ? "Grid: ON" : "Grid: OFF";
    if (gridEnabled) {
      fetchGridPixels();
    } else {
      gridPixels = [];
      redrawOverlay();
    }
  };
  gridNsideEl.addEventListener("change", () => { if (gridEnabled) fetchGridPixels(); });

  // --- View change events ---
  const onViewChange = () => {
    redrawOverlay();
    if (gridEnabled) fetchGridPixels();
  };
  aladin.on("positionChanged", onViewChange);
  aladin.on("zoomChanged", onViewChange);

  // Re-fetch grid when Aladin's coordinate frame changes.
  // cooFrameChanged may not exist in all Aladin versions; positionChanged
  // already covers most frame-switch cases since the view updates.
  try { aladin.on("cooFrameChanged", () => { if (gridEnabled) fetchGridPixels(); }); } catch (_) {}

  // Start on low
  await loadImage("low", { recenter: true });
  aladin.setFrame("GAL");
  aladin.gotoPosition(279.5, -31.7);
}

init().catch((e) => {
  console.error(e);
  setStatus(`Init failed: ${e.message}`);
});
