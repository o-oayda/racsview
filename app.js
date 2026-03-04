"use strict";

// CDS MocServer query endpoint (HiPS registry/aggregator)
const MOCSERVER = "https://alasky.cds.unistra.fr/MocServer/query";

// The two surveys you care about in the registry:
const IDS = {
  low: "CSIRO/P/RACS/low/I",
  mid: "CSIRO/P/RACS/mid/I",
};

const statusEl = document.getElementById("status");
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

const state = { recLow: null, recMid: null, surveyLow: null, surveyMid: null };

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

async function init() {
  // Aladin v3 must initialize WASM before creating a viewer.
  await A.init;

  // Create viewer after A.init resolves.
  aladin = A.aladin("#aladin", {
    target: "0 +0",
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

  document.getElementById("btnLow").onclick = () => {
    load("low").catch((e) => setStatus(`Error: ${e.message}`));
  };
  document.getElementById("btnMid").onclick = () => {
    load("mid").catch((e) => setStatus(`Error: ${e.message}`));
  };

  // Start on low.
  await load("low");
}

init().catch((e) => {
  console.error(e);
  setStatus(`Init failed: ${e.message}`);
});
