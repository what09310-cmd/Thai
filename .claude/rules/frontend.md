---
paths:
  - "frontend/**"
---

## Frontend

Static pages served by the API (`/static`, `/`, `/test`, `/{page}.html`) and, for the public ones, by GitHub Pages pointing at the Render API (`RENDER_API` in `js/common.js`). Asset paths are absolute (`/static/js/...`, `/static/img/...`) for that reason.

Pages: `index.html` (catalogue, any account), `premium.html` (public upsell: same catalogue, watermarked photos, "Voir l'annonce complète" CTA, no lightbox/contacts), `test.html` (public landing page, the "vitrine"), `payant.html` (offer/pricing), `vip.html` (premium map), `carte-thailande.html` (public map), `carte-bangkok/pattaya/phuket.html` (city maps: a `CITY` object + `js/city-map.js`).

Shared code — one copy, loaded as classic scripts (everything declared is global), in this order:

- `js/common.js` — `API`/`RENDER_API`, `esc`, `escapeHtml`, `AMENITY_ICONS`, the Render wake-up banner (all pages)
- `js/geo.js` — district/address coordinate fallbacks, `allData` (index, premium, test)
- `js/modal.js` — listing sheet, contract helpers, Google Maps links, lightbox, watermark (index, premium, test, carte-thailande)
- `js/catalog.js` — loading (`fetchAllListings`: `status=active`, pages after the first in parallel), filters, region menus, pager (index, premium)
- `js/map.js` — Leaflet state, prices, budget/duration filters (vip, carte-thailande, city maps); on top of it `js/thai-map.js` (vip, carte-thailande: pins, loading, filters) or `js/city-map.js` (the three city maps, driven by their `CITY` object)
- `css/common.css`, `css/modal.css`, `css/catalog.css`, `css/map.css`, `css/city-map.css` — same families

What stays inline in a page is what differs between pages (`cardHTML`, `modalHTML`, `openModal`, `mapPoint`, `init`, page-specific CSS). To change a shared behaviour, edit the module; to change one page, edit the page. A function moved to a module must not be redeclared in a page (`let`/`const` would throw "already been declared"). `index.html` and `premium.html` are deliberately two files: they sit behind different access rules (`PROTECTED_PATHS`) and both must exist as static files for GitHub Pages.

Price rules differ by design and are not unified: `monthlyPrice` (catalogue sort, 1-month contract via `contractNum`), `wallPrice` (landing wall: 1 → 3 → 6 months with a label), `priceValue` (city maps: structured `_min` fields), `priceOf` (vip/carte-thailande: duration filter). Unifying them is a product decision.

Verification used for the 2026-09 refactor and worth repeating after a frontend change: load every page in headless Chromium (Playwright is in `requirements-tools.txt`), assert zero console errors, compare screenshots, exercise modal/lightbox/pagination/map filters.
