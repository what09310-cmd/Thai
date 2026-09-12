/* Bloc compte du header (index.html, vip.html, premium.html).
 *
 * Interroge /me et rend, dans <div class="auth-box">, l'un des trois etats:
 *   anonyme      -> "Se connecter"
 *   compte libre -> email · "Passer premium" · "Deconnexion"
 *   premium/admin-> email · badge "Premium" · "Deconnexion"
 * Tout est same-origin (connect-src 'self'), le cookie de session part avec
 * fetch grace a credentials: "same-origin". Une erreur reseau laisse le
 * bloc vide plutot que d'afficher un faux "Se connecter".
 */
(function () {
  var API = window.location.origin.startsWith("file:") ? "http://localhost:8000" : window.location.origin;

  var style = document.createElement("style");
  style.textContent = [
    ".auth-box{display:flex;align-items:center;gap:10px;margin-left:auto;margin-right:12px;font-size:13px;color:var(--muted,#6b7280);white-space:nowrap}",
    ".auth-box .auth-email{max-width:180px;overflow:hidden;text-overflow:ellipsis;color:var(--text,#111)}",
    ".auth-box a,.auth-box button{font:inherit;font-weight:600;color:var(--accent,#0f766e);background:none;border:0;padding:0;cursor:pointer;text-decoration:none}",
    ".auth-box a:hover,.auth-box button:hover{text-decoration:underline}",
    ".auth-box .auth-badge{background:var(--accent,#0f766e);color:#fff;border-radius:999px;padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:.02em}",
    ".auth-box .auth-sep{opacity:.4}",
    "@media (max-width:640px){.auth-box .auth-email{display:none}.auth-box{gap:8px;margin-right:8px;font-size:12px}}",
  ].join("");
  document.head.appendChild(style);

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function logoutForm() {
    var f = document.createElement("form");
    f.method = "post";
    f.action = "/logout";
    f.style.display = "inline";
    var b = el("button", null, "Déconnexion");
    b.type = "submit";
    f.appendChild(b);
    return f;
  }

  function render(box, me) {
    box.textContent = "";
    if (!me.authenticated) {
      var a = el("a", null, "Se connecter");
      a.href = "/login";
      box.appendChild(a);
      return;
    }
    var email = el("span", "auth-email", me.email || "");
    email.title = me.email || "";
    box.appendChild(email);
    box.appendChild(el("span", "auth-sep", "·"));
    if (me.premium) {
      box.appendChild(el("span", "auth-badge", me.admin ? "Admin" : "Premium"));
    } else {
      var up = el("a", null, "Passer premium");
      up.href = "/premium.html";
      box.appendChild(up);
    }
    box.appendChild(el("span", "auth-sep", "·"));
    box.appendChild(logoutForm());
  }

  function init() {
    var header = document.querySelector("header");
    if (!header) return;
    var box = el("div", "auth-box");
    // Avant le bouton d'action (Voir la carte / Voir la liste), apres le logo.
    var cta = header.querySelector(".header-map, .header-list");
    header.insertBefore(box, cta || null);

    fetch(API + "/me", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (me) { if (me) render(box, me); })
      .catch(function () { /* reseau HS: on laisse le bloc vide */ });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
