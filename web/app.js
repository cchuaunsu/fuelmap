/* Fuel Prices — NCR map frontend.
 * A thin consumer of the Fuel Intelligence Engine REST API: it renders
 * whatever the engine verified and never invents a price. */

"use strict";

const API = "/api/v1";
const NCR_CENTER = [14.5995, 120.9842]; // Manila fallback
const SIDEBAR_LIMIT = 30;

const FUELS = [
  { id: "diesel", label: "Diesel" },
  { id: "gasoline_ron91", label: "RON 91" },
  { id: "gasoline_ron95", label: "RON 95" },
  { id: "gasoline_ron97", label: "RON 97" },
  { id: "gasoline_ron100", label: "RON 100" },
  { id: "premium_diesel", label: "Prem. Diesel" },
  { id: "kerosene", label: "Kerosene" },
];
const FUEL_LABELS = Object.fromEntries(FUELS.map((f) => [f.id, f.label]));

const BRAND_COLORS = {
  shell: "#e8b400", petron: "#1c56a4", caltex: "#00843d",
  seaoil: "#0a9e4e", unioil: "#f26f21", cleanfuel: "#0098d8",
  phoenix: "#e4572e", flying_v: "#c2262e", total: "#e2001a",
  jetti: "#7a3fb3", ptt: "#00539f", unknown: "#64748b",
};

// How many full price pills may appear, by zoom. Everything else in view
// renders as a small dot (hover for price, click to expand).
function pillBudget(zoom) {
  if (zoom < 13) return 8;
  if (zoom < 14) return 16;
  if (zoom < 15) return 34;
  if (zoom < 16) return 60;
  return 110;
}
const DOT_CAP = 1200;

const state = {
  stations: [],
  prices: {},            // station_id -> fuel_type -> price row
  fuel: "diesel",
  userPos: null,         // [lat, lng]
  userIsManual: false,
  activeStation: null,
  cheapestId: null,
  dataLoaded: false,
};

/* ================= map + tiles ================= */

const map = L.map("map", {
  zoomControl: true,
  wheelPxPerZoomLevel: 90,
  zoomSnap: 0.5,
}).setView(NCR_CENTER, 12);

// The map itself defaults to light tiles — they stay readable under price
// pills even when the app chrome is dark. A map control toggles dark
// tiles for night driving; the choice is remembered.
const TILE_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>';
const tileLight = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
  { maxZoom: 19, subdomains: "abcd", attribution: TILE_ATTR }
);
const tileDark = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
  { maxZoom: 19, subdomains: "abcd", attribution: TILE_ATTR }
);

let mapStyle = localStorage.getItem("fie-map-style") === "dark" ? "dark" : "light";
(mapStyle === "dark" ? tileDark : tileLight).addTo(map);

const StyleToggle = L.Control.extend({
  options: { position: "topleft" },
  onAdd() {
    const btn = L.DomUtil.create("a", "map-style-btn leaflet-bar");
    btn.href = "#";
    btn.title = "Toggle light/dark map";
    btn.textContent = "🌓";
    L.DomEvent.on(btn, "click", (e) => {
      L.DomEvent.stop(e);
      mapStyle = mapStyle === "dark" ? "light" : "dark";
      localStorage.setItem("fie-map-style", mapStyle);
      map.removeLayer(mapStyle === "dark" ? tileLight : tileDark);
      (mapStyle === "dark" ? tileDark : tileLight).addTo(map);
    });
    return btn;
  },
});
map.addControl(new StyleToggle());

// Canvas renderer makes hundreds of station dots cheap to draw.
const dotRenderer = L.canvas({ padding: 0.3 });

/* ================= location: coarse fix, then live tracking =========== */

const locateBtn = document.getElementById("locate-btn");
const locateLabel = document.getElementById("locate-label");
let userMarker = null;
let accuracyCircle = null;
let geoWatchId = null;
let hasGpsFix = false;
let pickingLocation = false;

function setUserPosition(lat, lng, { accuracy = null, manual = false, fly = false } = {}) {
  state.userPos = [lat, lng];
  state.userIsManual = manual;

  if (!userMarker) {
    userMarker = L.marker([lat, lng], {
      draggable: true,
      zIndexOffset: 900,
      icon: L.divIcon({ className: "user-marker", html: '<div class="user-dot"></div>', iconSize: null }),
    })
      .addTo(map)
      .bindTooltip("You are here — drag to adjust", { direction: "top", offset: [0, -10] });
    userMarker.on("dragend", () => {
      const p = userMarker.getLatLng();
      setUserPosition(p.lat, p.lng, { manual: true });
      toast("Location set manually — drag the pin anytime.");
    });
  } else {
    userMarker.setLatLng([lat, lng]);
  }
  userMarker.getElement()?.querySelector(".user-dot")?.classList.toggle("manual", manual);

  if (accuracy && !manual) {
    if (!accuracyCircle) {
      accuracyCircle = L.circle([lat, lng], {
        radius: Math.min(accuracy, 800),
        color: "#2563eb", weight: 1, fillColor: "#2563eb", fillOpacity: 0.07,
        interactive: false,
      }).addTo(map);
    } else {
      accuracyCircle.setLatLng([lat, lng]).setRadius(Math.min(accuracy, 800));
    }
  } else if (accuracyCircle) {
    accuracyCircle.remove();
    accuracyCircle = null;
  }

  if (manual) {
    localStorage.setItem("fie-manual-loc", JSON.stringify([lat, lng]));
    locateBtn.classList.remove("failed", "tracking");
    locateLabel.textContent = "Manual location";
  }
  if (fly) map.flyTo([lat, lng], Math.max(map.getZoom(), 14), { duration: 0.8 });
  renderSidebar();
}

function gotGpsFix(pos, { fly }) {
  const first = !hasGpsFix;
  hasGpsFix = true;
  locateBtn.classList.add("tracking");
  locateBtn.classList.remove("failed");
  locateLabel.textContent = "Tracking";
  localStorage.removeItem("fie-manual-loc");
  hideBanner();
  setUserPosition(pos.coords.latitude, pos.coords.longitude, {
    accuracy: pos.coords.accuracy,
    manual: false,
    fly: first || fly,
  });
}

function acquireLocation({ fly = false } = {}) {
  if (!("geolocation" in navigator)) {
    locationFailed("This browser has no geolocation support.");
    return;
  }
  locateLabel.textContent = "Locating…";
  locateBtn.classList.remove("failed");

  // Phase 1 — fast coarse fix: cached or network-based positions are fine
  // and rarely time out, unlike high-accuracy GPS on desktops.
  navigator.geolocation.getCurrentPosition(
    (pos) => gotGpsFix(pos, { fly }),
    (err) => {
      if (!hasGpsFix) locationFailed(reasonFor(err), err.code);
    },
    { enableHighAccuracy: false, timeout: 8000, maximumAge: 600000 }
  );

  // Phase 2 — precise live tracking upgrades the coarse fix when it can.
  if (geoWatchId !== null) navigator.geolocation.clearWatch(geoWatchId);
  geoWatchId = navigator.geolocation.watchPosition(
    (pos) => gotGpsFix(pos, { fly: false }),
    (err) => {
      // Stay quiet if any usable position (coarse, manual, saved) exists.
      if (!hasGpsFix && !state.userPos) locationFailed(reasonFor(err), err.code);
    },
    { enableHighAccuracy: true, maximumAge: 15000, timeout: 25000 }
  );
}

function reasonFor(err) {
  return {
    1: "Location permission was denied.",
    2: "Your position is currently unavailable.",
    3: "Locating timed out.",
  }[err.code] || "Could not get your location.";
}

function locationFailed(reason, code) {
  locateBtn.classList.add("failed");
  locateBtn.classList.remove("tracking");
  locateLabel.textContent = "Set location";
  if (state.userPos) return; // a manual/saved position still works

  let hint = "Click anywhere on the map to set your location manually — prices will sort around it.";
  if (code === 3 || code === 2) {
    hint += " On a Mac, also check System Settings → Privacy & Security → Location Services is on for your browser.";
  }
  // Nag once per session, not on every retry.
  if (!sessionStorage.getItem("fie-loc-banner")) {
    sessionStorage.setItem("fie-loc-banner", "1");
    showBanner(`${reason} ${hint}`);
  }
  enterPickMode();
}

function enterPickMode() {
  pickingLocation = true;
  document.getElementById("map").classList.add("picking");
}

map.on("click", (e) => {
  if (!pickingLocation) return;
  pickingLocation = false;
  document.getElementById("map").classList.remove("picking");
  hideBanner();
  setUserPosition(e.latlng.lat, e.latlng.lng, { manual: true });
  toast("Location set. Drag the pin to fine-tune it.");
});

locateBtn.addEventListener("click", () => {
  if (state.userPos && hasGpsFix) {
    map.flyTo(state.userPos, Math.max(map.getZoom(), 15), { duration: 0.8 });
    return;
  }
  if (locateBtn.classList.contains("failed")) {
    toast("Retrying GPS — or click the map to set your location manually.");
    enterPickMode();
  }
  acquireLocation({ fly: true });
});

function restoreSavedLocation() {
  try {
    const saved = JSON.parse(localStorage.getItem("fie-manual-loc") || "null");
    if (Array.isArray(saved) && saved.length === 2) {
      setUserPosition(saved[0], saved[1], { manual: true });
      map.setView(saved, 14);
    }
  } catch { /* ignore corrupt storage */ }
}

/* ================= banner + toast ================= */

const banner = document.getElementById("map-banner");
function showBanner(text) {
  document.getElementById("map-banner-text").textContent = text;
  banner.hidden = false;
}
function hideBanner() { banner.hidden = true; }
document.getElementById("map-banner-close").addEventListener("click", hideBanner);

let toastTimer = null;
function toast(message, isError) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", !!isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

/* ================= data ================= */

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = (await res.json()).detail || detail;
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.json();
}

async function loadData() {
  const [stations, prices] = await Promise.all([
    fetchJSON(`${API}/stations`),
    fetchJSON(`${API}/prices`),
  ]);
  state.stations = stations;
  state.prices = {};
  for (const row of prices) {
    (state.prices[row.station_id] ??= {})[row.fuel_type] = row;
  }
  state.dataLoaded = true;
  renderAll();

  const lastTimes = prices.map((p) => p.last_refresh_timestamp).filter(Boolean).sort();
  setStatus(
    lastTimes.length
      ? `${stations.length} stations · refreshed ${relTime(lastTimes[lastTimes.length - 1])}`
      : `${stations.length} stations · press Refresh to run the first investigation`
  );
}

async function refreshPrices() {
  const btn = document.getElementById("refresh-btn");
  const label = document.getElementById("refresh-label");
  btn.disabled = true;
  btn.classList.add("loading");
  label.textContent = "Investigating…";
  setStatus("Running a new investigation — collecting and verifying evidence…");
  try {
    const report = await fetchJSON(`${API}/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    await loadData();
    const s = report.stats;
    toast(
      `Verified ${s.verified} prices from ${s.evidence_collected} evidence items` +
      (s.unavailable ? ` · ${s.unavailable} unavailable` : "") +
      (s.provider_errors ? ` · ${s.provider_errors} source error${s.provider_errors === 1 ? "" : "s"}` : "")
    );
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.classList.remove("loading");
    label.textContent = "Refresh";
  }
}

/* ================= rendering helpers ================= */

function priceRow(stationId, fuel) {
  return (state.prices[stationId] || {})[fuel] || null;
}

function rowClass(row) {
  if (!row || row.verified_price == null) return "unavailable";
  if (row.status === "derived") return "derived";
  if (row.status === "last_successfully_verified") return "stale";
  return row.confidence; // high | medium | low
}

function badgeFor(row, cls, withTime) {
  if (cls === "derived") {
    return `<span class="badge derived" title="${escapeHTML(row.derivation_note)}">derived</span>`;
  }
  if (cls === "stale") {
    const when = withTime ? ` ${relTime(row.verification_timestamp)}` : "";
    return `<span class="badge stale" title="Kept from the last successful verification">last verified${when}</span>`;
  }
  const titles = {
    high: "Corroborated by the official adjustment record and in line with brand pricing",
    medium: "Single witness or unusual price — treat as indicative",
    low: "Weak evidence",
  };
  return `<span class="badge ${row.confidence}" title="${titles[row.confidence] || ""}">${row.confidence}</span>`;
}

/* ============ markers: a few readable pills + dots for the rest ========
 * Pills are placed greedily in priority order (active station, cheapest in
 * view, then nearest to center); any pill that would overlap an already
 * placed one is demoted to a dot. Both layers are pooled — panning and
 * fuel switches update in place instead of rebuilding. */

const pillPool = new Map(); // station_id -> { marker, key }
const dotPool = new Map();  // station_id -> { marker, key }

function pillIcon(st, row, cls, isCheapest, isActive) {
  const priceText = row && row.verified_price != null
    ? `₱${row.verified_price.toFixed(2)}` : "—";
  const color = BRAND_COLORS[st.brand] || BRAND_COLORS.unknown;
  const classes = ["pm", cls];
  if (isCheapest) classes.push("cheapest");
  if (isActive) classes.push("active");
  return L.divIcon({
    className: "price-marker",
    html:
      `<div class="${classes.join(" ")}">` +
      `<span class="pm-brand" style="background:${color}">${st.brand[0].toUpperCase()}</span>` +
      `<span>${priceText}</span></div>`,
    iconSize: null,
  });
}

function dotTooltip(st) {
  const row = priceRow(st.station_id, state.fuel);
  const price = row && row.verified_price != null
    ? `₱${row.verified_price.toFixed(2)}/L ${FUEL_LABELS[state.fuel]}`
    : "Price unavailable";
  return `<b>${escapeHTML(st.official_name)}</b><br>${price}`;
}

function estimatePillRect(point, row) {
  const priceText = row && row.verified_price != null
    ? `₱${row.verified_price.toFixed(2)}` : "—";
  const width = 46 + priceText.length * 7.4;
  return {
    x1: point.x - width / 2 - 3,
    x2: point.x + width / 2 + 3,
    y1: point.y - 40,
    y2: point.y - 4,
  };
}

function overlaps(rect, placed) {
  for (const p of placed) {
    if (rect.x1 < p.x2 && p.x1 < rect.x2 && rect.y1 < p.y2 && p.y1 < rect.y2) {
      return true;
    }
  }
  return false;
}

function renderMarkers() {
  const bounds = map.getBounds().pad(0.1);
  const center = map.getCenter();
  const visible = state.stations
    .filter((st) => bounds.contains([st.latitude, st.longitude]))
    .map((st) => ({
      st,
      d: haversineKm([center.lat, center.lng], [st.latitude, st.longitude]),
    }))
    .sort((a, b) => a.d - b.d)
    .slice(0, DOT_CAP);

  // Cheapest usable price in view for the selected fuel.
  state.cheapestId = null;
  let best = Infinity;
  for (const { st } of visible) {
    const row = priceRow(st.station_id, state.fuel);
    if (row && row.verified_price != null && row.verified_price < best) {
      best = row.verified_price;
      state.cheapestId = st.station_id;
    }
  }

  // Priority: active first, cheapest second, then nearest to center.
  const ordered = [...visible].sort((a, b) => {
    const pa = a.st.station_id === state.activeStation ? 0
      : a.st.station_id === state.cheapestId ? 1 : 2;
    const pb = b.st.station_id === state.activeStation ? 0
      : b.st.station_id === state.cheapestId ? 1 : 2;
    return pa - pb || a.d - b.d;
  });

  const budget = pillBudget(map.getZoom());
  const placedRects = [];
  const pillIds = new Set();
  const dotIds = new Set();

  for (const { st } of ordered) {
    const row = priceRow(st.station_id, state.fuel);
    const isActive = st.station_id === state.activeStation;
    const isCheapest = st.station_id === state.cheapestId;
    if (pillIds.size < budget) {
      const point = map.latLngToContainerPoint([st.latitude, st.longitude]);
      const rect = estimatePillRect(point, row);
      // Active and cheapest claim their space first and always win.
      if (isActive || isCheapest || !overlaps(rect, placedRects)) {
        placedRects.push(rect);
        pillIds.add(st.station_id);
        continue;
      }
    }
    dotIds.add(st.station_id);
  }

  // --- reconcile pill pool ---
  for (const { st } of ordered) {
    if (!pillIds.has(st.station_id)) continue;
    const row = priceRow(st.station_id, state.fuel);
    const cls = rowClass(row);
    const isCheapest = st.station_id === state.cheapestId;
    const isActive = st.station_id === state.activeStation;
    const key = `${state.fuel}|${cls}|${row?.verified_price ?? "-"}|${isCheapest}|${isActive}`;

    const pooled = pillPool.get(st.station_id);
    if (pooled) {
      if (pooled.key !== key) {
        pooled.marker.setIcon(pillIcon(st, row, cls, isCheapest, isActive));
        pooled.marker.setZIndexOffset(isActive ? 500 : isCheapest ? 400 : 0);
        pooled.key = key;
      }
      continue;
    }
    const marker = L.marker([st.latitude, st.longitude], {
      icon: pillIcon(st, row, cls, isCheapest, isActive),
      zIndexOffset: isActive ? 500 : isCheapest ? 400 : 0,
    })
      .addTo(map)
      .bindPopup(() => popupHTML(st), { maxWidth: 320 });
    marker.on("click", () => setActive(st.station_id, false));
    pillPool.set(st.station_id, { marker, key });
  }
  for (const [id, pooled] of pillPool) {
    if (!pillIds.has(id)) {
      pooled.marker.remove();
      pillPool.delete(id);
    }
  }

  // --- reconcile dot pool ---
  for (const { st } of visible) {
    if (!dotIds.has(st.station_id)) continue;
    const row = priceRow(st.station_id, state.fuel);
    const usable = row && row.verified_price != null;
    const key = `${st.brand}|${usable}`;
    const pooled = dotPool.get(st.station_id);
    if (pooled) {
      if (pooled.key !== key) {
        pooled.marker.setStyle({ fillOpacity: usable ? 0.95 : 0.35 });
        pooled.key = key;
      }
      continue;
    }
    const marker = L.circleMarker([st.latitude, st.longitude], {
      renderer: dotRenderer,
      radius: 5.5,
      weight: 1.5,
      color: "#ffffff",
      fillColor: BRAND_COLORS[st.brand] || BRAND_COLORS.unknown,
      fillOpacity: usable ? 0.95 : 0.35,
    })
      .addTo(map)
      .bindTooltip(() => dotTooltip(st), { direction: "top", offset: [0, -6] });
    marker.on("click", () => setActive(st.station_id, false, { popup: true }));
    dotPool.set(st.station_id, { marker, key });
  }
  for (const [id, pooled] of dotPool) {
    if (!dotIds.has(id)) {
      pooled.marker.remove();
      dotPool.delete(id);
    }
  }
}

/* ================= popup ================= */

function popupHTML(st) {
  const rows = FUELS.map(({ id, label }) => {
    const row = priceRow(st.station_id, id);
    if (!row) return "";
    if (row.verified_price == null) {
      return `<tr><td>${label}</td><td class="pt-unavail">Price unavailable</td><td></td></tr>`;
    }
    const cls = rowClass(row);
    return (
      `<tr><td>${label}</td>` +
      `<td class="pt-price">₱${row.verified_price.toFixed(2)}/L</td>` +
      `<td>${badgeFor(row, cls, false)}</td></tr>`
    );
  }).join("");

  const anyRow = Object.values(state.prices[st.station_id] || {}).find((r) => r.source_used);
  const src = anyRow
    ? `Source: ${escapeHTML(anyRow.source_used)}<br>Verified ${relTime(anyRow.verification_timestamp)}`
    : "No verified evidence yet.";
  const directions =
    `https://www.google.com/maps/dir/?api=1&destination=${st.latitude},${st.longitude}`;

  return (
    `<div class="popup-title">${escapeHTML(st.official_name)}</div>` +
    `<div class="popup-sub">${escapeHTML(st.address)}, ${escapeHTML(st.city)}</div>` +
    (rows ? `<table class="popup-table">${rows}</table>` : "") +
    `<div class="popup-foot"><div class="popup-src">${src}</div>` +
    `<a class="popup-directions" href="${directions}" target="_blank" rel="noopener">Directions ↗</a></div>`
  );
}

/* ================= sidebar ================= */

function renderSidebar() {
  if (!state.dataLoaded) return;
  const listEl = document.getElementById("station-list");
  listEl.classList.remove("loading");
  const origin = state.userPos || [map.getCenter().lat, map.getCenter().lng];

  const sorted = state.stations
    .map((st) => ({ st, dist: haversineKm(origin, [st.latitude, st.longitude]) }))
    .sort((a, b) => a.dist - b.dist)
    .slice(0, SIDEBAR_LIMIT);

  document.getElementById("station-count").textContent = state.userPos
    ? `${SIDEBAR_LIMIT} of ${state.stations.length} · from ${state.userIsManual ? "pinned spot" : "you"}`
    : `${SIDEBAR_LIMIT} of ${state.stations.length} · from map center`;

  let cheapestNearby = null;
  let bestPrice = Infinity;
  for (const { st } of sorted) {
    const row = priceRow(st.station_id, state.fuel);
    if (row && row.verified_price != null && row.verified_price < bestPrice) {
      bestPrice = row.verified_price;
      cheapestNearby = st.station_id;
    }
  }

  const frag = document.createDocumentFragment();
  for (const { st, dist } of sorted) {
    const row = priceRow(st.station_id, state.fuel);
    const cls = rowClass(row);
    const color = BRAND_COLORS[st.brand] || BRAND_COLORS.unknown;
    const isCheapest = st.station_id === cheapestNearby;

    const card = document.createElement("div");
    card.className = "station-card"
      + (state.activeStation === st.station_id ? " active" : "")
      + (isCheapest ? " cheapest-card" : "");

    let priceHTML, badgeHTML = "", metaHTML = "";
    if (row && row.verified_price != null) {
      priceHTML =
        `<span class="card-price">₱${row.verified_price.toFixed(2)}</span>` +
        `<span class="card-fuel">/L ${FUEL_LABELS[state.fuel]}</span>`;
      badgeHTML = badgeFor(row, cls, true);
      metaHTML = `<div class="card-meta">via ${escapeHTML(row.source_used || "—")} · ${relTime(row.verification_timestamp)}</div>`;
    } else {
      priceHTML =
        `<span class="card-price unavailable">Price unavailable</span>` +
        `<span class="card-fuel">${FUEL_LABELS[state.fuel]}</span>`;
    }

    card.innerHTML =
      (isCheapest ? `<span class="cheapest-tag">Cheapest nearby</span>` : "") +
      `<div class="card-top">
         <span class="brand-chip" style="background:${color}">${st.brand.replace("_", " ")}</span>
         <span class="card-name" title="${escapeHTML(st.official_name)}">${escapeHTML(st.official_name)}</span>
         <span class="card-distance">${dist < 1 ? Math.round(dist * 1000) + " m" : dist.toFixed(1) + " km"}</span>
       </div>
       <div class="card-price-row">${priceHTML}${badgeHTML}</div>${metaHTML}`;
    card.addEventListener("click", () => setActive(st.station_id, true));
    frag.appendChild(card);
  }

  listEl.replaceChildren(frag);
}

function setActive(stationId, fly, { popup = false } = {}) {
  state.activeStation = stationId;
  renderSidebar();
  renderMarkers(); // the active station is always promoted to a pill
  const st = state.stations.find((s) => s.station_id === stationId);
  if (fly && st) {
    map.flyTo([st.latitude, st.longitude], Math.max(map.getZoom(), 15.5), { duration: 0.7 });
    map.once("moveend", () => pillPool.get(stationId)?.marker.openPopup());
  } else if (popup) {
    pillPool.get(stationId)?.marker.openPopup();
  }
}

function renderAll() {
  renderMarkers();
  renderSidebar();
}

/* ================= utilities ================= */

function haversineKm([lat1, lon1], [lat2, lon2]) {
  const R = 6371, rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad, dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function relTime(iso) {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 0 || Number.isNaN(s)) return "—";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function escapeHTML(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

function setStatus(text) {
  document.getElementById("statusline").textContent = text;
}

/* ================= wiring ================= */

const chipsEl = document.getElementById("fuel-chips");
for (const { id, label } of FUELS) {
  const chip = document.createElement("button");
  chip.className = "chip" + (id === state.fuel ? " selected" : "");
  chip.textContent = label;
  chip.addEventListener("click", () => {
    state.fuel = id;
    for (const c of chipsEl.children) c.classList.remove("selected");
    chip.classList.add("selected");
    renderAll();
  });
  chipsEl.appendChild(chip);
}

document.getElementById("refresh-btn").addEventListener("click", refreshPrices);

let renderQueued = false;
map.on("moveend zoomend", () => {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderMarkers();
    if (!state.userPos) renderSidebar();
  });
});

restoreSavedLocation();
loadData().catch((err) => {
  setStatus("Could not reach the Fuel Intelligence Engine");
  toast(err.message, true);
});
acquireLocation({ fly: true });
