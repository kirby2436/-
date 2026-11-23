(function () {
  "use strict";

  var form = document.getElementById("pool-form");
  if (!form) return;
  var all = form.querySelector("[data-select-all]");
  var picks = form.querySelectorAll("[data-pick]");

  function syncAll() {
    if (!all) return;
    var n = 0;
    picks.forEach(function (p) { if (p.checked) n += 1; });
    all.checked = picks.length > 0 && n === picks.length;
    all.indeterminate = n > 0 && n < picks.length;
  }

  if (all) {
    all.addEventListener("change", function () {
      picks.forEach(function (p) { p.checked = all.checked; });
      syncAll();
    });
  }
  picks.forEach(function (p) { p.addEventListener("change", syncAll); });

  // editing a quantity ticks the row
  form.querySelectorAll("[data-qty]").forEach(function (q) {
    q.addEventListener("input", function () {
      var row = q.closest("tr");
      var pick = row && row.querySelector("[data-pick]");
      if (pick && !pick.checked) { pick.checked = true; syncAll(); }
    });
  });

  // block an empty submit with a clear message (server also checks)
  form.addEventListener("submit", function (e) {
    var any = false;
    picks.forEach(function (p) { if (p.checked) any = true; });
    if (!any) {
      e.preventDefault();
      window.alert("담을 부재를 먼저 선택하세요.");
    }
  });
})();
