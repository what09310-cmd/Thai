---
paths:
  - "frontend/**"
---

## Frontend

Static pages served by the API (`/static`, `/`, `/test`, `/{page}.html`) and, for the public ones, by GitHub Pages pointing at the Render API (`RENDER_API` in `js/common.js`). Asset paths are absolute (`/static/js/...`, `/static/img/...`) for that reason.

Pages: `index.html` (catalogue, any account), `premium.html` (public upsell: same catalogue, watermarked photos, "Voir l'annonce complète" CTA, no lightbox/contacts), `test.html` (public landing page, the "vitrine"), `payant.html` (offer/pricing), `vip.html` (premium map), `carte-thailande.html` (public map), `carte-bangkok/pattaya/phuket.html` (city maps: a `CITY` object + `js/city-map.js`), `789.html` (qualification tunnel: location/budget/duration, 3 steps, no reload) → `resultats.html` (its results: same Leaflet system as `carte-thailande.html`, pre-filtered server-side by city via `LOCATION_QUERY`, criteria read from `sessionStorage` since there's no server-side lookup of the last `UserSearchPreference` row).

Shared code — one copy, loaded as classic scripts (everything declared is global), in this order:

- `js/common.js` — `API`/`RENDER_API`, `esc`, `escapeHtml`, `AMENITY_ICONS`, the Render wake-up banner (all pages)
- `js/geo.js` — district/address coordinate fallbacks, `allData` (index, premium, test)
- `js/modal.js` — listing sheet, contract helpers, Google Maps links, lightbox, watermark (index, premium, test, carte-thailande, resultats)
- `js/catalog.js` — loading (`fetchAllListings`: `status=active`, pages after the first in parallel), filters, region menus, pager (index, premium)
- `js/map.js` — Leaflet state (`ads`, `markersById`, `selectedBudget`/`selectedDuration`/`selectedAmenities`, `SHOW_LOCK_BUTTONS`), prices, budget/duration filters (vip, carte-thailande, resultats, city maps); on top of it `js/thai-map.js` (vip, carte-thailande, resultats: pins, loading via `loadAds()` — reads an optional page-declared `LOCATION_QUERY` string to pre-filter `/listings` server-side, same principle as `CITY.query` below —, `applyFilters`, and the rich popup/modal: `popupHTML`, `popupActionsHTML`, `modalHTML`, `openModal`, `mapPoint`, `buildMap`) or `js/city-map.js` (the three city maps, driven by their `CITY` object, simpler popup)
- `css/common.css`, `css/modal.css`, `css/catalog.css`, `css/map.css`, `css/city-map.css` — same families

What stays inline in a page is what differs between pages (`cardHTML`, `init`, page-specific CSS). `carte-thailande.html`/`resultats.html` are the exception to "modalHTML/openModal/mapPoint/buildMap stay inline": both want the exact same rich popup and modal, so those functions were extracted into `js/thai-map.js` instead of duplicated — extract shared page logic like this only when two pages genuinely want identical behaviour, not as a default. `vip.html` loads `thai-map.js` too but keeps its own inline `buildMap` (simpler inline popup, no modal): a later `function buildMap(){}` in the page's own `<script>` silently overrides the shared one (classic scripts, not modules — this only works for `function` declarations, `let`/`const` would throw "already been declared"). The keyboard (`Escape`/arrows, `trapModalFocus`) and touch (lightbox swipe) listeners stay inline per page on purpose even though they're identical on `carte-thailande.html`/`resultats.html`/`index.html`/`premium.html`/`test.html`: they assume `#overlay`/`#lightbox-overlay` exist in the page's DOM, which `vip.html` (loads `thai-map.js` too) doesn't have — registering them in the shared file would throw on every keydown on `vip.html`. `index.html` and `premium.html` are deliberately two files: they sit behind different access rules (`PROTECTED_PATHS`) and both must exist as static files for GitHub Pages.

Price rules differ by design and are not unified: `monthlyPrice` (catalogue sort, 1-month contract via `contractNum`), `wallPrice` (landing wall: 1 → 3 → 6 months with a label), `priceValue` (city maps: structured `_min` fields), `priceOf` (vip/carte-thailande: duration filter). Unifying them is a product decision.

Verification used for the 2026-09 refactor and worth repeating after a frontend change: load every page in headless Chromium (Playwright is in `requirements-tools.txt`), assert zero console errors, compare screenshots, exercise modal/lightbox/pagination/map filters.

## Reading these files without burning context

These pages (`index.html`, `payant.html`, `premium.html`, `test.html`, `carte-thailande.html`, `vip.html`, `789.html`, `resultats.html`) are 150-1900 lines each — never `Read` one in full by default:

1. `Grep` first for the id/class/function/visible text you're after to get a line number.
2. `Read` with `offset`/`limit` around that line (~30 lines of margin), not the whole file.
3. Don't re-read a file already read this session unless it changed since (edited by another tool, or enough time/actions passed that it might have).
4. Logic shared across pages (modal, map, filters, price) lives in `frontend/js/*.js` / `frontend/css/*.css` (see above) — check there before searching every HTML page for it.
5. Don't spawn an Explore/general-purpose subagent for a simple symbol/section lookup in this folder — a direct `Grep`/`Glob` is cheaper and faster.
