/* Recovery Guard 3.0 — логика консоли администратора (guard.md §7).
   Vanilla JS, same-origin fetch с cookie-авторизацией админа. */
(function () {
  "use strict";
  var API = "/api/admin/recovery";
  var SEC = "/api/admin/security";
  var current = "overview";
  var autoTimer = null;

  function $(sel) { return document.querySelector(sel); }
  function panel(name) {
    return document.querySelector('.rg-panel[data-panel="' + name + '"]');
  }
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function toast(msg, bad) {
    var t = $("#rg-toast");
    t.textContent = msg;
    t.style.display = "block";
    t.style.borderColor = bad ? "var(--bad)" : "var(--line)";
    setTimeout(function () { t.style.display = "none"; }, 4000);
  }
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (r.status === 403) { throw new Error("нужны права администратора"); }
      return r.json();
    });
  }
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); });
  }

  function pill(ok, txtOk, txtBad) {
    return '<span class="rg-pill ' + (ok ? "rg-good" : "rg-bad") + '">' +
      esc(ok ? (txtOk || "ok") : (txtBad || "нет")) + "</span>";
  }

  // ------------------------------------------------------------- tabs -----
  function showTab(name) {
    current = name;
    document.querySelectorAll(".rg-tab").forEach(function (b) {
      b.classList.toggle("active", b.dataset.tab === name);
    });
    document.querySelectorAll(".rg-panel").forEach(function (p) {
      p.classList.toggle("rg-hide", p.dataset.panel !== name);
    });
    load(name);
  }

  function load(name) {
    var fn = {
      overview: loadOverview,
      diagnose: loadDiagnose,
      snapshots: loadSnapshots,
      restore: loadRestore,
      settings: loadSettings,
      security: loadSecurity,
      audit: loadAudit,
      load: loadDaemon,
    }[name];
    if (fn) { fn().catch(function (e) { toast("Ошибка: " + e.message, true); }); }
  }

  // ----------------------------------------------------------- overview ---
  function stat(k, v) {
    return '<div class="rg-stat"><div class="k">' + esc(k) +
      '</div><div class="v">' + v + "</div></div>";
  }
  function loadOverview() {
    return Promise.all([
      getJSON(API + "/status"),
      getJSON(API + "/recovery-mode").catch(function () { return {}; }),
      getJSON(API + "/slots").catch(function () { return {}; }),
    ]).then(function (r) {
      var s = r[0], mode = r[1], slots = r[2];
      var st = s.stats || {};
      var host = panel("overview");
      if (!host) return;
      host.innerHTML = "";
      var grid = el("div", "rg-grid");
      grid.innerHTML =
        stat("Снимков", st.count || 0) +
        stat("Годных", st.good || 0) +
        stat("Закреплено", st.pinned || 0) +
        stat("Занято МБ", st.total_mb || 0) +
        stat("Свободно МБ", st.free_mb == null ? "—" : st.free_mb) +
        stat("Активный слот", esc(slots.active || "—")) +
        stat("Режим восстановления",
          pill(!!mode.mode && mode.mode.active, "включён", "выключен")) +
        stat("Планировщик", pill((s.scheduler || {}).running, "работает", "остановлен"));
      host.appendChild(grid);
      var card = el("div", "rg-card");
      card.innerHTML = "<h2>Действия</h2>";
      var row = el("div", "rg-row");
      row.appendChild(btn("Снять снимок сейчас", "rg-btn", function () {
        postJSON(API + "/snapshot", { force: true }).then(function (r) {
          toast(r.data.created ? "Снимок создан: " + r.data.id : "Снимок не создан: " + (r.data.reason || ""));
          load("overview");
        });
      }));
      row.appendChild(btn("Чистка по лимитам", "rg-btn rg-ghost", function () {
        postJSON(API + "/prune", {}).then(function (r) {
          toast("Удалено: " + ((r.data.removed || []).length) + ", освобождено МБ: " + (r.data.freed_mb || 0));
          load("overview");
        });
      }));
      card.appendChild(row);
      host.appendChild(card);
    });
  }

  function btn(label, cls, fn) {
    var b = el("button", cls, esc(label));
    b.onclick = fn;
    return b;
  }

  // ---------------------------------------------------------- diagnose ----
  function loadDiagnose() {
    return getJSON(API + "/diagnose").then(function (d) {
      var host = panel("diagnose");
      if (!host) return;
      host.innerHTML = "";
      var info = el("div", "rg-card");
      info.innerHTML = "<h2>Состояние запуска</h2><div class='rg-muted'>boot_ok: " +
        (d.boot_ok ? "да" : "нет") + " · crash_count: " + (d.crash_count || 0) +
        " · checkpoint: " + esc(d.checkpoint || "—") +
        " · лог: " + esc(d.log_source || "не найден") + "</div>";
      host.appendChild(info);
      var hyps = d.hypotheses || [];
      if (!hyps.length) {
        host.appendChild(el("div", "rg-card", "<div class='rg-muted'>Признаков сбоя не найдено.</div>"));
      }
      hyps.forEach(function (h) {
        var cls = h.confidence >= 80 ? "high" : (h.confidence < 50 ? "low" : "");
        var box = el("div", "rg-hypo " + cls);
        box.innerHTML =
          "<div class='rg-row'><b>" + esc(h.title) + "</b>" +
          "<span class='rg-pill " + (h.confidence >= 80 ? "rg-bad" : "rg-warn") + "'>" +
          h.confidence + "%</span></div>" +
          "<div class='rg-bar' style='margin:8px 0'><i style='width:" + h.confidence + "%'></i></div>" +
          "<div class='rg-muted'>Доказательства:</div><pre class='rg-code'>" +
          esc((h.evidence || []).join("\n")) + "</pre>" +
          "<div>" + esc(h.fix || "") + "</div>";
        host.appendChild(box);
      });
      if (d.log_tail && d.log_tail.length) {
        var lg = el("div", "rg-card");
        lg.innerHTML = "<h2>Хвост server.log</h2><pre class='rg-code'>" +
          esc(d.log_tail.join("\n")) + "</pre>";
        host.appendChild(lg);
      }
    });
  }

  // ---------------------------------------------------------- snapshots ---
  function loadSnapshots() {
    return getJSON(API + "/backups").then(function (d) {
      var host = panel("snapshots");
      if (!host) return;
      host.innerHTML = "";
      var items = d.backups || [];
      var tbl = el("table", "rg-tbl");
      tbl.innerHTML = "<tr><th>ID</th><th>Дата</th><th>МБ</th><th>Код</th>" +
        "<th>Проверка</th><th>Статус</th><th>Пин</th><th></th></tr>";
      items.forEach(function (b) {
        var tr = el("tr");
        var status = b.broken ? pill(false, "", "битый") :
          (b.tampered ? pill(false, "", "подменён") :
            (b.ok ? pill(true, "цел", "повреждён") : pill(false, "", "повреждён")));
        var vlvl = b.verify_level || "—";
        var vcls = (b.verify && b.verify.code_ok === false) ? "rg-bad" : "rg-good";
        tr.innerHTML =
          "<td>" + esc(b.id) + "</td><td>" + esc(b.ts_human) + "</td>" +
          "<td>" + (b.size_mb || 0) + "</td><td>" + (b.code_count || 0) + "</td>" +
          "<td><span class='rg-pill " + vcls + "'>" + esc(vlvl) + "</span></td>" +
          "<td>" + status + "</td>" +
          "<td>" + (b.pinned ? "📌" : "") + "</td><td></td>";
        var cell = tr.lastChild;
        cell.appendChild(smallBtn("Проверить L1", function () { verify(b.id, "L1"); }));
        cell.appendChild(smallBtn("Закрепить", function () { pin(b.id, !b.pinned); }));
        cell.appendChild(smallBtn("Удалить", function () { del(b.id); }, "rg-danger"));
        tbl.appendChild(tr);
      });
      var card = el("div", "rg-card");
      card.innerHTML = "<h2>Снимки (" + items.length + ")</h2>";
      var row = el("div", "rg-row");
      row.appendChild(btn("Создать снимок", "rg-btn", function () {
        postJSON(API + "/snapshot", { force: true }).then(function (r) {
          toast(r.data.created ? "Создан " + r.data.id : "Не создан: " + (r.data.reason || ""));
          load("snapshots");
        });
      }));
      card.appendChild(row);
      card.appendChild(tbl);
      host.appendChild(card);
    });
  }
  function smallBtn(label, fn, cls) {
    var b = el("button", "rg-btn " + (cls || "rg-ghost"), esc(label));
    b.style.padding = "5px 8px";
    b.style.fontSize = "12px";
    b.style.marginRight = "4px";
    b.onclick = fn;
    return b;
  }
  function verify(id, level) {
    toast("Проверка " + id + " (" + level + ")…");
    postJSON(API + "/backups/" + encodeURIComponent(id) + "/verify", { level: level })
      .then(function (r) {
        var res = r.data.result || {};
        toast((res.ok ? "OK: " : "НЕ прошёл: ") + id + " (" + (res.error_count || 0) + " ошибок)");
        load("snapshots");
      });
  }
  function pin(id, pinned) {
    postJSON(API + "/backups/" + encodeURIComponent(id) + "/pin", { pinned: pinned })
      .then(function () { load("snapshots"); });
  }
  function del(id) {
    if (!confirm("Удалить снимок " + id + "?")) return;
    fetch(API + "/backups/" + encodeURIComponent(id), {
      method: "DELETE", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: true }),
    }).then(function () { load("snapshots"); });
  }

  // ----------------------------------------------------------- restore ----
  function loadRestore() {
    return Promise.all([
      getJSON(API + "/backups"),
      getJSON(API + "/recovery-mode").catch(function () { return {}; }),
      getJSON(API + "/slots").catch(function () { return {}; }),
    ]).then(function (r) {
      var items = (r[0].backups || []).filter(function (b) { return b.ok && !b.broken && !b.tampered; });
      var mode = r[1], slots = r[2];
      var host = panel("restore");
      if (!host) return;
      host.innerHTML = "";
      var card = el("div", "rg-card");
      card.innerHTML = "<h2>Восстановление кода из снимка</h2>" +
        "<div class='rg-muted'>Активный слот: " + esc(slots.active || "—") +
        " · restore_lock: " + (mode.lock && mode.lock.locked ? "ДА (нужно подтверждение)" : "нет") + "</div>";
      var sel = el("select");
      sel.id = "restore-snap";
      items.forEach(function (b) {
        var o = el("option", null, esc(b.id + " · " + b.ts_human + " · " + b.size_mb + " МБ"));
        o.value = b.id;
        sel.appendChild(o);
      });
      card.appendChild(el("div", "rg-field", "<label>Снимок</label>")).appendChild(sel);
      var row = el("div", "rg-row");
      row.appendChild(btn("Откатить в слот (canary)", "rg-btn", function () {
        var id = $("#restore-snap").value; if (!id) return;
        toast("Восстанавливаю " + id + " (canary)…");
        postJSON(API + "/slots/restore", { id: id, canary: true }).then(function (r) {
          toast(r.data.ok ? ("Активен слот " + r.data.active) : ("Не удалось: " + (r.data.error || r.data.stage)));
          load("restore");
        });
      }));
      row.appendChild(btn("Включить режим восстановления", "rg-btn rg-warn", function () {
        var id = $("#restore-snap").value; if (!id) return;
        postJSON(API + "/recovery-mode", { id: id, reason: "manual" }).then(function (r) {
          toast(r.data.ok ? "Режим включён" : ("Ошибка: " + (r.data.error || r.data.stage)));
          load("restore");
        });
      }));
      row.appendChild(btn("Снять режим", "rg-btn rg-ghost", function () {
        postJSON(API + "/recovery-mode/clear", {}).then(function (r) {
          toast(r.data.ok ? "Режим снят" : ("Ошибка: " + (r.data.error || "")));
          load("restore");
        });
      }));
      row.appendChild(btn("Разблокировать откаты", "rg-btn rg-ghost", function () {
        postJSON(API + "/recovery-mode/unlock", { reason: "manual via UI" }).then(function () {
          toast("restore_lock снят"); load("restore");
        });
      }));
      card.appendChild(row);
      host.appendChild(card);
    });
  }

  // ---------------------------------------------------------- settings ----
  function loadSettings() {
    return Promise.all([
      getJSON(API + "/config"),
      getJSON(API + "/disk").catch(function () { return {}; }),
    ]).then(function (r) {
      var cfg = r[0].config || {}, schema = r[0].schema || {}, disk = r[1];
      var host = panel("settings");
      if (!host) return;
      host.innerHTML = "";
      var card = el("div", "rg-card");
      card.innerHTML = "<h2>Настройки резервного копирования</h2>";
      Object.keys(schema).forEach(function (key) {
        var spec = schema[key];
        var val = getPath(cfg, key);
        var field = el("div", "rg-field");
        field.appendChild(el("label", null, esc(key)));
        var input;
        if (spec.type === "enum" && spec.choices) {
          input = el("select");
          spec.choices.forEach(function (c) {
            var o = el("option", null, esc(String(c))); o.value = String(c);
            if (String(c) === String(val)) o.selected = true;
            input.appendChild(o);
          });
        } else if (spec.type === "bool") {
          input = el("select");
          [["true", "да"], ["false", "нет"]].forEach(function (p) {
            var o = el("option", null, p[1]); o.value = p[0];
            if ((val ? "true" : "false") === p[0]) o.selected = true;
            input.appendChild(o);
          });
        } else {
          input = el("input");
          input.type = "number";
          if (spec.type === "float") input.step = "0.01";
          if (spec.min != null) input.min = spec.min;
          if (spec.max != null) input.max = spec.max;
          input.value = val == null ? "" : val;
        }
        input.dataset.key = key;
        input.dataset.type = spec.type || "int";
        input.id = "cfg-" + key.replace(/\./g, "__");
        field.appendChild(input);
        card.appendChild(field);
      });
      var row = el("div", "rg-row");
      row.appendChild(btn("Применить", "rg-btn", function () {
        var body = {};
        document.querySelectorAll('.rg-panel[data-panel="settings"] [data-key]').forEach(function (inp) {
          var v = inp.value;
          if (v === "true") v = true; else if (v === "false") v = false;
          else if (inp.dataset.type === "float") v = parseFloat(v);
          else if (inp.type === "number") v = parseInt(v, 10);
          body[inp.dataset.key] = v;
        });
        postJSON(API + "/config", body).then(function (r) {
          if (r.data.ok) toast("Настройки применены (без рестарта)");
          else toast("Ошибки: " + JSON.stringify(r.data.errors));
          load("settings");
        });
      }));
      row.appendChild(btn("Сбросить к умолчаниям", "rg-btn rg-danger", function () {
        if (!confirm("Сбросить все настройки к умолчаниям?")) return;
        postJSON(API + "/config/reset", {}).then(function () { load("settings"); });
      }));
      card.appendChild(row);
      if (disk && disk.free_mb != null) {
        card.appendChild(el("div", "rg-muted",
          "Диск: свободно " + disk.free_mb + " МБ · снимки " + disk.backups_mb +
          " МБ из " + disk.max_total_mb + " МБ · остаток " + disk.headroom_mb + " МБ"));
      }
      host.appendChild(card);
    });
  }
  function getPath(obj, dotted) {
    var cur = obj;
    dotted.split(".").forEach(function (p) { cur = cur == null ? null : cur[p]; });
    return cur;
  }

  // ---------------------------------------------------------- security ----
  function loadSecurity() {
    return getJSON(SEC + "/status").then(function (d) {
      var host = panel("security");
      if (!host) return;
      host.innerHTML = "";
      var s = d.stats || {};
      var grid = el("div", "rg-grid");
      grid.innerHTML =
        stat("Пропущено", s.allowed || 0) +
        stat("Отказ (лимит)", s.denied_rate || 0) +
        stat("Отказ (503)", s.denied_conc || 0) +
        stat("Отказ (413)", s.denied_body || 0) +
        stat("Отказ (квота)", s.denied_quota || 0) +
        stat("Банов", s.banned || 0) +
        stat("Параллельно", (d.concurrent || 0) + " / " + (d.max_concurrent || 0)) +
        stat("Paranoid", pill(!!d.paranoid, "вкл", "выкл"));
      host.appendChild(grid);
      var card = el("div", "rg-card");
      card.innerHTML = "<h2>Заблокированные (разбан)</h2>";
      var banned = d.banned || [];
      if (!banned.length) {
        card.appendChild(el("div", "rg-muted", "Заблокированных нет."));
      }
      banned.forEach(function (b) {
        var tr = el("div", "rg-row");
        tr.style.marginBottom = "6px";
        tr.innerHTML = "<code>" + esc(b.key) + "</code> <span class='rg-muted'>осталось " +
          b.ttl_left + "с</span>";
        tr.appendChild(smallBtn("Разбанить", function () {
          postJSON(SEC + "/unban", { key: b.key }).then(function () { load("security"); });
        }));
        card.appendChild(tr);
      });
      host.appendChild(card);
    });
  }

  // ------------------------------------------------------------ audit -----
  function loadAudit() {
    return getJSON(API + "/audit?limit=300").then(function (d) {
      var host = panel("audit");
      if (!host) return;
      host.innerHTML = "";
      var entries = (d.entries || []).slice().reverse();
      var tbl = el("table", "rg-tbl");
      tbl.innerHTML = "<tr><th>Время</th><th>Действие</th><th>Снимок</th><th>Результат</th></tr>";
      entries.forEach(function (e) {
        var tr = el("tr");
        tr.innerHTML = "<td>" + esc(e.time || "") + "</td><td>" + esc(e.action || "") +
          "</td><td>" + esc(e.snapshot || "") + "</td><td>" + esc(e.result || "") + "</td>";
        tbl.appendChild(tr);
      });
      var card = el("div", "rg-card");
      card.innerHTML = "<h2>Журнал действий (" + entries.length + ")</h2>";
      card.appendChild(tbl);
      host.appendChild(card);
    });
  }

  // --------------------------------------------------------- load daemon --
  function loadDaemon() {
    return getJSON("/api/load-daemon/status").then(function (d) {
      var host = panel("load");
      if (!host) return;
      host.innerHTML = "";
      var r = d.resources || {}, lim = d.limits || {};
      var grid = el("div", "rg-grid");
      grid.innerHTML =
        stat("Tier", esc(r.tier || "—")) +
        stat("CPU %", r.cpu_percent || 0) +
        stat("IO-wait %", r.io_wait || 0) +
        stat("RAM своб. ГБ", r.avail_ram_gb || 0) +
        stat("Бюджет", lim.effective_concurrency == null ? "—" : lim.effective_concurrency) +
        stat("Загружено (cost)", lim.running_cost || 0) +
        stat("Пауза по нагрузке", pill(!!lim.paused_by_load, "да", "нет")) +
        stat("Starving", (d.starving || []).length);
      host.appendChild(grid);
      var card = el("div", "rg-card");
      card.innerHTML = "<h2>Задачи</h2><pre class='rg-code'>" +
        esc(JSON.stringify({ running: d.running, queued: d.queued,
          quarantined: d.quarantined }, null, 2)) + "</pre>";
      host.appendChild(card);
    });
  }

  // ------------------------------------------------------------- init -----
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".rg-tab").forEach(function (b) {
      b.onclick = function () { showTab(b.dataset.tab); };
    });
    var auto = $("#rg-auto");
    if (auto) {
      auto.onchange = function () {
        if (auto.checked) {
          autoTimer = setInterval(function () { load(current); }, 8000);
        } else if (autoTimer) {
          clearInterval(autoTimer); autoTimer = null;
        }
      };
    }
    var refresh = $("#rg-refresh");
    if (refresh) refresh.onclick = function () { load(current); };
    showTab("overview");
  });
})();
