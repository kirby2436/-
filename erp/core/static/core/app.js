/* Sidebar helpers. The page works without this file: the sidebar stays open on wide screens and the
   hamburger is a plain checkbox on narrow ones. This only adds: remembered collapse state, Esc to close. */
(function () {
  var KEY = "erp.sidebar.collapsed";
  var root = document.documentElement;
  function read() { try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; } }
  function write(v) { try { localStorage.setItem(KEY, v ? "1" : "0"); } catch (e) { /* private mode */ } }
  if (read()) { root.classList.add("sb-collapsed"); }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("sb-collapse");
    var toggle = document.getElementById("nav-toggle");
    function sync() {
      if (!btn) { return; }
      var c = root.classList.contains("sb-collapsed");
      btn.setAttribute("aria-expanded", c ? "false" : "true");
      btn.setAttribute("aria-label", c ? "사이드바 펼치기" : "사이드바 접기");
    }
    if (btn) {
      btn.hidden = false;
      btn.addEventListener("click", function () {
        root.classList.toggle("sb-collapsed");
        write(root.classList.contains("sb-collapsed"));
        sync();
      });
      sync();
    }
    if (toggle) {
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && toggle.checked) { toggle.checked = false; toggle.focus(); }
      });
      var links = document.querySelectorAll(".sidebar a");
      for (var i = 0; i < links.length; i++) {
        links[i].addEventListener("click", function () { toggle.checked = false; });
      }
    }
  });
})();
