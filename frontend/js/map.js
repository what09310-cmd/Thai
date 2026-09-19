// map.js -- Cartes Leaflet (vip.html, carte-*.html): etat, prix, filtres budget/duree.
// Partage par: vip, carte-thailande, carte-bangkok, carte-pattaya, carte-phuket.
// Script classique (pas de module): tout ce qui est declare ici est global.

let ads = [];

let leafletMap = null;

let clusterGroup = null;

let selectedBudget = null;

let selectedDuration = "";

let onlyNew = false;

// Bascule du bloc d'actions du popup vers deux boutons verrouilles (une
// recherche issue de /789 vers /resultats), au lieu de l'unique "Voir
// l'annonce complete" existant sur vip.html/carte-thailande.html. false par
// defaut: aucun changement pour les pages qui ne la definissent pas.
let SHOW_LOCK_BUTTONS = false;

const selectedAmenities = new Set();

const markersById = {};

const DURATION_FIELD = {
  "1 mois": "contract_monthly",
  "3 mois": "contract_3_month",
  "6 mois": "contract_6_month",
};

const EUR_TO_THB = 38.28;

function priceValue(ad){
  const n = ad.contract_monthly_min ?? ad.price_monthly_min;
  return Number.isFinite(n) && n > 0 ? n : null;
}

function priceLabel(ad){
  const n = priceValue(ad);
  return n ? Math.round(n / EUR_TO_THB).toLocaleString("fr-FR") + " €" : "";
}

function amenityChips(ad){
  const amenities = ad.amenities || [];
  // "Air Conditioner" est déjà affiché séparément (Oui/Non) dans le popup.
  return Object.keys(AMENITY_ICONS).filter(k => k !== "Air Conditioner" && amenities.includes(k));
}

function hasRaw(raw){
  return !!raw && String(raw).trim() !== "-" && String(raw).trim() !== "";
}

function parseRaw(raw){
  if (!hasRaw(raw)) return null;
  const m = String(raw).match(/[\d,\s]+/);
  if (!m) return null;
  const n = parseInt(m[0].replace(/[\s,]/g, ""), 10);
  return Number.isFinite(n) ? n : null;
}

// Même logique que priceOf() dans payant.html : le prix du contrat
// correspondant à la durée choisie, sinon le prix général de l'annonce.
function priceOf(ad, durationField){
  if (durationField) {
    const min = ad[`${durationField}_min`];
    if (min) return min;
    const n = parseRaw(ad[`${durationField}_raw`]);
    if (n !== null) return n;
  }
  if (ad.price_monthly_min) return ad.price_monthly_min;
  return parseRaw(ad.contract_monthly_raw);
}

const BUDGET_MIN = 10;

const BUDGET_MAX = 500;

function setupBudgetSlider(){
  const minInput = document.getElementById("budget-min");
  const maxInput = document.getElementById("budget-max");
  const minLabel = document.getElementById("budget-min-label");
  const maxLabel = document.getElementById("budget-max-label");
  const fill = document.getElementById("budget-fill");

  function render(){
    let min = Number(minInput.value);
    let max = Number(maxInput.value);
    if (min > max) {
      [min, max] = [max, min];
      minInput.value = min;
      maxInput.value = max;
    }

    const pctMin = ((min - BUDGET_MIN) / (BUDGET_MAX - BUDGET_MIN)) * 100;
    const pctMax = ((max - BUDGET_MIN) / (BUDGET_MAX - BUDGET_MIN)) * 100;
    fill.style.left = pctMin + "%";
    fill.style.width = (pctMax - pctMin) + "%";

    minLabel.textContent = min + " €";
    maxLabel.textContent = max >= BUDGET_MAX ? BUDGET_MAX + " €+" : max + " €";

    selectedBudget = (min === BUDGET_MIN && max === BUDGET_MAX)
      ? null
      : { min, max: max >= BUDGET_MAX ? Infinity : max };
    applyFilters();
  }

  minInput.addEventListener("input", render);
  maxInput.addEventListener("input", render);
  render();
}

function setupFormFilters(){
  setupBudgetSlider();

  document.querySelectorAll("#f-duration .chip[data-value]").forEach(chip => {
    chip.addEventListener("click", () => {
      const wasActive = chip.classList.contains("active");
      document.querySelectorAll("#f-duration .chip").forEach(c => c.classList.remove("active"));
      if (!wasActive) chip.classList.add("active");
      selectedDuration = wasActive ? "" : chip.dataset.value;
      applyFilters();
    });
  });

  document.getElementById("chip-new").addEventListener("click", () => {
    onlyNew = !onlyNew;
    document.getElementById("chip-new").classList.toggle("active", onlyNew);
    applyFilters();
  });

  document.querySelectorAll("#f-amenities .chip[data-value]").forEach(chip => {
    chip.addEventListener("click", () => {
      const value = chip.dataset.value;
      if (selectedAmenities.has(value)) {
        selectedAmenities.delete(value);
        chip.classList.remove("active");
      } else {
        selectedAmenities.add(value);
        chip.classList.add("active");
      }
      applyFilters();
    });
  });
}

let monthlyOnly = false;

let priceBoundsMax = 0;

let priceMin = 0;

let priceMax = 0;

function setupPriceFilter(){
  const prices = ads.map(priceValue).filter(Boolean);
  priceBoundsMax = prices.length ? Math.ceil(Math.max(...prices) / 1000) * 1000 : 0;
  priceMin = 0;
  priceMax = priceBoundsMax;

  const minInput = document.getElementById("priceMin");
  const maxInput = document.getElementById("priceMax");
  [minInput, maxInput].forEach(el => { el.max = priceBoundsMax; el.value = el === minInput ? 0 : priceBoundsMax; });

  updatePriceUI();

  minInput.addEventListener("input", () => {
    priceMin = Math.min(Number(minInput.value), priceMax);
    minInput.value = priceMin;
    updatePriceUI();
    applyFilters();
  });
  maxInput.addEventListener("input", () => {
    priceMax = Math.max(Number(maxInput.value), priceMin);
    maxInput.value = priceMax;
    updatePriceUI();
    applyFilters();
  });
}

function updatePriceUI(){
  document.getElementById("priceMinLabel").textContent = Math.round(priceMin / EUR_TO_THB).toLocaleString("fr-FR") + " €";
  document.getElementById("priceMaxLabel").textContent = Math.round(priceMax / EUR_TO_THB).toLocaleString("fr-FR") + " €";
  const fill = document.getElementById("rangeFill");
  if (priceBoundsMax > 0) {
    fill.style.left = (priceMin / priceBoundsMax * 100) + "%";
    fill.style.right = (100 - priceMax / priceBoundsMax * 100) + "%";
  }
}

// Sur mobile, le panneau de filtres (#controls) couvrait tout l'ecran et
// cachait la carte en dessous. Replie par defaut sous 640px (seule la
// premiere section -- resultats/recherche -- reste visible), le bouton
// #controls-toggle deplie le reste. Pages sans ce bouton (city-map.js): no-op.
function setupControlsToggle(){
  const toggle = document.getElementById("controls-toggle");
  const panel = document.getElementById("controls");
  if (!toggle || !panel) return;
  const isMobile = () => window.matchMedia("(max-width: 640px)").matches;
  function setCollapsed(collapsed){
    panel.classList.toggle("collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
  }
  setCollapsed(isMobile());
  window.addEventListener("resize", () => { if (!isMobile()) setCollapsed(false); });
  toggle.addEventListener("click", () => setCollapsed(!panel.classList.contains("collapsed")));
}
document.addEventListener("DOMContentLoaded", setupControlsToggle);
