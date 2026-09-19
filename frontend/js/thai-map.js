// thai-map.js -- Carte Thailande (vip, carte-thailande, resultats): pins, chargement,
// filtres budget/duree/equipements, popup riche + modale (photos, specs, contact).
// Partage par: vip, carte-thailande, resultats.
// Script classique (pas de module): tout ce qui est declare ici est global.

// Derniere liste filtree (budget/duree/equipements) calculee par
// applyFilters() -- renderVisibleResults() la recroise avec les bornes
// actuelles de la carte a chaque pan/zoom, independamment des marqueurs
// (les marqueurs affichent toujours tout `filtered`, seule la colonne
// resultats se restreint a ce qui est visible a l'ecran).
let lastFiltered = [];

function renderVisibleResults(){
  if (!leafletMap) { renderResultsList(lastFiltered); return; }
  const bounds = leafletMap.getBounds();
  const visible = lastFiltered.filter(a => bounds.contains([a.latitude, a.longitude]));
  renderResultsList(visible);
}

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
        // LINE/WhatsApp ne sont pas des equipements (pas dans a.amenities):
        // ce sont des champs de contact a part, testes ici comme des
        // pseudo-equipements pour reutiliser les memes chips de filtre.
        if (am === "LINE") { if (!a.line_id) return false; continue; }
        if (am === "WhatsApp") { if (!a.whatsapp || String(a.whatsapp).replace(/\D/g, "").length < 5) return false; continue; }
        if (!amenities.includes(am)) return false;
      }
    }
    return true;
  });
  clusterGroup.clearLayers();
  clusterGroup.addLayers(filtered.map(a => markersById[a.id]).filter(Boolean));

  const countEl = document.getElementById("result-count");
  if (countEl) countEl.textContent = `${filtered.length.toLocaleString("fr-FR")} annonce${filtered.length > 1 ? "s" : ""}`;

  // Colonne resultats (layout 3 colonnes): restreinte a ce qui est visible
  // dans le cadre actuel de la carte -- voir renderVisibleResults ci-dessus.
  lastFiltered = filtered;
  renderVisibleResults();
}

// ── Colonne resultats (cards) ────────────────────────────────────────
// Rendu adapte de cardHTML() (index.html/frontend/js/catalog.js, en LECTURE
// seule): memes classes CSS de card (deja chargees ici via modal.css), pour
// un style identique au reste du site, mais sans les toggles "X mois
// uniquement" de la page catalogue (absents de cette page) -- on affiche
// simplement les durees de contrat disponibles.
function resultCardHTML(ad){
  const district = ad.district || ad.subdistrict || "";
  const c1 = contractVal(ad.contract_monthly_raw);
  const c3 = contractVal(ad.contract_3_month_raw);
  const c6 = contractVal(ad.contract_6_month_raw);
  const updated = ad.source_updated_at ? new Date(ad.source_updated_at).toLocaleDateString("fr-FR") : "";

  const boxes = [
    ad.has_monthly_contract === "true" && c1 !== "—" ? `<div class="cc"><div class="cc-label">1 mois</div><div class="cc-val">${c1}</div></div>` : "",
    c3 !== "—" ? `<div class="cc"><div class="cc-label">3 mois</div><div class="cc-val">${c3}</div></div>` : "",
    c6 !== "—" ? `<div class="cc"><div class="cc-label">6 mois</div><div class="cc-val">${c6}</div></div>` : "",
  ].filter(Boolean);

  return `<div class="card" tabindex="0" role="button" aria-label="Voir le détail : ${escapeHtml(ad.name || "annonce")}" onclick="openModal(${ad.id})" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openModal(${ad.id});}">
    <div class="card-img">${ad.images && ad.images.length
      ? `<img src="${escapeHtml(ad.images[0])}" alt="" loading="lazy" draggable="false" oncontextmenu="return false" onerror="this.parentElement.innerHTML='<div class=&quot;no-img&quot;>🏠</div>'">`
      : `<div class="no-img">🏠</div>`}</div>
    <div class="card-badge-slot">${ad.has_monthly_contract === "true" && c1 !== "—" ? `<span class="badge badge-monthly" style="position:static;display:inline-block">1 MOIS</span>` : ""}${c3 !== "—" ? `<span class="badge badge-term-3" style="position:static;display:inline-block">3 MOIS</span>` : ""}${c6 !== "—" ? `<span class="badge badge-term-6" style="position:static;display:inline-block">6 MOIS</span>` : ""}</div>
    <div class="card-body">
      <div class="card-loc">
        ${district ? `<span class="loc-district">${escapeHtml(district)}</span>` : ""}
        ${ad.province ? `<span class="loc-province">${escapeHtml(ad.province)}</span>` : ""}
      </div>
      ${boxes.length ? `<div class="card-contracts">${boxes.join("")}</div>` : ""}
      ${updated ? `<div class="card-date">Mis à jour: ${updated}</div>` : ""}
    </div>
  </div>`;
}

// Meme source filtree que la carte (applyFilters() l'appelle juste apres
// avoir recalcule clusterGroup) -- liste et markers restent synchronises.
function renderResultsList(list){
  const wrap = document.getElementById("results-list");
  if (!wrap) return; // vip.html/resultats.html n'ont pas cette colonne.

  if (!list.length) {
    wrap.innerHTML = `<div class="state-box">Aucune annonce<br><small style="margin-top:6px;color:var(--muted)">Élargissez les filtres</small></div>`;
    return;
  }

  const sortSel = document.getElementById("result-sort");
  const sort = sortSel ? sortSel.value : "updated";
  const sorted = list.slice().sort((a, b) => {
    if (sort === "price_asc") return (priceValue(a) ?? Infinity) - (priceValue(b) ?? Infinity);
    if (sort === "price_desc") return (priceValue(b) ?? -Infinity) - (priceValue(a) ?? -Infinity);
    return new Date(b.source_updated_at || 0) - new Date(a.source_updated_at || 0); // "updated" par defaut
  });

  wrap.innerHTML = sorted.map(resultCardHTML).join("");
}

async function loadAds(){
  const limit = 500;
  // /resultats (tunnel /789) declare LOCATION_QUERY ("province=Bangkok" ou
  // "district=Bang Lamung") avant d'appeler loadAds(): meme principe que
  // CITY.query dans city-map.js, applique ici en plus du filtre budget/duree
  // deja client. vip.html/carte-thailande.html ne la definissent pas: carte
  // nationale inchangee.
  const locationQuery = typeof LOCATION_QUERY !== "undefined" && LOCATION_QUERY ? `${LOCATION_QUERY}&` : "";

  async function fetchBatch(offset) {
    const r = await fetch(`${API}/listings?${locationQuery}status=active&limit=${limit}&offset=${offset}`);
    if (!r.ok) throw new Error(r.status);
    const batch = await r.json();
    if (!Array.isArray(batch)) throw new Error("Réponse /listings invalide");
    return batch;
  }

  let first;
  try {
    first = await fetchBatch(0);
  } catch (e) {
    document.getElementById("leaflet-map").innerHTML =
      `<div class="empty">Impossible de charger les annonces pour le moment.<br><br>Le serveur met parfois une minute à démarrer : recharge la page dans quelques instants.</div>`;
    return;
  }

  ads = first.filter(a => Number.isFinite(a.latitude) && Number.isFinite(a.longitude));

  if (!ads.length) {
    document.getElementById("leaflet-map").innerHTML = `<div class="empty">Aucune annonce géolocalisée.</div>`;
    return;
  }

  buildMap();
  setupFormFilters();
  applyFilters();

  // Charge le reste en arriere-plan: la carte s'affiche des le premier lot
  // au lieu d'attendre tout le catalogue.
  if (first.length === limit) {
    (async () => {
      let offset = limit;
      let last = first.length;
      try {
        while (last === limit) {
          const batch = await fetchBatch(offset);
          const geo = batch.filter(a => Number.isFinite(a.latitude) && Number.isFinite(a.longitude));
          ads.push(...geo);
          addMarkersFor(geo);
          applyFilters();
          last = batch.length;
          offset += limit;
        }
      } catch (e) { console.error(e); }
    })();
  }
}

// ── Popup riche + modale (extrait de carte-thailande.html: /resultats,
// derriere le tunnel /789, reutilise exactement ce que vip.html/
// carte-thailande.html affichent deja, sans deuxieme implementation Leaflet).

const APPROX_RADIUS_M = 350;

const POPUP_MAX_CHIPS = 6;

const detailCache = {};

function popupSpec(label, value, cls){
  const has = value != null && value !== "" && value !== "-" && value !== "Please contact";
  return `<div class="popup-spec">` +
    `<span class="popup-spec-label">${escapeHtml(label)}</span>` +
    `<span class="popup-spec-val${has ? (cls ? " " + cls : "") : " na"}">${has ? escapeHtml(value) : "Non communiqu&eacute;"}</span>` +
    `</div>`;
}

function popupPhotosHTML(ad, images){
  const list = (images && images.length) ? images : (ad.images || []);
  if (!list.length) return `<div class="popup-photos is-empty"></div>`;
  const shown = list.slice(0, 10);
  return `<div class="popup-photos-wrap">` +
    `<div class="popup-photos">` +
    shown.map((u, i) => `<div class="popup-photo">` +
      `<img src="${escapeHtml(u)}" alt="" loading="lazy" draggable="false" oncontextmenu="return false" ` +
      `onerror="this.parentElement.style.display='none'">` +
      watermarkHTML() +
      `</div>`).join("") +
    `</div>` +
    (shown.length > 1 ? `<div class="popup-photos-count">&#x1F4F7; ${shown.length}</div>` : "") +
    `</div>`;
}

// Un visiteur non premium recoit deja `url: null` depuis l'API
// (main.py::_listing_to_response) -- jamais de logique de masquage ici,
// seulement l'affichage qui en decoule. SHOW_LOCK_BUTTONS (map.js) ne
// change que la mise en page du bloc, pas la donnee.
function popupActionsHTML(ad){
  const locked = ad.url == null;
  if (SHOW_LOCK_BUTTONS && locked) {
    return `<div class="popup-actions popup-actions-stacked">` +
      `<a href="/payant.html?listing=${encodeURIComponent(ad.id)}" class="popup-btn popup-btn-lock">🔒 Voir le contact du propriétaire</a>` +
      `<a href="/payant.html?listing=${encodeURIComponent(ad.id)}" class="popup-btn popup-btn-lock">🔒 Voir l'annonce d'origine</a>` +
      `</div>`;
  }
  return `<div class="popup-actions">` +
    `<a href="payant.html?listing=${encodeURIComponent(ad.id)}" class="popup-btn">Voir l'annonce complète</a>` +
    `</div>`;
}

function popupHTML(ad, images){
  const thb = priceValue(ad);
  const amenities = ad.amenities || [];
  const hasAc = amenities.includes("Air Conditioner");
  const chips = amenityChips(ad);
  const shownChips = chips.slice(0, POPUP_MAX_CHIPS);
  const extra = chips.length - shownChips.length;
  const locs = [ad.subdistrict, ad.district, ad.province].filter(Boolean);
  const badges =
    (ad.has_monthly_contract === "true" && contractNum(ad.contract_monthly_raw) !== null ? `<span class="popup-badge popup-badge-1">1 mois</span>` : "") +
    (contractNum(ad.contract_3_month_raw) !== null ? `<span class="popup-badge popup-badge-3">3 mois</span>` : "") +
    (contractNum(ad.contract_6_month_raw) !== null ? `<span class="popup-badge popup-badge-6">6 mois</span>` : "");

  const priceHTML = thb
    ? `<span class="popup-price-eur">${Math.round(thb / EUR_TO_THB).toLocaleString("fr-FR")} &euro;</span>` +
      `<span class="popup-price-unit">/ mois</span>`
    : `<span class="popup-price-unit">Prix non communiqu&eacute;</span>`;

  return `<div class="popup-card">` +
    `<div class="popup-photos-slot">${popupPhotosHTML(ad, images)}</div>` +
    `<div class="popup-body">` +
      `<div>` +
        `<div class="popup-price">${priceHTML}</div>` +
        (locs.length ? `<div class="popup-loc">&#x1F4CD; ${locs.map(escapeHtml).join(" &middot; ")}</div>` : "") +
        (badges ? `<div class="popup-badges">${badges}</div>` : "") +
      `</div>` +
      `<div class="popup-specs">` +
        popupSpec("Deposit", ad.deposit) +
        popupSpec("Électricité", ad.electric_price) +
        popupSpec("Climatisation", hasAc ? "✓ Oui" : "Non", hasAc ? "yes" : "") +
      `</div>` +
      `<div><div class="popup-section-head">${escapeHtml(contractsSectionTitle(ad))}</div>` +
      `<div class="popup-rooms">${renderContractsHTML(ad)}</div></div>` +
      (shownChips.length ? `<div class="popup-amenities">` +
        shownChips.map(c => `<span class="popup-chip">${escapeHtml(AMENITY_ICONS[c])}</span>`).join("") +
        (extra > 0 ? `<span class="popup-chip popup-chip-more">+${extra}</span>` : "") +
      `</div>` : "") +
    `</div>` +
    popupActionsHTML(ad) +
  `</div>`;
}

/** Remplace la vignette unique du popup par la galerie complete de l'annonce. */
async function loadPopupPhotos(ad){
  const marker = markersById[ad.id];
  if (!marker) return;

  const slotOf = () => {
    const popup = marker.getPopup();
    if (!popup || !popup.isOpen()) return null;
    const el = popup.getElement();
    return el ? el.querySelector(".popup-photos-slot") : null;
  };

  const paint = images => {
    const slot = slotOf();
    if (!slot) return;
    slot.innerHTML = popupPhotosHTML(ad, images);
    marker.getPopup().update();
  };

  if (detailCache[ad.id]) { paint(detailCache[ad.id]); return; }

  // Skeleton seulement s'il n'y a rien a montrer en attendant.
  if (!(ad.images || []).length) {
    const slot = slotOf();
    if (slot) slot.innerHTML = `<div class="popup-photos"><div class="popup-photos-skel"></div></div>`;
  }

  try {
    const l = await fetch(`${API}/listings/${ad.id}`).then(r => r.json());
    detailCache[ad.id] = l.images || [];
  } catch (e) {
    detailCache[ad.id] = ad.images || [];
  }
  paint(detailCache[ad.id]);
}

// Cree les marqueurs Leaflet pour une liste d'annonces sans toucher a la
// carte/au clusterGroup (utilise pour le rendu initial et pour les lots
// charges en arriere-plan par loadAds()).
function addMarkersFor(list){
  list.forEach(ad => {
    // Visiteur non connecte: l'API decale la position et le signale par
    // location_approx (main.py::_approximate_position). Comme Airbnb, on
    // dessine alors une zone plutot qu'un point; le rayon doit rester
    // >= FUZZ_MAX_M cote API pour que la vraie position soit dedans.
    const marker = ad.location_approx
      ? L.circle([ad.latitude, ad.longitude], {
          radius: APPROX_RADIUS_M, color: "var(--accent)", weight: 2,
          fillColor: "var(--accent)", fillOpacity: .18,
        })
      : L.marker([ad.latitude, ad.longitude], { icon: pinIcon() });
    marker
      .bindPopup(popupHTML(ad), { maxWidth: 390, minWidth: 390, className: "popup-lg", autoPanPadding: [24, 24] });
    // La liste ne renvoie qu'une vignette par annonce (_thumbnail_map cote
    // API): la galerie complete n'est chargee qu'a l'ouverture du popup,
    // pour ne pas tirer ~45 images par annonce au chargement de la carte.
    marker.on("popupopen", () => loadPopupPhotos(ad));
    markersById[ad.id] = marker;
  });
}

function buildMap(){
  leafletMap = L.map("leaflet-map", { zoomControl: false });
  L.control.zoom({ position: "topright" }).addTo(leafletMap);
  tileLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(leafletMap);

  clusterGroup = L.markerClusterGroup({ maxClusterRadius: 50 });
  leafletMap.addLayer(clusterGroup);

  addMarkersFor(ads);

  const bounds = L.latLngBounds(ads.map(a => [a.latitude, a.longitude]));
  leafletMap.fitBounds(bounds, { padding: [30, 30] });

  // Layout 3 colonnes: #leaflet-map n'est plus plein-viewport en position
  // absolute, sa taille depend maintenant du flex de .col-map calcule par
  // le navigateur. Sans invalidateSize() Leaflet garde les dimensions
  // mesurees a l'instant de L.map() (souvent 0x0 avant le premier layout),
  // et les tuiles s'affichent tronquees/mal centrees.
  setTimeout(() => leafletMap.invalidateSize(), 0);
  window.addEventListener("resize", () => { if (leafletMap) leafletMap.invalidateSize(); });

  // Colonne resultats: ne montre que les annonces dans le cadre actuel de
  // la carte (comme Booking) -- se met a jour a chaque pan/zoom.
  leafletMap.on("moveend", renderVisibleResults);
}

// La carte ne charge pas les caches de geocodage d'index.html : seules les
// coordonnees propres de l'annonce servent (une annonce sans position
// n'apparait de toute facon pas sur la carte).
function mapPoint(l) {
  if (l.latitude != null && l.longitude != null) {
    return { lat: l.latitude, lon: l.longitude, precise: !l.location_approx };
  }
  return null;
}

async function openModal(id) {
  lastFocusedEl = document.activeElement;
  document.getElementById("overlay").classList.add("open");
  lockBodyScroll();
  const body = document.getElementById("modal-body");
  body.innerHTML = `<div class="state-box"><div class="spinner"></div></div>`;
  body.focus();
  try {
    const l = await fetch(`${API}/listings/${id}`).then(r => r.json());
    body.innerHTML = modalHTML(l);
    currentGalleryImages = l.images || [];
  } catch(e) {
    body.innerHTML = `<div class="state-box">Erreur de chargement</div>`;
  }
}

function modalHTML(l) {
  const locs = [l.subdistrict, l.district, l.province].filter(Boolean);
  const amenities = l.amenities || [];
  const mapsUrl = googleMapsUrl(l);
  const embedUrl = googleMapsEmbedUrl(l);
  const pt = mapPoint(l);
  const precisionLabel = !pt ? ""
    : pt.precise ? "📌 Position précise"
    : l.location_approx ? "🔵 Position approximative (zone de ~350 m — connectez-vous pour la position exacte)"
    : "🔵 Position approximative (centre du quartier — RentHub ne publie pas l'adresse exacte)";

  return `
    <div class="modal-top">
      <button class="modal-close" onclick="closeModal()" aria-label="Fermer">✕</button>
    </div>
    ${l.images && l.images.length ? `
    <div class="modal-gallery">
      ${l.images.map((u, i) => `<div class="modal-gallery-item"><img src="${esc(u)}" alt="" loading="lazy" draggable="false" oncontextmenu="return false" onclick="openLightbox(${i})" onerror="this.parentElement.style.display='none'">${watermarkHTML()}</div>`).join("")}
    </div>` : ""}
    <div class="modal-tags">
      ${locs.map(loc => `<span class="tag">${esc(loc)}</span>`).join("")}
      ${l.has_monthly_contract === "true" && contractVal(l.contract_monthly_raw) !== "—" ? `<span class="badge badge-monthly" style="position:static;display:inline-block">1 MOIS</span>` : ""}
      ${contractVal(l.contract_3_month_raw) !== "—" ? `<span class="badge badge-term-3" style="position:static;display:inline-block">3 MOIS</span>` : ""}
      ${contractVal(l.contract_6_month_raw) !== "—" ? `<span class="badge badge-term-6" style="position:static;display:inline-block">6 MOIS</span>` : ""}
    </div>
    <div>
      <div class="section-head">${esc(contractsSectionTitle(l))}</div>
      <div class="modal-contracts">
        ${renderContractsHTML(l)}
      </div>
    </div>
    ${(()=>{const hasWa=hasRealWhatsapp(l.whatsapp);return l.phone||l.line_id||hasWa ? `
    <div>
      <div class="section-head">Contact</div>
      <div class="modal-contracts">
        ${l.phone?`<a class="mc mc-phone" href="tel:${encodeURIComponent(String(l.phone).replace(/[^+\d]/g,""))}"><div class="mc-label">Téléphone</div><div class="mc-val">📞 ${esc(l.phone)}</div></a>`:""}
        ${l.line_id?(()=>{
          // line_verified===false : identifiant deja confirme inexistant par
          // scripts/verify_line_ids.py -- on route vers line.me/ti/p/~<id>
          // pour eviter un 404 (voir Listing.line_verified). Sinon (valide ou
          // jamais verifie), page.line.me/<id> reste le lien qui marche vraiment.
          const slug = encodeURIComponent(String(l.line_id).replace(/^@/,""));
          const href = l.line_verified===false ? `https://line.me/ti/p/~${slug}` : `https://page.line.me/${slug}`;
          return `<a class="mc mc-line" href="${href}" target="_blank" rel="noopener"><div class="mc-label">LINE</div><div class="mc-val" style="display:flex;align-items:center;gap:6px">${LINE_ICON_SVG} ${esc(l.line_id)}</div></a>`;
        })():""}
        ${hasWa?`<a class="mc mc-wa" href="https://api.whatsapp.com/send/?phone=${encodeURIComponent(String(l.whatsapp).replace(/\D/g,""))}&text&type=phone_number&app_absent=0" target="_blank" rel="noopener"><div class="mc-label">WhatsApp</div><div class="mc-val" style="display:flex;align-items:center;gap:6px">${WHATSAPP_ICON_SVG} ${esc(formatWhatsapp(l.whatsapp))}</div></a>`:""}
      </div>
    </div>` : "";})()}
    ${l.deposit || (l.electric_price && l.electric_price !== "Please contact") || amenities.length ? `
    <div>
      <div class="section-head">Infos pratiques</div>
      <div class="modal-contracts">
        <div class="mc mc-compact"><div class="mc-label">Deposit</div><div class="mc-val${l.deposit ? "" : " na"}">${l.deposit ? esc(l.deposit) : "Non communiqué"}</div></div>
        ${l.electric_price && l.electric_price !== "Please contact" ? `<div class="mc mc-compact"><div class="mc-label">Electric price</div><div class="mc-val">${esc(l.electric_price)}</div></div>` : ""}
        <div class="mc mc-compact"><div class="mc-label">Air Conditioner</div><div class="mc-val${amenities.includes("Air Conditioner") ? "" : " na"}">${amenities.includes("Air Conditioner") ? "YES" : "NO"}</div></div>
      </div>
    </div>` : ""}
    ${amenities.length ? `
    <div>
      <div class="section-head">Équipements</div>
      <div class="modal-amenities">${amenities.map(a=>`<span class="amenity">${esc(AMENITY_ICONS[a] || a)}</span>`).join("")}</div>
    </div>` : ""}
    ${mapsUrl ? `
    <div>
      <div style="display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap">
        <a class="modal-link" style="padding:6px 12px;font-size:11px" href="${esc(mapsUrl)}" target="_blank" rel="noopener">📍 Google Maps ↗</a>
      </div>
      ${embedUrl ? `<iframe src="${esc(embedUrl)}" style="width:100%;height:220px;border:0;border-radius:8px;margin-top:10px" loading="lazy" referrerpolicy="no-referrer-when-downgrade" title="Carte de localisation"></iframe>` : ""}
      ${precisionLabel ? `<div style="font-size:10px;color:var(--muted);margin-top:6px">${precisionLabel}</div>` : ""}
    </div>` : ""}
    ${l.url
      ? `<a class="modal-link" href="${safeHref(l.url)}" target="_blank" rel="noopener">Voir sur RentHub ↗</a>`
      : `<a class="modal-link" href="/payant.html?listing=${encodeURIComponent(l.id)}">🔒 Débloquer l'annonce d'origine</a>`}
  `;
}

// L'ecoute clavier Echap/fleches + trapModalFocus + le swipe tactile du
// lightbox restent inline, page par page (comme index.html/premium.html/
// test.html): vip.html charge ce fichier mais n'a pas de #overlay/
// #lightbox-overlay dans son DOM, et trapModalFocus/closeModal y planteraient
// au premier keydown si on les enregistrait ici sans condition.

// Pas de clic droit / glisser / selection sur les photos (popup de la carte,
// galerie de la modale, lightbox). Le popup Leaflet est insere dynamiquement,
// d'ou l'ecoute au niveau du document.
["contextmenu", "dragstart", "selectstart", "copy"].forEach(eventName => {
  document.addEventListener(eventName, event => {
    if (event.target.closest(".popup-photos-wrap, .modal-gallery, .lightbox-overlay")) event.preventDefault();
  });
});
