"use strict";

// Image paths in the manifest are relative to the manifest's own folder (web/data/).
const DATA_DIR = "data/";
const assetUrl = (p) => (p ? DATA_DIR + p : "");

const state = { images: [], index: 0 };

const $ = (sel) => document.querySelector(sel);

function fmtTime(iso) {
  if (!iso) return "time unknown";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
}

function fmtKm(km) {
  if (km == null) return null;
  return Math.round(km).toLocaleString("en-US") + " km";
}

function metaRows(img) {
  const d = img.display || {};
  const rows = [
    ["Frame ID", img.id],
    ["Acquired", fmtTime(img.start_utc)],
    ["Distance to Earth", fmtKm(img.earth_distance_km)],
    ["Mission phase", img.mission_phase],
    ["Instrument", img.instrument],
    ["Exposure", img.exposure_s != null ? +(img.exposure_s * 1000).toFixed(3) + " ms" : null],
    ["Band center", img.band_center_nm != null ? img.band_center_nm + " nm" : null],
    ["Dimensions", img.lines && img.samples ? `${img.samples} × ${img.lines}` : null],
    ["Processing level", img.processing_level],
    ["Display stretch", d.display_stretch],
    ["Smear removed", d.smear_max_dn != null ? `up to ${Math.round(d.smear_max_dn)} DN / column` : null],
    ["Saturated pixels", d.saturated_pixels != null ? d.saturated_pixels.toLocaleString("en-US") : null],
  ];
  return rows.filter(([, v]) => v != null && v !== "");
}

function renderGallery() {
  const gallery = $("#gallery");
  gallery.innerHTML = "";
  state.images.forEach((img, i) => {
    const card = document.createElement("button");
    card.className = "card";
    card.type = "button";
    card.addEventListener("click", () => openViewer(i));

    const thumb = document.createElement("img");
    thumb.loading = "lazy";
    thumb.src = assetUrl(img.png);
    thumb.alt = `ONC-W2 frame ${img.id}`;

    const body = document.createElement("div");
    body.className = "card-body";
    const dist = fmtKm(img.earth_distance_km);
    body.innerHTML =
      `<div class="card-time">${fmtTime(img.start_utc)}</div>` +
      `<div class="card-sub">${dist ? "Earth " + dist + " away" : img.id}</div>`;

    card.append(thumb, body);
    gallery.append(card);
  });
}

function openViewer(i) {
  state.index = i;
  const img = state.images[i];
  const viewer = $("#viewer");
  $("#viewer-img").src = assetUrl(img.png);
  $("#viewer-img").alt = `ONC-W2 frame ${img.id}`;

  const rows = metaRows(img)
    .map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`)
    .join("");
  const link = img.source_label_url
    ? `<p style="margin:.5rem 0 0"><a href="${img.source_label_url}" rel="noopener">PDS label &nearr;</a></p>`
    : "";
  $("#viewer-meta").innerHTML = `<table>${rows}</table>${link}`;

  $(".viewer-prev").disabled = i === 0;
  $(".viewer-next").disabled = i === state.images.length - 1;
  viewer.hidden = false;
}

function closeViewer() {
  $("#viewer").hidden = true;
  $("#viewer-img").src = "";
}

function step(delta) {
  const next = state.index + delta;
  if (next >= 0 && next < state.images.length) openViewer(next);
}

function wireEvents() {
  $("#viewer").addEventListener("click", (e) => {
    const action = e.target.dataset.action;
    if (action === "close" || e.target.id === "viewer") closeViewer();
    else if (action === "prev") step(-1);
    else if (action === "next") step(1);
  });
  document.addEventListener("keydown", (e) => {
    if ($("#viewer").hidden) return;
    if (e.key === "Escape") closeViewer();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
  });
}

async function main() {
  wireEvents();
  const status = $("#status");
  try {
    const resp = await fetch("data/manifest.json", { cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const manifest = await resp.json();
    state.images = (manifest.images || []).filter((im) => im.png);

    const ds = manifest.dataset || {};
    $("#dataset-line").textContent = [
      ds.frame_count != null ? `${ds.frame_count} frames` : null,
      ds.instrument,
      ds.source_archive,
      ds.generated_utc ? `generated ${ds.generated_utc}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    $("#citation").textContent = ds.citation || "";
    $("#pipeline-note").textContent = ds.processing ? "Pipeline: " + ds.processing : "";

    if (!state.images.length) {
      status.textContent = "Manifest loaded, but no images are available yet.";
      return;
    }
    status.hidden = true;
    renderGallery();
  } catch (err) {
    status.className = "status error";
    status.textContent =
      "Could not load data/manifest.json (" + err.message + "). " +
      "Run the process-images workflow and commit web/data/, or serve this folder over HTTP.";
  }
}

main();
