// catalog.js -- Catalogue (index.html, premium.html): chargement, filtres, menus de lieux, pagination.
// Partage par: index, premium.
// Script classique (pas de module): tout ce qui est declare ici est global.

const PER_PAGE = 30;

let filtered = [];

let page = 1;

let activeCity = "";

let activeDistrict = "";

let onlyNew24h = false;

let featuredCity = "";

let activeRegion = "";

async function loadStats() {
  try {
    const s = await fetch(`${API}/stats`).then(r => r.json());
    document.getElementById("h-total").textContent = s.total_active.toLocaleString();
    document.getElementById("h-monthly").textContent = s.monthly_contract_count.toLocaleString();
    document.getElementById("h-3month").textContent = s.three_month_contract_count.toLocaleString();
    document.getElementById("h-6month").textContent = s.six_month_contract_count.toLocaleString();

    // Comptages par ville dans les boutons
    const map = {};
    (s.by_province || []).forEach(r => { map[r.province] = r.count; });

    document.querySelectorAll(".city-btn[data-province]").forEach(btn => {
      const prov = btn.dataset.province;
      if (!prov) return;
      const count = map[prov];
      const key = prov.replace(/\s+/g, "");
      const el = document.getElementById("count-" + key);
      if (el && count) el.textContent = `(${count})`;
    });
  } catch(e) {}
}

// Charge tout le catalogue par pages de 500. La premiere page part seule
// (elle dit s'il y en a d'autres), les suivantes sont demandees par lots en
// parallele: en serie, chaque page attendait la precedente. `status=active`
// evite de telecharger puis d'ecarter cote client les annonces retirees.
const PAGE_LIMIT = 500;
const PARALLEL_PAGES = 3;

async function fetchPage(query, offset) {
  const r = await fetch(`${API}/listings?${query}&limit=${PAGE_LIMIT}&offset=${offset}`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const batch = await r.json();
  if (!Array.isArray(batch)) throw new Error("Réponse /listings invalide");
  return batch;
}

async function fetchAllListings(query) {
  const all = await fetchPage(query, 0);
  let offset = PAGE_LIMIT;
  let last = all.length;
  while (last === PAGE_LIMIT) {
    const offsets = Array.from({ length: PARALLEL_PAGES }, (_, i) => offset + i * PAGE_LIMIT);
    const batches = await Promise.all(offsets.map((o) => fetchPage(query, o)));
    for (const batch of batches) all.push(...batch);
    last = batches[batches.length - 1].length;
    offset += PARALLEL_PAGES * PAGE_LIMIT;
  }
  return all;
}

async function loadListings() {
  showLoading();
  try {
    allData = await fetchAllListings("status=active");

    applyUrlParams();
    buildLocationMenus();
    _updateNew24hCount();
    _updateFeaturedCounts();
    applyFilters();
  } catch(e) {
    console.error(e);
    document.getElementById("grid-wrap").innerHTML =
      `<div class="state-box">❌ API inaccessible<br><small style="margin-top:6px;color:var(--muted)">Lancez uvicorn sur ${API}</small></div>`;
  }
}

// Pré-remplit les filtres depuis l'URL (ex: redirection depuis le
// formulaire de payant.html vers les annonces correspondantes).
function applyUrlParams() {
  const params = new URLSearchParams(window.location.search);
  const city = params.get("city");
  const min = params.get("min");
  const max = params.get("max");
  const monthly = params.get("monthly");

  if (city) activeCity = city;
  if (min) document.getElementById("f-min").value = min;
  if (max) document.getElementById("f-max").value = max;
  if (monthly === "1") document.getElementById("f-monthly").checked = true;
}

// Regroupe les provinces à faible volume (<5 annonces) dans la province
// voisine la plus proche de la même région, pour alléger le menu Ville.
const PROVINCE_MERGE_MAP = {
  // Sud
  "Songkhla": "Surat Thani",
  "Nakhon Sri Thammarat": "Surat Thani",
  "Yala": "Surat Thani",
  "Krabi": "Phuket",
  "Ranong": "Phuket",
  "Prachaubkirikhan": "Ratchburi",
  // Ouest / Centre
  "Petchburi": "Ratchburi",
  "Samut Songkram": "Samut Sakhon",
  "Suphanburi": "Nakhon Pathom",
  "Lopburi": "Phra Nakhon Sri Ayutthaya",
  "Saraburi": "Phra Nakhon Sri Ayutthaya",
  "Nakhon Nayok": "Pathumthani",
  // Est
  "Prachinburi": "Chachoengsao",
  "Chonburi": "Pattaya",
  // Nord
  "Phitsanulok": "Lamphang",
  "Phrae": "Lamphang",
  "Lamphun": "Chiang Mai",
  // Isaan (nord-est)
  "Udon Thani": "Khon Kaen",
  "Nong Bua Lam Phu": "Khon Kaen",
  "Maha Sarakham": "Khon Kaen",
  "Sakon Nakhon": "Khon Kaen",
  "Roi Et": "Khon Kaen",
  "Surin": "Nakhon Ratchasima",
  "Chaiyaphum": "Nakhon Ratchasima",
  "Buri Ram": "Nakhon Ratchasima",
  "Ubon Ratchathani": "Nakhon Ratchasima",
  "Yasothon": "Nakhon Ratchasima",
  "Amnat Charoen": "Nakhon Ratchasima",
};

function rawProvinceOf(l) {
  return norm(l.city || l.province || "");
}

function cityOf(l) {
  const district = norm(l.district || "");
  for (const [from, to] of Object.entries(DISTRICT_AS_CITY)) {
    if (key(district).includes(key(from))) return to;
  }
  // Utilise la vraie ville si elle existe ; sinon retombe sur la province.
  const raw = rawProvinceOf(l);
  return PROVINCE_MERGE_MAP[raw] || raw;
}

// Regroupe les villes/provinces canoniques (retournées par cityOf) en 5
// grandes régions de Thaïlande, pour le filtre régional (bouton flottant).
const REGION_MAP = {
  // Centre (Bangkok + périphérie + Ouest)
  "Bangkok": "Centre",
  "Pathumthani": "Centre",
  "Samut Prakarn": "Centre",
  "Nonthaburi": "Centre",
  "Nakhon Pathom": "Centre",
  "Samut Sakhon": "Centre",
  "Phra Nakhon Sri Ayutthaya": "Centre",
  "Ratchburi": "Centre",
  "Hua Hin": "Centre",
  // Nord
  "Chiang Mai": "Nord",
  "Lamphang": "Nord",
  "Chiang Rai": "Nord",
  // Nord-Est (Isaan)
  "Khon Kaen": "Nord-Est",
  "Nakhon Ratchasima": "Nord-Est",
  // Est
  "Pattaya": "Est",
  "Rayong": "Est",
  "Chachoengsao": "Est",
  "Chanthaburi": "Est",
  // Sud
  "Phuket": "Sud",
  "Surat Thani": "Sud",
  "Ko Samui": "Sud",
  "Ko Phangan": "Sud",
};

const REGION_BY_KEY = {};

function regionOf(l) {
  return REGION_BY_KEY[key(cityOf(l))] || "";
}

function districtOf(l) {
  return norm(l.district || l.subdistrict || l.neighborhood || l.quartier || "");
}

function key(v) {
  return norm(v).toLowerCase();
}

function buildLocationMenus() {
  const citySelect = document.getElementById("city-select");
  const districtSelect = document.getElementById("district-select");
  if (!citySelect || !districtSelect) return;

  const cities = new Map();
  const districts = new Map();

  allData.forEach(l => {
    if (l.status !== "active") return;
    const city = cityOf(l);
    if (!city) return;
    const ck = key(city);

    if (!cities.has(ck)) {
      cities.set(ck, {name: city, count: 0});
      districts.set(ck, new Map());
    }
    cities.get(ck).count++;

    const district = districtOf(l);
    if (district) {
      const dm = districts.get(ck);
      const dk = key(district);
      if (!dm.has(dk)) dm.set(dk, {name: district, count: 0});
      dm.get(dk).count++;
    }
  });

  const totalActive = [...cities.values()].reduce((sum, c) => sum + c.count, 0);

  citySelect.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = `Toutes les villes (${totalActive.toLocaleString("fr-FR")})`;
  citySelect.appendChild(all);

  [...cities.values()]
    .sort((a,b) => b.count - a.count)
    .forEach(c => {
      const o = document.createElement("option");
      o.value = c.name;
      o.textContent = `${c.name} (${c.count.toLocaleString("fr-FR")})`;
      citySelect.appendChild(o);
    });

  citySelect.value = activeCity;

  districtSelect.innerHTML = "";
  const allD = document.createElement("option");
  allD.value = "";
  allD.textContent = activeCity ? "Tous les quartiers" : "Sélectionnez d'abord une ville";
  districtSelect.appendChild(allD);

  const dm = districts.get(key(activeCity)) || new Map();
  [...dm.values()]
    .sort((a,b) => a.name.localeCompare(b.name, "fr", {sensitivity:"base"}))
    .forEach(d => {
      const o = document.createElement("option");
      o.value = d.name;
      o.textContent = `${d.name} (${d.count.toLocaleString("fr-FR")})`;
      districtSelect.appendChild(o);
    });

  districtSelect.disabled = !activeCity || dm.size === 0;
  districtSelect.value = activeDistrict;
}

function _updateNew24hCount() {
  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  const count = allData.filter(l => {
    if (l.status === "removed") return false;
    return new Date(l.first_seen_at || 0).getTime() >= cutoff;
  }).length;
  const el = document.getElementById("new24h-count");
  if (el) el.textContent = count ? `(${count.toLocaleString("fr-FR")})` : "";
  const hNew = document.getElementById("h-new");
  if (hNew) hNew.textContent = count.toLocaleString("fr-FR");
}

function toggleNew24h() {
  onlyNew24h = !onlyNew24h;
  document.getElementById("btn-new24h").classList.toggle("active", onlyNew24h);
  page = 1;
  applyFilters();
}

// Grandes villes mises en avant : correspond sur la province (ex: Bangkok,
// Phuket) ou le district (ex: Pattaya, qui est un district de Chonburi).
function _updateFeaturedCounts() {
  const counts = {};
  const options = document.querySelectorAll("#featured-city-select option[value]:not([value=''])");
  allData.forEach(l => {
    if (l.status === "removed") return;
    options.forEach(opt => {
      const name = opt.value;
      if (key(cityOf(l)).includes(key(name)) ||
          key(rawProvinceOf(l)).includes(key(name)) ||
          key(districtOf(l)).includes(key(name))) {
        counts[name] = (counts[name] || 0) + 1;
      }
    });
  });
  options.forEach(opt => {
    const count = counts[opt.value];
    opt.textContent = count ? `${opt.value} (${count.toLocaleString("fr-FR")})` : opt.value;
  });
}

function toggleFeaturedCity(name) {
  featuredCity = name;
  document.getElementById("featured-city-select").value = featuredCity;
  activeCity = "";
  activeDistrict = "";
  page = 1;
  buildLocationMenus();
  applyFilters();
}

function toggleRegionMenu() {
  const menu = document.getElementById("region-menu");
  const open = menu.classList.toggle("open");
  document.getElementById("region-fab").setAttribute("aria-expanded", open ? "true" : "false");
}

function selectRegion(region) {
  activeRegion = (activeRegion === region) ? "" : region;
  document.querySelectorAll(".region-item").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.region === activeRegion);
  });
  document.getElementById("region-fab").classList.toggle("active", !!activeRegion);
  document.getElementById("region-menu").classList.remove("open");
  document.getElementById("region-fab").setAttribute("aria-expanded", "false");
  page = 1;
  applyFilters();
}

function selectCity(value) {
  activeCity = norm(value);
  activeDistrict = "";
  featuredCity = "";
  document.getElementById("featured-city-select").value = "";
  page = 1;
  buildLocationMenus();
  applyFilters();
}

function selectDistrict(value) {
  activeDistrict = norm(value);
  page = 1;
  applyFilters();
}

function applyFilters() {
  const rawMin = document.getElementById("f-min").value;
  const rawMax = document.getElementById("f-max").value;
  // "0" est une valeur valide (≠ vide) : ne pas la faire retomber sur le
  // défaut via "|| ..." sinon "≤ 0 ฿" désactive silencieusement le filtre.
  const parsedMin = parseInt(rawMin);
  const parsedMax = parseInt(rawMax);
  const priceMin = rawMin === "" || isNaN(parsedMin) ? 0 : parsedMin;
  const priceMax = rawMax === "" || isNaN(parsedMax) ? Infinity : parsedMax;
  const monthlyOnly = document.getElementById("f-monthly").checked;
  const threeMonthOnly = document.getElementById("f-3month").checked;
  const sixMonthOnly = document.getElementById("f-6month").checked;
  const sort = document.getElementById("f-sort").value;

  const amenityFilters = [
    ["f-am-desk", "Desk"],
    ["f-am-wifi", "In-room WIFI"],
    ["f-am-smoking", "Smoking"],
    ["f-am-pool", "Pool"],
    ["f-am-fitness", "Fitness"],
    ["f-am-store", "Convenient Store"],
  ].filter(([id]) => document.getElementById(id).checked)
   .map(([, name]) => name);

  filtered = allData.filter(l => {
    if (l.status !== "active") return false;

    if (onlyNew24h) {
      const cutoff = Date.now() - 24 * 60 * 60 * 1000;
      if (new Date(l.first_seen_at || 0).getTime() < cutoff) return false;
    }

    if (activeCity && !key(cityOf(l)).includes(key(activeCity))) return false;
    if (activeDistrict && key(districtOf(l)) !== key(activeDistrict)) return false;

    if (featuredCity &&
        !key(cityOf(l)).includes(key(featuredCity)) &&
        !key(rawProvinceOf(l)).includes(key(featuredCity)) &&
        !key(districtOf(l)).includes(key(featuredCity))) return false;

    if (activeRegion && regionOf(l) !== activeRegion) return false;

    if (monthlyOnly && l.has_monthly_contract !== "true") return false;
    if (threeMonthOnly && contractNum(l.contract_3_month_raw) === null) return false;
    if (sixMonthOnly && contractNum(l.contract_6_month_raw) === null) return false;

    if (amenityFilters.length) {
      const amenities = l.amenities || [];
      if (!amenityFilters.every(a => amenities.includes(a))) return false;
    }

    if (monthlyOnly) {
      // Contrat 1 mois : on compare au prix réel de ce contrat (même valeur
      // que le tri "Prix"), pas à une plage qui chevaucherait juste la
      // fourchette demandée.
      const price = monthlyPrice(l);
      if (priceMin > 0 && price < priceMin) return false;
      if (priceMax < Infinity && price > priceMax) return false;
    } else {
      // On compare aux vrais prix des contrats disponibles (1/3/6 mois),
      // pas au champ "price_monthly_min/max" (prix d'en-tête de l'annonce
      // source) qui peut être vide même quand des contrats existent —
      // ça faisait passer le filtre max en no-op pour ces annonces.
      const contractPrices = [l.contract_monthly_raw, l.contract_3_month_raw, l.contract_6_month_raw]
        .map(contractNum)
        .filter(n => n !== null);
      const pMin = contractPrices.length ? Math.min(...contractPrices) : (l.price_monthly_min || 0);
      const pMax = contractPrices.length ? Math.max(...contractPrices) : (l.price_monthly_max || 0);
      if (priceMin > 0 && pMax > 0 && pMax < priceMin) return false;
      if (priceMax < Infinity && pMin > 0 && pMin > priceMax) return false;
    }

    return true;
  });

  filtered.sort((a,b) => {
    if (sort === "price_asc")
      return monthlyPrice(a) - monthlyPrice(b);
    if (sort === "price_desc")
      return monthlyPrice(b) - monthlyPrice(a);
    if (sort === "name")
      return (a.name || "").localeCompare(b.name || "");
    return new Date(b.source_updated_at || 0) -
           new Date(a.source_updated_at || 0);
  });

  document.getElementById("res-count").textContent =
    filtered.length.toLocaleString();

  renderPage();
  renderPager();
}

function resetFilters() {
  document.getElementById("f-min").value = "";
  document.getElementById("f-max").value = "";
  document.getElementById("f-slider").value = 1000;
  document.getElementById("slider-val").textContent = "Tous les prix";
  updateContractSectionVisibility();
  document.getElementById("f-monthly").checked = false;
  document.getElementById("f-3month").checked = false;
  document.getElementById("f-6month").checked = false;
  document.getElementById("f-am-desk").checked = false;
  document.getElementById("f-am-wifi").checked = false;
  document.getElementById("f-am-smoking").checked = false;
  document.getElementById("f-am-pool").checked = false;
  document.getElementById("f-am-fitness").checked = false;
  document.getElementById("f-am-store").checked = false;

  activeCity = "";
  activeDistrict = "";
  onlyNew24h = false;
  featuredCity = "";
  activeRegion = "";
  document.getElementById("btn-new24h").classList.remove("active");
  document.getElementById("featured-city-select").value = "";
  document.querySelectorAll(".region-item").forEach(btn => btn.classList.remove("active"));
  document.getElementById("region-fab").classList.remove("active");
  page = 1;

  buildLocationMenus();
  applyFilters();
}

// Slider (échelle exponentielle : plus de précision sur les loyers bas)
const SLIDER_RES = 1000;

const SLIDER_MAX_PRICE = 60000;

const SLIDER_STEP = 500;

// Exposant choisi pour que le milieu du curseur (50%) corresponde à 10 000 ฿
const SLIDER_EXP = Math.log(SLIDER_MAX_PRICE / 10000) / Math.log(2);

function sliderPosToPrice(pos) {
  const raw = SLIDER_MAX_PRICE * Math.pow(pos / SLIDER_RES, SLIDER_EXP);
  return Math.round(raw / SLIDER_STEP) * SLIDER_STEP;
}

// En dessous de 100 ฿, aucun contrat réel n'existe à ce prix : on masque
// le filtre "Contrat" plutôt que de laisser des cases à cocher inutiles.
function updateContractSectionVisibility() {
  const rawMax = document.getElementById("f-max").value;
  const max = parseInt(rawMax);
  const hide = rawMax !== "" && !isNaN(max) && max < 100;
  document.getElementById("contract-filter-section").style.display = hide ? "none" : "";
  if (hide) {
    document.getElementById("f-monthly").checked = false;
    document.getElementById("f-3month").checked = false;
    document.getElementById("f-6month").checked = false;
  }
}

// Debounce texte
let _t;

function renderPage() {
  const start = (page - 1) * PER_PAGE;
  const slice = filtered.slice(start, start + PER_PAGE);
  const wrap = document.getElementById("grid-wrap");

  if (!slice.length) {
    wrap.innerHTML = `<div class="state-box">Aucune annonce<br><small style="margin-top:6px;color:var(--muted)">Élargissez les filtres</small></div>`;
    return;
  }

  wrap.innerHTML = `<div class="grid">${slice.map(cardHTML).join("")}</div>`;
}

function monthlyPrice(l) {
  // Le tri utilise uniquement le prix du contrat 1 mois. On délègue à
  // contractNum() pour la même logique de nettoyage (fourchettes, prix
  // aberrants < MIN_VALID_PRICE) que l'affichage des cartes.
  const n = contractNum(l.contract_monthly_raw);
  return n === null ? Infinity : n;
}

function renderPager() {
  const total = Math.ceil(filtered.length / PER_PAGE);
  const el = document.getElementById("pager");
  if (total <= 1) { el.innerHTML = ""; return; }

  const pages = [];
  for (let i = Math.max(1, page-2); i <= Math.min(total, page+2); i++) pages.push(i);
  if (pages[0] > 1) pages.unshift(1);
  if (pages[pages.length-1] < total) pages.push(total);

  let html = `<button aria-label="Page précédente" ${page===1?"disabled":""} onclick="goPage(${page-1})">‹</button>`;
  let prev = null;
  pages.forEach(p => {
    if (prev !== null && p - prev > 1) html += `<button disabled aria-hidden="true">…</button>`;
    html += `<button class="${p===page?"on":""}" aria-label="Page ${p}" aria-current="${p===page?"page":"false"}" onclick="goPage(${p})">${p}</button>`;
    prev = p;
  });
  html += `<button aria-label="Page suivante" ${page===total?"disabled":""} onclick="goPage(${page+1})">›</button>`;
  el.innerHTML = html;
}

function goPage(p) {
  page = p;
  renderPage();
  renderPager();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showLoading() {
  document.getElementById("grid-wrap").innerHTML = `<div class="state-box"><div class="spinner"></div>Chargement…</div>`;
}
