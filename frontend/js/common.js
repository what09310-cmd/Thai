// common.js -- Base de toutes les pages: adresse de l'API, echappement HTML, icones d'equipements.
// Partage par: index, premium, test, payant, vip, carte-thailande, carte-bangkok, carte-pattaya, carte-phuket.
// Script classique (pas de module): tout ce qui est declare ici est global.

const RENDER_API = "https://thai-month.onrender.com";

const API = window.location.origin.startsWith("file:") ? "http://localhost:8000"
          : window.location.hostname === "what09310-cmd.github.io" ? RENDER_API
          : window.location.origin;

// Icônes pour les 28 catégories d'équipements — voir src/normalizers/amenities.py.
const AMENITY_ICONS = {
  "Air Conditioner": "❄️ Air Conditioner",
  "Furnished": "🛋️ Furnished",
  "Water Heater": "🚿 Water Heater",
  "Fan": "🌀 Fan",
  "Television": "📺 Television",
  "Refrigerator": "🧊 Refrigerator",
  "Sofa": "🪑 Sofa",
  "Desk": "🖥️ Desk",
  "Kitchen Stove": "🍳 Kitchen Stove",
  "Phone": "☎️ Phone",
  "In-room WIFI": "📶 WIFI",
  "Cable TV": "📺 TV",
  "Pets": "🐾 Pets",
  "Smoking": "🚬 Smoking",
  "Parking": "🅿️ Parking",
  "Bicycle Parking": "🚲 Bicycle Parking",
  "Lift": "🛗 Lift",
  "Pool": "🏊 Pool",
  "Fitness": "🏋️ Fitness",
  "Security keycard": "🔑 Security keycard",
  "Security finger print": "👆 Security finger print",
  "CCTV": "📹 CCTV",
  "Security": "🛡️ Security",
  "Restaurant/Food Shop": "🍽️ Restaurant/Food Shop",
  "Convenient Store": "🏪 Store",
  "Laundry": "🧺 Laundry",
  "Beauty Salon in Building": "💇 Beauty Salon in Building",
  "EV Charger": "🔌 EV Charger",
};

function esc(s) {
  return (s || "").toString().replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

function text(v){ return v == null ? "" : String(v); }

function escapeHtml(s){
  return text(s).replace(/[&<>"']/g,c=>({
    "&":"&amp;","<":"&lt;",">":"&gt;",
    '"':"&quot;","'":"&#039;"
  }[c]));
}

// Render (plan gratuit) endort l'API après ~15 min sans trafic : le premier
// visiteur attend 30 à 60 s de redémarrage, pendant lesquelles les fetch vers
// l'API pendent, échouent (erreur réseau) ou reçoivent un 502/503. Plutôt
// qu'une page vide qui a l'air cassée, on affiche un bandeau dès qu'une
// requête traîne, et en cas d'échec on attend que /health réponde avant de
// rejouer la requête. Les URLs hors API (fichiers locaux, tuiles) ne sont
// pas concernées.
(() => {
  const nativeFetch = window.fetch.bind(window);
  const SLOW_AFTER_MS = 4000;     // au-delà, on prévient le visiteur
  const HEALTH_EVERY_MS = 3000;
  const MAX_WAIT_MS = 120000;     // au-delà, on laisse l'erreur remonter
  let banner = null;
  let slowRequests = 0;
  let waking = null;

  function refreshBanner() {
    const visible = slowRequests > 0 || waking !== null;
    if (visible && !banner) {
      banner = document.createElement("div");
      banner.id = "api-wake-banner";
      banner.setAttribute("role", "status");
      banner.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;padding:10px 16px;" +
        "background:#1f2937;color:#fff;font:14px/1.4 system-ui,sans-serif;text-align:center;" +
        "box-shadow:0 2px 8px rgba(0,0,0,.3)";
      banner.textContent = "Chargement des annonces… Première visite : le serveur se réveille, compte environ 30 secondes.";
      (document.body || document.documentElement).appendChild(banner);
    } else if (!visible && banner) {
      banner.remove();
      banner = null;
    }
  }

  function looksAsleep(response, error) {
    return error !== null || response.status === 502 || response.status === 503 || response.status === 504;
  }

  // Une seule attente partagée, quel que soit le nombre de requêtes en échec.
  function waitForApi() {
    if (waking) return waking;
    waking = (async () => {
      const deadline = Date.now() + MAX_WAIT_MS;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, HEALTH_EVERY_MS));
        try {
          const h = await nativeFetch(`${API}/health`, { cache: "no-store" });
          if (h.ok) return true;
        } catch (e) {}
      }
      return false;
    })().finally(() => { waking = null; refreshBanner(); });
    refreshBanner();
    return waking;
  }

  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if (!url.startsWith(`${API}/`)) return nativeFetch(input, init);

    for (let attempt = 0; ; attempt++) {
      let slow = false;
      const timer = setTimeout(() => { slow = true; slowRequests++; refreshBanner(); }, SLOW_AFTER_MS);
      let response = null, error = null;
      try {
        response = await nativeFetch(input, init);
      } catch (e) {
        error = e;
      } finally {
        clearTimeout(timer);
        if (slow) { slowRequests--; refreshBanner(); }
      }

      if (!looksAsleep(response, error) || attempt > 0) {
        if (error) throw error;
        return response;
      }
      if (!(await waitForApi())) {
        if (error) throw error;
        return response;
      }
      // /health répond : on rejoue la requête une fois.
    }
  };
})();
