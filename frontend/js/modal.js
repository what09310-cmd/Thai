// modal.js -- Fiche annonce: contrats, carte Google, galerie/lightbox, filigrane, focus.
// Partage par: index, premium, test, carte-thailande.
// Script classique (pas de module): tout ce qui est declare ici est global.

const LINE_ICON_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" style="flex-shrink:0"><rect width="24" height="24" rx="6" fill="#06C755"/><path fill="#fff" d="M12 5.5c-4.14 0-7.5 2.73-7.5 6.1 0 3.02 2.66 5.55 6.26 6.03.24.05.58.16.66.37.08.19.05.49.03.68l-.11.65c-.03.19-.15.75.65.41.8-.34 4.33-2.55 5.9-4.36 1.09-1.19 1.61-2.41 1.61-3.78 0-3.37-3.36-6.1-7.5-6.1Z"/></svg>`;

const WHATSAPP_ICON_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" style="flex-shrink:0"><rect width="24" height="24" rx="6" fill="#25D366"/><path fill="#fff" d="M12 5.3a6.7 6.7 0 0 0-5.72 10.18l-.78 2.85 2.92-.77A6.7 6.7 0 1 0 12 5.3Zm0 12.2a5.47 5.47 0 0 1-2.79-.76l-.2-.12-2.07.54.55-2.02-.13-.21a5.5 5.5 0 1 1 4.64 2.57Zm3.02-4.12c-.16-.08-.97-.48-1.12-.53-.15-.06-.26-.08-.37.08-.11.16-.42.53-.52.64-.1.11-.19.12-.36.04-.16-.08-.68-.25-1.3-.8-.48-.43-.8-.96-.9-1.12-.09-.16-.01-.25.07-.33.08-.07.16-.19.24-.28.08-.1.11-.16.16-.27.05-.11.03-.2-.01-.28-.05-.08-.37-.9-.51-1.23-.13-.32-.27-.28-.37-.28h-.31c-.11 0-.28.04-.42.2-.15.16-.55.54-.55 1.32s.57 1.53.65 1.64c.08.11 1.12 1.71 2.72 2.4.38.16.68.26.91.33.38.12.73.1 1-.06.31-.18.97-.4 1.1-.78.14-.39.14-.72.1-.79-.04-.07-.15-.11-.31-.19Z"/></svg>`;

// Certaines fiches RentHub ont un WhatsApp qui n'est en fait qu'un indicatif
// pays jamais complété par le loueur (ex: whatsapp="66", lien wa.me/66 casse
// sur la page d'origine) : moins de 5 chiffres ne peut pas etre un numero
// reel, donc pas la peine d'afficher la case Contact pour ca.
function hasRealWhatsapp(raw) {
  return !!raw && String(raw).replace(/\D/g, "").length >= 5;
}

// Affichage voulu: "(+66) 81 806 3131" — indicatif entre parenthèses, puis
// le numéro thaï découpé 2-3-4 chiffres (format international standard des
// mobiles thaïlandais). Les numéros hors format habituel (indicatif
// étranger, longueur atypique) sont juste regroupés par 3 en repli.
function formatWhatsapp(raw) {
  const digits = String(raw || "").replace(/\D/g, "");
  const cc = digits.slice(0, 2);
  const rest = digits.slice(2);
  const groups = rest.length === 9
    ? [rest.slice(0, 2), rest.slice(2, 5), rest.slice(5, 9)]
    : (rest.match(/.{1,3}/g) || []);
  return `(+${cc}) ${groups.join(" ")}`;
}

// Retourne le nombre brut (pour le calcul), ou null si absent/invalide.
// Certains prix sont une fourchette ("85 - 5,200 THB/month") où le bas est
// une aberration de scraping : en dessous de MIN_VALID_PRICE, on garde le
// haut de la fourchette ; si lui aussi est invalide, on abandonne le contrat.
const MIN_VALID_PRICE = 1000;

function contractNum(raw) {
  if (!raw || raw.trim() === "-" || raw.trim() === "") return null;
  const nums = (raw.match(/[\d,]+/g) || [])
    .map(s => parseInt(s.replace(/,/g, ""), 10))
    .filter(n => Number.isFinite(n) && n > 0);
  if (!nums.length) return null;

  const low = nums[0];
  const high = nums[nums.length - 1];
  if (low >= MIN_VALID_PRICE) return low;
  if (high >= MIN_VALID_PRICE) return high;
  return null;
}

function contractVal(raw) {
  const n = contractNum(raw);
  if (n === null) return "—";

  const eur = n / 38.28;

  const euros = eur.toLocaleString("fr-FR", {
    maximumFractionDigits: 0
  });

  return `${euros} €`;
}

let lastFocusedEl = null;

let currentGalleryImages = [];

let currentLightboxIndex = 0;

function openLightbox(i) {
  currentLightboxIndex = i;
  document.getElementById("lightbox-img").src = currentGalleryImages[i] || "";
  document.getElementById("lightbox-overlay").classList.add("open");
  const showNav = currentGalleryImages.length > 1;
  document.getElementById("lightbox-prev").disabled = !showNav;
  document.getElementById("lightbox-next").disabled = !showNav;
}

function closeLightbox(e) {
  if (e && e.target !== document.getElementById("lightbox-overlay")) return;
  document.getElementById("lightbox-overlay").classList.remove("open");
  document.getElementById("lightbox-img").src = "";
}

function navigateLightbox(delta) {
  if (!currentGalleryImages.length) return;
  currentLightboxIndex = (currentLightboxIndex + delta + currentGalleryImages.length) % currentGalleryImages.length;
  document.getElementById("lightbox-img").src = currentGalleryImages[currentLightboxIndex];
}

let lightboxTouchStartX = null;

function trapModalFocus(e) {
  if (e.key !== "Tab") return;
  const overlay = document.getElementById("overlay");
  if (!overlay.classList.contains("open")) return;
  const focusable = document.getElementById("modal-body")
    .querySelectorAll('a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])');
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
}

function mapQuery(l) {
  const pt = mapPoint(l);
  if (pt) return `${pt.lat},${pt.lon}`;
  const q = l.address || [l.subdistrict, l.district, l.province].filter(Boolean).join(", ");
  return q ? `${q}, Thailand` : null;
}

function googleMapsUrl(l) {
  const q = mapQuery(l);
  if (!q) return null;
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(q)}`;
}

// Embed public sans clé API (endpoint historique `output=embed`) — pas
// aussi précis/officiel que l'API Maps Embed payante, mais suffisant
// pour situer un point sur une carte interactive intégrée à la modal.
function googleMapsEmbedUrl(l) {
  const q = mapQuery(l);
  if (!q) return null;
  const pt = mapPoint(l);
  const zoom = pt && pt.precise ? 17 : (pt ? 15 : 13);
  return `https://maps.google.com/maps?q=${encodeURIComponent(q)}&z=${zoom}&output=embed`;
}

const humanizeRoomType = s => s ? s.toLowerCase().replace(/_/g, " ").replace(/^./, c => c.toUpperCase()) : null;

function roomTypeLabel(rt) {
  const roomType = humanizeRoomType(rt.room_type);
  return [
    rt.name && rt.name !== "Unknown" ? rt.name : null,
    roomType && roomType !== rt.name ? roomType : null,
    rt.size_sqm ? `${rt.size_sqm} m²` : null,
  ].filter(Boolean).join(" · ");
}

function contractRooms(l) {
  const rooms = (l.room_types || []).filter(rt =>
    rt.monthly_min_thb != null || rt.monthly_max_thb != null ||
    rt.contract_1_month_thb != null || rt.contract_3_month_thb != null || rt.contract_6_month_thb != null);
  const available = rooms.filter(rt => !rt.status || rt.status.trim().toLowerCase() === "available");
  return available.length ? available : rooms;
}

function contractsSectionTitle(l) {
  const useRooms = contractRooms(l);
  if (useRooms.length === 1) {
    const label = roomTypeLabel(useRooms[0]);
    if (label) return label;
  }
  return "Room Type";
}

function renderContractsHTML(l) {
  const fmt = n => n == null ? null : `${Math.round(n / EUR_TO_THB).toLocaleString("fr-FR")} €/mois`;
  const useRooms = contractRooms(l);

  if (useRooms.length) {
    return useRooms.map(rt => {
      const label = roomTypeLabel(rt);
      const terms = [
        ["1 mois", fmt(rt.contract_1_month_thb)],
        ["3 mois", fmt(rt.contract_3_month_thb)],
        ["6 mois", fmt(rt.contract_6_month_thb)],
      ].filter(([, v]) => v != null);
      if (!terms.length) return "";
      const termsHTML = terms.map(([term, v]) => `
        <div class="mc mc-price">
          <div class="mc-label">${term}</div>
          <div class="mc-val">${esc(v)}</div>
        </div>`).join("");
      if (useRooms.length <= 1) return termsHTML;
      return `
        <div class="mc-room-group">
          ${label ? `<div class="mc-room-label">${esc(label)}</div>` : ""}
          <div class="mc-room-terms">${termsHTML}</div>
        </div>`;
    }).join("");
  }

  const contracts = [["1 mois", l.contract_monthly_raw], ["3 mois", l.contract_3_month_raw], ["6 mois", l.contract_6_month_raw]]
    .filter(([, raw]) => raw && raw !== "-");
  if (!contracts.length) return `<div class="mc"><div class="mc-val na">Aucun contrat disponible</div></div>`;
  return contracts.map(([label, raw]) => `
        <div class="mc mc-price">
          <div class="mc-label">${label}</div>
          <div class="mc-val">${esc(raw)}</div>
        </div>`).join("");
}

// Verrouille le scroll de la page derriere la modale. iOS Safari ignore
// `overflow:hidden`/`overscroll-behavior:contain` sur body (le scroll de la
// page continue sous la modale, "scroll chaining"), donc on fige le body en
// `position:fixed` a la position de scroll actuelle -- seule technique
// fiable sur Safari mobile -- et on restaure la position a la fermeture.
let _scrollLockY = 0;
function lockBodyScroll() {
  _scrollLockY = window.scrollY || document.documentElement.scrollTop || 0;
  document.body.style.position = "fixed";
  document.body.style.top = `-${_scrollLockY}px`;
  document.body.style.width = "100%";
}
function unlockBodyScroll() {
  document.body.style.position = "";
  document.body.style.top = "";
  document.body.style.width = "";
  window.scrollTo(0, _scrollLockY);
}

function closeModal(e) {
  if (e && e.target !== document.getElementById("overlay")) return;
  document.getElementById("overlay").classList.remove("open");
  unlockBodyScroll();
  if (lastFocusedEl) { lastFocusedEl.focus(); lastFocusedEl = null; }
}

const WM_TILE_COUNT = 120;

function watermarkHTML(){
  const logo = `<span class="wm-thai">Thai</span><span class="wm-month">Month</span>`;
  return `<div class="wm-tile">${Array.from({ length: WM_TILE_COUNT }, () => `<span>${logo}</span>`).join("")}</div>` +
    `<div class="wm-badge">${logo}</div>`;
}
