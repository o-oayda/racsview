"use strict";

// CDS MocServer query endpoint (HiPS registry/aggregator)
const MOCSERVER = "https://alasky.cds.unistra.fr/MocServer/query";
const CASDA_TAP_SYNC = "https://casda.csiro.au/casda_vo_tools/tap/sync";

// The two surveys you care about in the registry:
const IDS = {
  low: "CSIRO/P/RACS/low/I",
  mid: "CSIRO/P/RACS/mid/I",
};

const CATALOG_TABLES = {
  // CASDA RACS source-catalogue tables discovered via TAP_SCHEMA.
  low: "AS110.racs_dr1_sources_galacticcut_v2021_08_v02",
  mid: "AS110.racs_mid_sources_v01",
};

const CATALOG_MIN_RADIUS_DEG = 0.2;
const CATALOG_MAX_RADIUS_DEG = 2.0;

const statusEl = document.getElementById("status");
const minFluxEl = document.getElementById("minFlux");
const setStatus = (s) => {
  statusEl.textContent = s;
};

let aladin = null;

// Fetch a HiPS "record" from MocServer as JSON
async function fetchHipsRecordById(id) {
  // MocServer supports: ID=... get=record fmt=json fields=... MAXREC=...
  const fields = [
    "ID",
    "obs_title",
    "hips_frame",
    "hips_order",
    "hips_service_url",
    "hips_service_url_1",
    "hips_service_url_2",
    "hips_initial_ra",
    "hips_initial_dec",
    "hips_initial_fov",
    "hips_tile_format",
    "dataproduct_type"
  ].join(",");

  const url = new URL(MOCSERVER);
  url.searchParams.set("ID", id);
  url.searchParams.set("get", "record");
  url.searchParams.set("fmt", "json");
  url.searchParams.set("fields", fields);
  url.searchParams.set("MAXREC", "1");

  const resp = await fetch(url.toString());
  if (!resp.ok) {
    throw new Error(`MocServer HTTP ${resp.status}`);
  }
  const json = await resp.json();

  // MocServer JSON can be either:
  // - an object with "data"/"records" depending on version, or
  // - an array of records.
  let rec = null;
  if (Array.isArray(json)) {
    rec = json[0];
  } else if (json && Array.isArray(json.data)) {
    rec = json.data[0];
  } else if (json && Array.isArray(json.records)) {
    rec = json.records[0];
  } else if (json && json.ID) {
    rec = json;
  }
  if (!rec) {
    throw new Error("Unexpected MocServer JSON shape");
  }

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

  // Aladin v3 UMD API: construct an image HiPS layer from URL/metadata.
  return A.HiPS(url, {
    name: title,
    cooFrame: frame,
    maxOrder: order,
    // Prefer PNG for faster interactive rendering; use FITS when PNG is unavailable.
    imgFormat: preferredFormat
  });
}

const state = {
  recLow: null,
  recMid: null,
  surveyLow: null,
  surveyMid: null,
  catLow: null,
  catMid: null,
};

async function load(which) {
  const rec = which === "low" ? state.recLow : state.recMid;
  const survey = which === "low" ? state.surveyLow : state.surveyMid;

  if (!rec || !survey) {
    throw new Error("Survey not ready");
  }

  const title = rec.obs_title || rec.ID;
  setStatus(`Loading: ${title}`);

  // Set the base image layer to this HiPS survey.
  aladin.setBaseImageLayer(survey);

  // Use registry-provided initial view if present.
  const ra = num(rec.hips_initial_ra, 0);
  const dec = num(rec.hips_initial_dec, 0);
  const fov = num(rec.hips_initial_fov, 6);

  aladin.gotoRaDec(ra, dec);
  aladin.setFoV(fov);

  setStatus(`Showing: ${title}`);
}

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

function buildTapSyncUrl(query) {
  const url = new URL(CASDA_TAP_SYNC);
  url.searchParams.set("REQUEST", "doQuery");
  url.searchParams.set("LANG", "ADQL");
  url.searchParams.set("FORMAT", "votable");
  url.searchParams.set("QUERY", query);
  return url.toString();
}

function getMinFluxFilterValue() {
  const value = Number(minFluxEl.value);
  if (!Number.isFinite(value) || value <= 0) {
    return null;
  }
  return value;
}

function buildRacsSourceQuery(table, ra, dec, radiusDeg, minFlux) {
  const raStr = Number(ra).toFixed(6);
  const decStr = Number(dec).toFixed(6);
  const radStr = Number(radiusDeg).toFixed(6);
  const lowerTable = String(table).toLowerCase();
  const nameCol = lowerTable.includes("racs_dr1_sources") ? "source_name" : "name";
  const totalFluxCol = lowerTable.includes("racs_dr1_sources")
    ? "total_flux_source"
    : "total_flux";
  const fluxWhere = Number.isFinite(minFlux)
    ? `AND ${totalFluxCol} >= ${Number(minFlux).toFixed(6)}`
    : "";

  // Cone-limited query around the current view center to keep result volume manageable.
  return [
    `SELECT ra, dec, ${nameCol} AS name, ${totalFluxCol} AS total_flux, peak_flux`,
    `FROM ${table}`,
    `WHERE 1 = CONTAINS(`,
    `  POINT('ICRS', ra, dec),`,
    `  CIRCLE('ICRS', ${raStr}, ${decStr}, ${radStr})`,
    `)`,
    fluxWhere
  ].join(" ");
}

function setCatalogButtonsDisabled(disabled) {
  document.getElementById("btnLowCat").disabled = disabled;
  document.getElementById("btnMidCat").disabled = disabled;
}

async function loadSourceCatalog(which) {
  const table = which === "low" ? CATALOG_TABLES.low : CATALOG_TABLES.mid;
  const label = which === "low" ? "RACS low sources" : "RACS mid sources";
  const color = which === "low" ? "#f59e0b" : "#22c55e";
  const stateKey = which === "low" ? "catLow" : "catMid";
  const oldCatalog = state[stateKey];
  const minFlux = getMinFluxFilterValue();

  const cone = getTapConeFromView();
  const query = buildRacsSourceQuery(table, cone.ra, cone.dec, cone.radiusDeg, minFlux);
  const url = buildTapSyncUrl(query);

  setCatalogButtonsDisabled(true);
  const fluxText = minFlux === null ? "no flux cut" : `min flux ${minFlux} mJy`;
  setStatus(`Querying ${label} (${fluxText}, ${cone.radiusDeg.toFixed(2)}° cone)...`);

  try {
    const catalog = await new Promise((resolve, reject) => {
      A.catalogFromURL(
        url,
        {
          name: label,
          color,
          hoverColor: "#ff5555",
          onClick: "showTable"
        },
        (loadedCatalog) => resolve(loadedCatalog),
        (err) => reject(err),
        true
      );
    });

    if (oldCatalog && typeof oldCatalog.hide === "function") {
      oldCatalog.hide();
    }

    state[stateKey] = catalog;
    aladin.addCatalog(catalog);
    setStatus(`Showing ${label} (${fluxText}, ${cone.radiusDeg.toFixed(2)}° cone)`);
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    setStatus(`Catalog load failed: ${message}`);
  } finally {
    setCatalogButtonsDisabled(false);
  }
}

async function init() {
  // Aladin v3 must initialize WASM before creating a viewer.
  await A.init;

  // Create viewer after A.init resolves.
  aladin = A.aladin("#aladin", {
    target: "279.5 -31.7",
    cooFrame: "GAL",
    fov: 6,
    showGotoControl: true,
    showLayersControl: true
  });

  setStatus("Querying CDS HiPS registry…");

  // Pull both records from MocServer.
  state.recLow = await fetchHipsRecordById(IDS.low);
  state.recMid = await fetchHipsRecordById(IDS.mid);

  // Build survey objects from registry metadata.
  state.surveyLow = makeSurveyFromRecord(state.recLow);
  state.surveyMid = makeSurveyFromRecord(state.recMid);

  // Enable UI.
  document.getElementById("btnLow").disabled = false;
  document.getElementById("btnMid").disabled = false;
  setCatalogButtonsDisabled(false);

  document.getElementById("btnLow").onclick = () => {
    load("low").catch((e) => setStatus(`Error: ${e.message}`));
  };
  document.getElementById("btnMid").onclick = () => {
    load("mid").catch((e) => setStatus(`Error: ${e.message}`));
  };
  document.getElementById("btnLowCat").onclick = () => {
    loadSourceCatalog("low").catch((e) => setStatus(`Error: ${e.message}`));
  };
  document.getElementById("btnMidCat").onclick = () => {
    loadSourceCatalog("mid").catch((e) => setStatus(`Error: ${e.message}`));
  };

  // Start on low.
  await load("low");
  aladin.setFrame("GAL");
  aladin.gotoPosition(279.5, -31.7);
}

init().catch((e) => {
  console.error(e);
  setStatus(`Init failed: ${e.message}`);
});
