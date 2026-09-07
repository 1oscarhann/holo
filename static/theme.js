/* Theme + typeface, chosen by the person using the app.
 *
 * Kept in localStorage rather than on the user row: it is a per-device display
 * preference, it must resolve before the first paint (a round trip cannot), and
 * it needs no migration. The cost is that it does not follow you to a second
 * device, which is the right trade for a setting you change once.
 *
 * This file is loaded synchronously in <head>, before any stylesheet paints, so
 * the attributes are on <html> for the first frame and there is no flash of the
 * default theme. It also injects the one font family that is actually needed —
 * loading all eight would cost more than the feature is worth.
 */
(function () {
  var FACES = {
    familjen:   "Familjen+Grotesk:wght@400;500;600;700",
    archivo:    "Archivo:wght@400;500;600;700",
    schibsted:  "Schibsted+Grotesk:wght@400;500;600;700",
    bricolage:  "Bricolage+Grotesque:opsz,wght@12..96,400;12..96,500;12..96,600;12..96,700",
    chivo:      "Chivo:wght@400;500;600;700",
    intertight: "Inter+Tight:wght@400;500;600;700",
    instrument: "Instrument+Sans:wght@400;500;600;700",
    fraunces:   "Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700"
  };
  var THEMES = ["ultramarine","steel","acid","ember","gold","jade","mono","vault","gallery"];

  function read(k, fallback, allowed) {
    var v;
    try { v = localStorage.getItem(k); } catch (e) { v = null; }   // private mode throws
    return (v && allowed.indexOf(v) !== -1) ? v : fallback;
  }

  var theme = read("holo.theme", "ember", THEMES);
  var face  = read("holo.font", "bricolage", Object.keys(FACES));

  var root = document.documentElement;
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-font", face);

  var link = document.createElement("link");
  link.rel = "stylesheet";
  link.id = "holo-face";
  link.href = "https://fonts.googleapis.com/css2?family=" + FACES[face] + "&display=swap";
  document.head.appendChild(link);

  window.holoTheme = {
    themes: THEMES,
    faces: FACES,
    get: function () { return { theme: theme, face: face }; },
    set: function (kind, value) {
      if (kind === "theme") {
        if (THEMES.indexOf(value) === -1) return;
        theme = value; root.setAttribute("data-theme", value);
      } else {
        if (!FACES[value]) return;
        face = value; root.setAttribute("data-font", value);
        document.getElementById("holo-face").href =
          "https://fonts.googleapis.com/css2?family=" + FACES[value] + "&display=swap";
      }
      try { localStorage.setItem("holo." + (kind === "theme" ? "theme" : "font"), value); }
      catch (e) { /* private mode: the choice still applies for this session */ }
    }
  };
})();
