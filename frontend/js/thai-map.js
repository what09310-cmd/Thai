// thai-map.js -- Carte Thailande (vip, carte-thailande): pins, chargement, filtres budget/duree/equipements.
// Partage par: vip, carte-thailande.
// Script classique (pas de module): tout ce qui est declare ici est global.

function pinIcon(){
  return L.divIcon({ className: "", html: `<div class="pin-icon"></div>`, iconSize: [12, 12] });
}

function applyFilters(){
  const durationField = DURATION_FIELD[selectedDuration] || null;

  const filtered = ads.filter(a => {
    if (durationField && !hasRaw(a[`${durationField}_raw`])) return false;
    if (onlyNew) {
      const cutoff = Date.now() - 24 * 60 * 60 * 1000;
      if (new Date(a.first_seen_at || 0).getTime() < cutoff) return false;
    }
    if (selectedBudget) {
      const minThb = selectedBudget.min * EUR_TO_THB;
      const maxThb = selectedBudget.max * EUR_TO_THB;
      const p = priceOf(a, durationField);
      if (p == null || p < minThb || p > maxThb) return false;
    }
    if (selectedAmenities.size) {
      const amenities = a.amenities || [];
      for (const am of selectedAmenities) {
        if (!amenities.includes(am)) return false;
      }
    }
    return true;
  });
  clusterGroup.clearLayers();
  clusterGroup.addLayers(filtered.map(a => markersById[a.id]).filter(Boolean));

  const countEl = document.getElementById("result-count");
  if (countEl) countEl.textContent = `${filtered.length.toLocaleString("fr-FR")} annonce${filtered.length > 1 ? "s" : ""}`;
}

async function loadAds(){
  const limit = 500;
  let offset = 0;
  const data = [];

  try {
    while (true) {
      const r = await fetch(`${API}/listings?status=active&limit=${limit}&offset=${offset}`);
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

  ads = data.filter(a => Number.isFinite(a.latitude) && Number.isFinite(a.longitude));

  if (!ads.length) {
    document.getElementById("leaflet-map").innerHTML = `<div class="empty">Aucune annonce géolocalisée.</div>`;
    return;
  }

  buildMap();
  setupFormFilters();
  applyFilters();
}
