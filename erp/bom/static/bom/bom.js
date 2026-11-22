(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); }
  });
  var radios = document.querySelectorAll("input[name='mode']");
  var box = document.getElementById("replace-confirm");
  function sync() {
    if (!box) { return; }
    var replace = document.querySelector("input[name='mode'][value='replace']");
    box.hidden = !(replace && replace.checked);
  }
  for (var i = 0; i < radios.length; i++) { radios[i].addEventListener("change", sync); }
  sync();
})();
