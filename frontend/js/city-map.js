// city-map.js -- Carte d'une ville (carte-bangkok, carte-pattaya, carte-phuket).
// La page declare `CITY` avant de charger ce script:
//   { name: "Bangkok", query: "province=Bangkok", cluster: true, pinSize: 12, pageLimit: 500 }
// query    : filtre de /listings (province=... ou district=...)
// cluster  : regroupe les marqueurs (Leaflet.markercluster doit etre charge)
// pinSize  : diametre du pin en px (doit suivre .pin-icon de la page)
// pageLimit: taille des pages de /listings
// Script classique (pas de module): s'appuie sur map.js (etat, priceLabel...).

function pinIcon() {
  return L.divIcon({ className: "", html: `<div class="pin-icon"></div>`, iconSize: [CITY.pinSize, CITY.pinSize] });
}

function buildMap() {
  leafletMap = L.map("leaflet-map");
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(leafletMap);

  if (CITY.cluster) {
    clusterGroup = L.markerClusterGroup({ maxClusterRadius: 40 });
    leafletMap.addLayer(clusterGroup);
  }

  ads.forEach((ad) => {
    const price = priceLabel(ad);
    const marker = L.marker([ad.latitude, ad.longitude], { icon: pinIcon() })
      .bindPopup(
        `<div class="popup-price">${price ? escapeHtml(price) + " / mois" : "Prix non disponible"}</div>` +
        `<a href="/payant.html?listing=${ad.id}" class="popup-btn">Voir l'annonce complète</a>`
      );
    markersById[ad.id] = marker;
  });

  const bounds = L.latLngBounds(ads.map((a) => [a.latitude, a.longitude]));
  leafletMap.fitBounds(bounds, { padding: [30, 30] });
}

function applyFilters() {
  const priceActive = priceMin > 0 || priceMax < priceBoundsMax;
  const filtered = ads.filter((a) => {
    if (monthlyOnly && a.has_monthly_contract !== "true") return false;
    if (priceActive) {
      const p = priceValue(a);
      if (p == null || p < priceMin || p > priceMax) return false;
    }
    return true;
  });
  if (CITY.cluster) {
    clusterGroup.clearLayers();
    clusterGroup.addLayers(filtered.map((a) => markersById[a.id]).filter(Boolean));
    return;
  }
  const ids = new Set(filtered.map((a) => a.id));
  Object.entries(markersById).forEach(([id, marker]) => {
    const shouldShow = ids.has(Number(id));
    const onMap = leafletMap.hasLayer(marker);
    if (shouldShow && !onMap) marker.addTo(leafletMap);
    if (!shouldShow && onMap) leafletMap.removeLayer(marker);
  });
}

async function loadAds() {
  const limit = CITY.pageLimit;
  let offset = 0;
  const data = [];

  try {
    while (true) {
      const r = await fetch(`${API}/listings?${CITY.query}&status=active&limit=${limit}&offset=${offset}`);
      if (!r.ok) throw new Error(r.status);
      const batch = await r.json();
      if (!Array.isArray(batch)) throw new Error("Réponse /listings invalide");
      data.push(...batch);
      if (batch.length < limit) break;
      offset += limit;
    }
  } catch (e) {
    document.getElementById("leaflet-map").innerHTML =
      `<div class="empty">Impossible de charger les annonces pour le moment.<br><br>Le serveur met parfois une minute à démarrer : recharge la page dans quelques instants.</div>`;
    return;
  }

  ads = data.filter((a) => Number.isFinite(a.latitude) && Number.isFinite(a.longitude));

  if (!ads.length) {
    document.getElementById("leaflet-map").innerHTML = `<div class="empty">Aucune annonce géolocalisée pour ${escapeHtml(CITY.name)}.</div>`;
    return;
  }

  buildMap();
  setupPriceFilter();
  applyFilters();

  document.getElementById("monthlyToggle").addEventListener("click", (e) => {
    monthlyOnly = !monthlyOnly;
    e.target.classList.toggle("active", monthlyOnly);
    applyFilters();
  });
}

loadAds();
