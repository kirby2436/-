(function () {
  "use strict";

  // ---- formset: add / remove rows, mark deletions
  document.addEventListener("click", function (e) {
    var add = e.target.closest("[data-add-row]");
    if (add) {
      var tpl = document.getElementById(add.dataset.template);
      var box = document.getElementById(add.dataset.container);
      var total = document.getElementById("id_" + add.dataset.prefix + "-TOTAL_FORMS");
      var n = parseInt(total.value, 10);
      var holder = document.createElement("div");
      holder.innerHTML = tpl.innerHTML.replace(/__prefix__/g, n);
      var row = holder.firstElementChild;
      box.appendChild(row);
      total.value = n + 1;
      var first = row.querySelector("input:not([type=hidden]), select");
      if (first) first.focus();
      return;
    }
    var rm = e.target.closest("[data-remove-row]");
    if (rm) {
      var r = rm.closest("[data-row]");
      if (r) r.remove();
    }
  });
  document.addEventListener("change", function (e) {
    if (e.target.matches(".del-check input")) {
      e.target.closest("[data-row]").classList.toggle("marked", e.target.checked);
    }
  });
  document.querySelectorAll(".del-check input:checked").forEach(function (c) {
    c.closest("[data-row]").classList.add("marked");
  });

  // ---- drag and drop file input
  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var box = zone.querySelector(".dropzone");
    var label = zone.querySelector("[data-drop-name]");
    function show() { label.textContent = input.files.length ? "선택한 파일: " + input.files[0].name : ""; }
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach(function (t) {
      box.addEventListener(t, function (ev) { ev.preventDefault(); box.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (t) {
      box.addEventListener(t, function (ev) { ev.preventDefault(); box.classList.remove("over"); });
    });
    box.addEventListener("drop", function (ev) {
      if (ev.dataTransfer && ev.dataTransfer.files.length) {
        input.files = ev.dataTransfer.files;
        show();
      }
    });
  });

  // ---- quick add dialog
  var dialog = document.getElementById("quick-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return;
  var form = document.getElementById("quick-form");
  var nameInput = document.getElementById("quick-name");
  var errBox = document.getElementById("quick-error");
  var status = document.getElementById("quick-status");
  var saveBtn = document.getElementById("quick-save");
  var LABELS = {
    customer: ["매출처", "partner"], contractor: ["원청", "partner"],
    fabricator: ["외주제작처", "partner"], floor: ["층", "floor"]
  };
  var current = null;
  var lastFocused = {};

  document.addEventListener("focusin", function (e) {
    var k = e.target.dataset && e.target.dataset.quick;
    if (k) lastFocused[k] = e.target;
  });

  function setError(msg) { errBox.hidden = !msg; errBox.textContent = msg || ""; }

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-quick-open]");
    if (!b) return;
    var kind = b.dataset.quickOpen;
    var target = b.dataset.target ? document.getElementById(b.dataset.target) : null;
    current = { kind: kind, target: target };
    document.getElementById("quick-title").textContent = LABELS[kind][0] + " 새로 추가";
    document.getElementById("quick-label").textContent = LABELS[kind][0] + " 이름";
    nameInput.maxLength = LABELS[kind][1] === "floor" ? 30 : 100;
    nameInput.value = "";
    setError("");
    dialog.showModal();
    nameInput.focus();
  });
  document.getElementById("quick-cancel").addEventListener("click", function () { dialog.close(); });

  function addOption(sel, id, name) {
    var exists = Array.prototype.some.call(sel.options, function (o) { return o.value === String(id); });
    if (!exists) sel.appendChild(new Option(name, id));
  }
  function applyResult(data) {
    var kind = current.kind;
    var sel = 'select[data-quick="' + kind + '"]';
    document.querySelectorAll(sel).forEach(function (s) { addOption(s, data.id, data.name); });
    document.querySelectorAll("template").forEach(function (t) {
      t.content.querySelectorAll(sel).forEach(function (s) { addOption(s, data.id, data.name); });
    });
    var pick = current.target || lastFocused[kind];
    if (pick) { pick.value = String(data.id); pick.focus(); }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var name = nameInput.value.trim();
    if (!name) { setError("이름을 입력하세요."); nameInput.focus(); return; }
    var kind = current.kind;
    var url = dialog.dataset[LABELS[kind][1] + "Url"];
    var fd = new FormData();
    fd.append("name", name);
    if (LABELS[kind][1] === "partner") fd.append("role", kind);
    fd.append("csrfmiddlewaretoken", form.querySelector("[name=csrfmiddlewaretoken]").value);
    saveBtn.disabled = true;
    fetch(url, { method: "POST", body: fd, credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: "서버 응답을 처리하지 못했습니다." }; }); })
      .then(function (data) {
        if (data.ok) {
          applyResult(data);
          status.textContent = data.message || (data.name + " 항목을 추가하고 선택했습니다.");
          dialog.close();
        } else {
          setError(data.error || "추가하지 못했습니다.");
        }
      })
      .catch(function () { setError("네트워크 오류로 추가하지 못했습니다. 다시 시도하세요."); })
      .then(function () { saveBtn.disabled = false; });
  });
})();
