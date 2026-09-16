// geo.js -- Points GPS de repli (quartier / adresse) et catalogue charge en memoire.
// Partage par: index, premium, test.
// Script classique (pas de module): tout ce qui est declare ici est global.

let allData = [];

let districtCoords = {};

let addressCoords = {};

// Points GPS de repli par quartier (subdistrict/district/province),
// pré-calculés hors-ligne via Nominatim — voir scripts/geocode_districts.py.
// Sert de dernier recours quand ni l'annonce ni son adresse complète n'ont
// de coordonnées, pour que le lien/carte Google Maps pointe toujours sur
// un vrai marqueur plutôt qu'une recherche texte approximative.
async function loadDistrictCoords() {
  try {
    const r = await fetch(`${API}/static/district_coords.json`);
    if (r.ok) districtCoords = await r.json();
  } catch (e) {}
}

// Points GPS par adresse complète (rue/route + quartier, quand RentHub la
// publie) — voir scripts/geocode_addresses.py. Plus précis que le centre
// de quartier: tombe près du bâtiment réel pour les annonces qui ont un
// nom de rue, sinon équivalent au fallback quartier.
async function loadAddressCoords() {
  // Fichier optionnel (scripts/geocode_addresses.py): absent, on retombe
  // sur le centre de quartier sans autre requete.
  try {
    const r = await fetch(`${API}/static/address_coords.json`);
    if (r.ok) addressCoords = await r.json();
  } catch (e) {}
}

function norm(v) {
  return String(v ?? "").trim();
}

// Districts promus au rang de "ville" dans le menu, avec un nom plus
// parlant (ex: le district administratif "Bang Lamung" correspond à la
// zone touristique connue sous le nom de Pattaya).
const DISTRICT_AS_CITY = {
  "Bang Lamung": "Pattaya",
  "Ko Samui": "Ko Samui",
  "Hua Hin": "Hua Hin",
  "Ko Phangan": "Ko Phangan",
};

// Point GPS de l'adresse complète (rue/route + quartier) de l'annonce,
// si présent dans le cache chargé par loadAddressCoords(). Plus précis que
// districtPoint() quand RentHub publie un nom de rue.
function addressPoint(l) {
  if (!l.address) return null;
  return addressCoords[norm(l.address).toLowerCase()] || null;
}

// Point GPS pré-calculé du quartier (subdistrict/district/province) de
// l'annonce, si présent dans le cache chargé par loadDistrictCoords().
function districtPoint(l) {
  const k = [l.subdistrict, l.district, l.province].map(v => norm(v).toLowerCase()).join("|");
  return districtCoords[k] || null;
}
