let DATA = window.__TESTLENS_DATA__;
const RUN_ENABLED = window.__TESTLENS_RUN__ === true;

/* 每个包一套独立状态：勾选、当前运行、轮询定时器都不共享。
   主视图（全部 / 当前更新）是全局的，切包时保留。 */
const state = {
  active: DATA.packages[0].name,
  view: "all",
  query: "",
  pkg: Object.fromEntries(DATA.packages.map((item) => [
    item.name,
    { chosen: new Set(), run: null, timer: null },
  ])),
  drawerView: "current",
  drawerOpen: false,
  showDurations: false,
  allRun: null,
  history: [],
  historyLoaded: false,
  historySelected: null,
  historyDetail: null,
  coverage: null,
  coverageFile: null,
};

const el = (id) => document.getElementById(id);
const currentPkg = () => DATA.packages.find((item) => item.name === state.active);
const currentState = () => state.pkg[state.active];

// ?list=changed 直接看更新列表；?view=coverage|history 直接打开抽屉页签
const urlParams = new URLSearchParams(location.search);
const requestedList = urlParams.get("list");
const requestedView = urlParams.get("view");
const requestedCovFile = urlParams.get("cov");
const requestedQuery = urlParams.get("q");
if (requestedList === "changed") state.view = "changed";
if (requestedQuery) state.query = requestedQuery;
if (requestedView === "coverage" || requestedView === "history") {
  state.drawerView = requestedView;
  state.drawerOpen = true;   // URL 指定了页签，面板要跟着打开
}

const esc = (value) => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

/* ---------------- 复制到剪贴板 ---------------- */

/* 按钮原文按元素记一次：连点两下时第二次读到的是「已复制」，
   直接拿 textContent 当原文会让文案永远停在「已复制」。 */
const copyLabels = new WeakMap();

async function copyText(text, button) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch {
    // file:// 或非安全上下文里 Clipboard API 不可用，退回 execCommand
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.top = "-9999px";
    document.body.appendChild(area);
    area.select();
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    area.remove();
  }

  if (button) {
    if (!copyLabels.has(button)) copyLabels.set(button, button.textContent);
    const label = copyLabels.get(button);
    button.textContent = ok ? "已复制" : "复制失败";
    button.classList.toggle("copied", ok);
    setTimeout(() => {
      button.textContent = label;
      button.classList.remove("copied");
    }, 1400);
  }
  return ok;
}

const pad = (n) => String(n).padStart(2, "0");
function fmtTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso || "");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
         `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const RUN_STATUS = {
  running: "运行中",
  passed: "全部通过",
  failed: "有失败",
  stopped: "已停止",
  error: "启动失败",
};

/* ---------------- 数据刷新 ---------------- */

function applyPayload(payload) {
  if (!payload || !Array.isArray(payload.packages)) return;
  DATA = payload;

  // 包还在不在
  if (!DATA.packages.some((item) => item.name === state.active)) {
    state.active = DATA.packages[0].name;
  }
  // 每个包都要有状态槽位
  for (const item of DATA.packages) {
    if (!state.pkg[item.name]) {
      state.pkg[item.name] = { chosen: new Set(), run: null, timer: null };
    }
  }
  // 勾选里可能残留已经删掉的脚本
  const alive = new Set(
    DATA.packages.flatMap((item) => item.scripts.map((script) => script.path)),
  );
  for (const slot of Object.values(state.pkg)) {
    for (const path of [...slot.chosen]) {
      if (!alive.has(path)) slot.chosen.delete(path);
    }
  }
  render();
}

async function refreshScripts() {
  try {
    const response = await fetch("/api/scripts");
    const data = await response.json();
    if (response.ok) applyPayload(data);
  } catch {
    /* 静态页没有后端，忽略 */
  }
}

async function confirmAll() {
  if (!RUN_ENABLED) return;
  el("btn-confirm").disabled = true;
  try {
    const response = await fetch("/api/scripts/confirm", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "确认失败");
    applyPayload(data.data);
  } catch (error) {
    el("run-badge").textContent = "确认失败";
    el("run-badge").className = "badge error";
    el("run-meta").textContent = String(error.message || error);
  }
}

/* ---------------- 渲染 ---------------- */

function statusTag(script) {
  if (script.status === "changed") {
    const diff = script.diff;
    const detail = diff && (diff.added || diff.removed)
      ? ` +${diff.added} -${diff.removed}`
      : "";
    return `<span class="tag changed">已更新${detail}</span>`;
  }
  if (script.status === "new") return '<span class="tag new">新增</span>';
  if (script.status === "missing") return '<span class="tag missing">已删除</span>';
  if (script.status === "unreadable") return '<span class="tag missing">无法读取</span>';
  return '<span class="tag unchanged">无变化</span>';
}

function scriptCard(script, chosen) {
  const isChosen = chosen.has(script.path);
  const disabled = script.status === "missing" || script.status === "unreadable";
  return `
    <div class="card ${isChosen ? "chosen" : ""} ${disabled ? "missing" : ""}"
         data-path="${esc(script.path)}">
      <input type="checkbox" class="pick" data-path="${esc(script.path)}"
             ${isChosen ? "checked" : ""}
             ${RUN_ENABLED && !disabled ? "" : "disabled"}>
      <div class="card-body">
        <span class="name">${esc(script.name)}</span>
        <span class="meta">${esc(script.dir)}</span>
      </div>
      ${statusTag(script)}
      <span class="count-chip">${script.tests} 个测试函数</span>
      ${RUN_ENABLED ? `
        <button class="run-one" data-run-one="${esc(script.path)}"
                title="只跑这个脚本" ${disabled ? "disabled" : ""}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <polygon points="7 4 19 12 7 20 7 4"></polygon>
          </svg>
          执行
        </button>` : ""}
    </div>`;
}

function renderTabs() {
  el("tabs").innerHTML = DATA.packages.map((item) => `
    <button class="tab ${item.name === state.active ? "active" : ""}"
            data-pkg="${esc(item.name)}" role="tab"
            aria-selected="${item.name === state.active}">
      ${esc(item.label)}
      <span class="tab-count">${item.total_scripts}</span>
    </button>`).join("");
}

function renderViews() {
  const pkg = currentPkg();
  el("views").innerHTML = `
    <button data-list="all" class="${state.view === "all" ? "active" : ""}">
      全部 <span class="tab-count">${pkg.total_scripts}</span>
    </button>
    <button data-list="changed" class="${state.view === "changed" ? "active" : ""}">
      当前更新 <span class="tab-count">${pkg.changed_scripts}</span>
    </button>`;
}

function groupOf(script) {
  // 分组键由后端算好下发（PackageSpec.tests_dir 可配置，前端不再猜前缀）
  return script.group || "（根目录）";
}

/* 「可见脚本」= 当前视图 ∩ 当前搜索。全选、分组全选都跟随它，
   而不是磁盘上的全部文件——搜索 security 时全选就只选匹配的那 1 个。 */
function selectable(script) {
  return script.status !== "missing" && script.status !== "unreadable";
}

function viewScripts() {
  const pkg = currentPkg();
  return state.view === "changed"
    ? pkg.scripts.filter((script) => script.changed)
    : pkg.scripts;
}

function visibleScripts() {
  const query = state.query.trim().toLowerCase();
  const base = viewScripts();
  if (!query) return base;
  return base.filter((script) =>
    script.name.toLowerCase().includes(query) ||
    script.path.toLowerCase().includes(query));
}

function renderContent() {
  const chosen = currentState().chosen;
  const query = state.query.trim().toLowerCase();
  const total = viewScripts().length;
  const scripts = visibleScripts();

  el("search-hint").textContent = query
    ? `匹配 ${scripts.length} / ${total} 个文件`
    : "";
  el("btn-search-clear").hidden = !state.query;

  if (!scripts.length) {
    let text;
    if (query) {
      text = `<strong>没有匹配「${esc(state.query)}」的文件</strong>
        换个关键词，或点「清空」恢复全部文件。`;
    } else if (state.view === "changed") {
      text = `<strong>没有更新的脚本</strong>这个包所有测试脚本都与缓存一致。改过代码后刷新页面即可看到。`;
    } else {
      text = `<strong>没有找到测试脚本</strong>这个包下面还没有 test_*.py。`;
    }
    el("content").innerHTML = `<div class="empty">${text}</div>`;
    return;
  }

  // 按所在目录分组（unit / integration / e2e / smoke），比按修改时间稳定
  const order = ["unit", "integration", "e2e", "smoke"];
  const groups = new Map();
  for (const script of scripts) {
    const key = groupOf(script);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(script);
  }
  const keys = [...groups.keys()].sort((a, b) => {
    const ai = order.indexOf(a);
    const bi = order.indexOf(b);
    if (ai !== -1 || bi !== -1) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    return a.localeCompare(b);
  });

  el("content").innerHTML = keys.map((key) => `
    <div class="group" data-group="${esc(key)}">
      <div class="group-head">
        <button class="group-pick" data-group-pick="${esc(key)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <rect x="3" y="3" width="18" height="18" rx="3"></rect>
            <polyline points="8 12 11 15 16 9"></polyline>
          </svg>
          <span>全选</span>
        </button>
        <span class="group-name">${esc(key)}</span>
        <span class="group-count">${groups.get(key).length} 个脚本</span>
      </div>
      ${groups.get(key).map((script) => scriptCard(script, chosen)).join("")}
    </div>`).join("");

  updatePickButtons();
}

function syncButtons() {
  const count = currentState().chosen.size;
  const busy = allRunBusy();
  el("btn-run").textContent = count ? `运行选中 (${count})` : "运行选中";
  el("btn-run").disabled = !RUN_ENABLED || count === 0 || busy;
  el("btn-run-all").disabled = !RUN_ENABLED || busy
    || !DATA.packages.some((pkg) => pkg.scripts.some(selectable));
  el("btn-unpick").disabled = !RUN_ENABLED || count === 0;
  el("btn-confirm").disabled = !RUN_ENABLED || currentPkg().changed_scripts === 0;
  updatePickButtons();
}

function updateHistoryCount() {
  const count = state.history.filter((item) => item.package === state.active).length;
  el("history-count").textContent = count;
}

function render() {
  renderTabs();
  renderViews();
  renderContent();
  syncButtons();
  updateHistoryCount();
  renderDrawer();
}

/* ---------------- 选择 ---------------- */

function toggle(path, value) {
  const script = currentPkg().scripts.find((item) => item.path === path);
  if (script && (script.status === "missing" || script.status === "unreadable")) return;

  const active = currentState();
  const next = value === undefined ? !active.chosen.has(path) : value;
  if (next) active.chosen.add(path); else active.chosen.delete(path);

  const card = document.querySelector(`.card[data-path="${CSS.escape(path)}"]`);
  if (card) {
    card.classList.toggle("chosen", next);
    const box = card.querySelector(".pick");
    if (box) box.checked = next;
  }
  syncButtons();
}

/* ---- 批量勾选：只更新受影响的卡片，不重绘列表（不丢滚动位置） ---- */
function setChosen(paths, select) {
  const active = currentState();
  for (const path of paths) {
    if (select) active.chosen.add(path); else active.chosen.delete(path);
    const card = document.querySelector(`.card[data-path="${CSS.escape(path)}"]`);
    if (!card) continue;
    card.classList.toggle("chosen", select);
    const box = card.querySelector(".pick");
    if (box) box.checked = select;
  }
  syncButtons();
  updatePickButtons();
}

function pickable(list) {
  return list.filter(selectable).map((script) => script.path);
}

function togglePickList(list) {
  const chosen = currentState().chosen;
  const paths = pickable(list);
  if (!paths.length) return;
  setChosen(paths, !paths.every((path) => chosen.has(path)));
}

function toggleGroup(key) {
  togglePickList(visibleScripts().filter((script) => groupOf(script) === key));
}

function toggleVisible() {
  togglePickList(visibleScripts());
}

function toggleAll() {
  togglePickList(currentPkg().scripts);
}

/* 三态文案：整组未选 →「全选」；选了一部分 →「已选 3」；全选 →「取消」 */
function updatePickButtons() {
  const chosen = currentState().chosen;

  const visiblePaths = pickable(visibleScripts());
  const picked = visiblePaths.filter((path) => chosen.has(path)).length;
  const visibleBtn = el("btn-pick-visible");
  visibleBtn.hidden = !RUN_ENABLED || visiblePaths.length === 0;
  visibleBtn.textContent = picked > 0 && picked === visiblePaths.length
    ? "取消全选"
    : `全选 ${visiblePaths.length} 个`;

  for (const group of document.querySelectorAll(".group")) {
    const button = group.querySelector(".group-pick");
    if (!button) continue;
    const paths = pickable(visibleScripts().filter((script) => groupOf(script) === group.dataset.group));
    const on = paths.filter((path) => chosen.has(path)).length;
    // 静态页跑不了测试，全选没有意义，跟工具栏保持一致地藏掉
    button.hidden = !RUN_ENABLED || paths.length === 0;
    button.classList.toggle("active", on > 0);
    button.querySelector("span").textContent = on === 0
      ? "全选"
      : on === paths.length ? "取消" : `已选 ${on}`;
  }

  el("btn-pick-all").disabled = !RUN_ENABLED || pickable(currentPkg().scripts).length === 0;
}

document.addEventListener("change", (event) => {
  const box = event.target;
  if (!box.classList || !box.classList.contains("pick")) return;
  toggle(box.dataset.path, box.checked);
});

document.addEventListener("click", (event) => {
  const packTab = event.target.closest("[data-pkg]");
  if (packTab) {
    switchTab(packTab.dataset.pkg);
    return;
  }

  const viewTab = event.target.closest("[data-list]");
  if (viewTab) {
    state.view = viewTab.dataset.list;
    render();
    return;
  }

  const groupPick = event.target.closest("[data-group-pick]");
  if (groupPick) {
    toggleGroup(groupPick.dataset.groupPick);
    return;
  }

  const runOne = event.target.closest("[data-run-one]");
  if (runOne) {
    runOneScript(runOne);
    return;
  }

  if (!RUN_ENABLED) return;
  const card = event.target.closest(".card");
  if (card && !event.target.closest("input")) toggle(card.dataset.path);
});

function switchTab(name) {
  if (!state.pkg[name] || state.active === name) return;
  state.active = name;
  // 历史/覆盖率都是跟着包走的：换了包就别再显示上一个包的数据
  state.historyDetail = null;
  state.historySelected = null;
  state.coverage = null;
  state.coverageFile = null;
  state.coverageDetail = null;
  render();
  prefetchCoverage();
}

/* ---------------- 抽屉 ---------------- */

function renderDrawer() {
  const active = currentState();
  const hasRun = !!active.run;
  const wantHistory = state.drawerView === "history";
  const wantCoverage = state.drawerView === "coverage";

  // 「运行面板」按钮常驻：面板默认收起，点按钮才滑出；再点左边缘的收起钮回去。
  // 没有运行记录时展开显示的是「待运行」空状态，不会是一片空白。
  const open = state.drawerOpen;
  el("drawer").classList.toggle("open", open);
  el("drawer").setAttribute("aria-hidden", open ? "false" : "true");
  el("drawer-handle").hidden = open;

  el("view-current").classList.toggle("active", !wantHistory && !wantCoverage);
  el("view-history").classList.toggle("active", wantHistory);
  el("view-coverage").classList.toggle("active", wantCoverage);
  el("current-view").hidden = wantHistory || wantCoverage;
  el("history-view").hidden = !wantHistory;
  el("coverage-view").hidden = !wantCoverage;
  el("all-run").hidden = !(state.allRun && !wantHistory && !wantCoverage);

  if (wantHistory) {
    el("run-badge").textContent = "运行历史";
    el("run-badge").className = "badge";
    const count = state.history.filter((item) => item.package === state.active).length;
    el("run-meta").textContent = `${currentPkg().label} · ${count} 次运行`;
    el("run-log").hidden = true;
    el("btn-stop").hidden = true;
    el("btn-copy-run").hidden = true;
    renderHistory();
    return;
  }

  if (wantCoverage) {
    el("run-badge").textContent = "覆盖率";
    el("run-badge").className = "badge";
    el("run-meta").textContent = state.coverage
      ? `${state.coverage.label} · 数据于 ${state.coverage.generated_at.replace("T", " ")}`
      : `${currentPkg().label} · 加载中…`;
    el("run-log").hidden = true;
    el("btn-stop").hidden = true;
    el("btn-copy-run").hidden = true;
    renderCoverage();
    return;
  }

  if (state.allRun) {
    el("run-badge").textContent = allRunBusy() ? "全部执行中" : "全部执行";
    el("run-badge").className = "badge" + (allRunBusy() ? " running" : "");
    el("run-meta").textContent = `${state.allRun.items.length} 个包`;
    el("run-log").hidden = true;
    el("btn-stop").hidden = true;
    el("btn-copy-run").hidden = true;
    renderAllRun();
    return;
  }

  if (!hasRun) {
    el("run-badge").textContent = "待运行";
    el("run-badge").className = "badge";
    el("run-meta").textContent = `${currentPkg().label} · 还没有运行记录`;
    el("run-empty").hidden = false;
    el("run-output").hidden = true;
    el("run-durations").hidden = true;
    el("run-summary").hidden = true;
    state.showDurations = false;
    el("run-log").hidden = true;
    el("btn-stop").hidden = true;
    el("btn-copy-run").hidden = true;
    return;
  }
  renderRun(active.run);
}

function renderRun(run) {
  const badge = el("run-badge");
  badge.textContent = RUN_STATUS[run.status] || run.status;
  badge.className = "badge " + run.status;

  const parts = [];
  if (run.title) parts.push(run.title);
  // 有结构化结果时，耗时/退出码已经在摘要条里，这里不再重复
  if (!run.results) {
    if (run.duration !== null && run.duration !== undefined) {
      parts.push(`耗时 ${run.duration.toFixed(1)}s`);
    }
    if (run.exit_code !== null && run.exit_code !== undefined) {
      parts.push(`退出码 ${run.exit_code}`);
    }
  }
  el("run-meta").textContent = parts.join(" · ");

  el("run-empty").hidden = true;
  renderRunSummary(run);

  const output = el("run-output");
  const atBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
  const keepScroll = output.scrollTop;
  output.textContent = run.output || "";
  output.scrollTop = atBottom ? output.scrollHeight : keepScroll;

  el("btn-stop").hidden = run.status !== "running" || !run.run_id;
  el("run-log").hidden = !run.log_available;
  el("btn-copy-run").hidden = !(run.output || "").length;
  if (run.log_available) el("run-log").href = `/api/runs/${run.run_id}/log`;
}

/* ---------------- 运行摘要与耗时 ---------------- */

function fmtCost(seconds) {
  const value = Number(seconds) || 0;
  return value >= 1 ? `${value.toFixed(2)}s` : `${Math.round(value * 1000)}ms`;
}

/* 包在仓库里的路径前缀。"." 表示包根就是项目根，此时前缀为空。 */
function pkgPrefix() {
  const root = currentPkg().root || "";
  return root === "." || root === "" ? "" : root + "/";
}

function shortPath(path) {
  const root = pkgPrefix();
  return path && path.startsWith(root) ? path.slice(root.length) : (path || "");
}

/* 失败用例所在脚本（去重）：这是「只跑失败」要传给后端的东西 */
function failureFiles(results) {
  const list = (results && results.failures) || [];
  return [...new Set(list.map((item) => item.file).filter(Boolean))];
}

function failureText(results) {
  return ((results && results.failures) || [])
    .map((item) => `${item.name}  ${item.file}\n${item.message}`)
    .join("\n\n");
}

/* 同包上一次有耗时的运行；history 是最新在前，取第一条即可 */
function previousDuration(run) {
  const list = state.history || [];
  const prev = list.find((item) =>
    item.package === run.package && item.run_id !== run.run_id && item.duration > 0);
  return prev ? prev.duration : null;
}

/* 耗时面板：脚本条形 → 最慢用例 → 失败用例。历史详情复用同一段 */
function renderResults(results) {
  const files = results.files || [];
  const slowest = results.slowest || [];
  const failures = results.failures || [];
  const maxTime = Math.max(0.001, ...files.map((item) => item.time));

  const fileRows = files.map((item) => `
    <div class="dur-row">
      <span class="dur-name" title="${esc(item.path)}">${esc(shortPath(item.path))}</span>
      <span class="dur-bar"><i style="width:${Math.max(2, Math.round((item.time / maxTime) * 100))}%"></i></span>
      <span class="dur-time">${fmtCost(item.time)}</span>
    </div>`).join("");

  const caseRows = slowest.map((item) => `
    <div class="dur-row case">
      <span class="dur-name" title="${esc(item.name)} · ${esc(shortPath(item.file))}">${esc(item.name)}</span>
      <span class="dur-time">${fmtCost(item.time)}</span>
    </div>`).join("");

  const failRows = failures.map((item) => `
    <div class="fail-item">
      <div class="fail-name">${esc(item.name)}</div>
      <div class="fail-file">${esc(shortPath(item.file))}</div>
      ${item.message ? `<div class="fail-msg">${esc(item.message)}</div>` : ""}
    </div>`).join("");

  return `
    <div class="dur-section">
      <div class="dur-head">按脚本耗时<span>${files.length} 个脚本</span></div>
      ${fileRows || '<div class="hd-empty">这次运行没有留下脚本耗时。</div>'}
    </div>
    <div class="dur-section">
      <div class="dur-head">最慢用例<span>Top ${slowest.length}</span></div>
      ${caseRows || '<div class="hd-empty">没有用例级数据。</div>'}
    </div>
    ${failures.length ? `
      <div class="dur-section">
        <div class="dur-head">失败用例<span>${failures.length} 条</span>
          <button class="copy-btn" data-copy="failures">复制</button>
        </div>
        ${failRows}
      </div>` : ""}`;
}

function renderRunSummary(run) {
  const results = run.results;
  const summary = el("run-summary");
  const hasResults = !!(results && results.totals);

  summary.hidden = !hasResults;
  if (!hasResults) {
    // 老历史或 junit 解析失败：退回纯输出
    el("run-durations").hidden = true;
    el("run-output").hidden = false;
    state.showDurations = false;
    return;
  }

  const totals = results.totals;
  const failed = totals.failed + totals.errors;
  el("run-summary-dot").className = "rs-dot" + (failed ? " failed" : "");

  const parts = [`${totals.tests} 用例`, `${totals.passed} 通过`];
  if (failed) parts.push(`${failed} 失败`);
  if (totals.skipped) parts.push(`${totals.skipped} 跳过`);
  parts.push(fmtCost(run.duration || 0));
  el("run-summary-text").textContent = parts.join(" · ");

  const delta = el("run-summary-delta");
  const prev = previousDuration(run);
  if (prev && prev > 0.05) {
    const diff = ((run.duration || 0) - prev) / prev;
    if (Math.abs(diff) >= 0.05) {
      delta.textContent = `比上次${diff < 0 ? "快" : "慢"} ${Math.round(Math.abs(diff) * 100)}%`;
      delta.className = "rs-delta " + (diff < 0 ? "faster" : "slower");
    } else {
      delta.textContent = "与上次持平";
      delta.className = "rs-delta";
    }
  } else {
    delta.textContent = "";
    delta.className = "rs-delta";
  }

  const files = failureFiles(results);
  const rerun = el("btn-rerun-failed");
  rerun.hidden = !RUN_ENABLED || files.length === 0;
  rerun.textContent = `只跑失败 (${files.length})`;

  const showDur = state.showDurations;
  el("btn-durations").textContent = showDur ? "输出" : "耗时";
  el("btn-durations").classList.toggle("active", showDur);
  el("run-durations").innerHTML = renderResults(results);
  el("run-durations").hidden = !showDur;
  el("run-output").hidden = showDur;
}

el("btn-durations").onclick = () => {
  state.showDurations = !state.showDurations;
  renderDrawer();
};

el("btn-rerun-failed").onclick = () => {
  const run = currentState().run;
  const files = failureFiles(run && run.results);
  if (!files.length) return;
  startRun(files, `只跑失败的 ${files.length} 个脚本`);
};

el("run-durations").addEventListener("click", (event) => {
  const button = event.target.closest('[data-copy="failures"]');
  if (!button) return;
  const text = failureText(currentState().run && currentState().run.results);
  if (text) copyText(text, button);
});

/* ---------------- 全部执行：跨包并行 ---------------- */

function allRunBusy() {
  const all = state.allRun;
  return !!all && all.items.some((item) => {
    const status = item.run ? item.run.status : item.status;
    return status === "running" || status === "starting";
  });
}

function allRunMeta(item) {
  if (item.error) return item.error;
  const run = item.run;
  if (!run) return "启动中…";
  const totals = run.results && run.results.totals;
  const parts = [`${run.script_total} 脚本`];
  if (totals) {
    const bad = totals.failed + totals.errors;
    parts.push(`${totals.tests} 用例`, `${totals.passed} 通过`);
    if (bad) parts.push(`${bad} 失败`);
    if (totals.skipped) parts.push(`${totals.skipped} 跳过`);
  }
  if (run.duration) parts.push(`${run.duration}s`);
  return parts.join(" · ");
}

function allRunStatusText() {
  const items = state.allRun.items;
  const done = items.filter((item) => item.run && item.run.status !== "running").length;
  const running = items.filter((item) => !item.run || item.run.status === "running").length;
  const failed = items.filter((item) =>
    ["failed", "error", "stopped"].includes(item.run && item.run.status)).length;
  if (running) return `${items.length} 个包 · ${done} 完成 · ${running} 运行中`;
  return failed
    ? `${items.length} 个包 · ${done} 完成 · ${failed} 有失败`
    : `${items.length} 个包 · 全部完成`;
}

function renderAllRun() {
  const all = state.allRun;
  const items = all.items;
  const running = items.some((item) => !item.run || item.run.status === "running");
  const failed = items.some((item) =>
    ["failed", "error", "stopped"].includes(item.run && item.run.status));

  el("run-summary").hidden = true;
  el("run-durations").hidden = true;
  el("run-empty").hidden = true;
  el("run-output").hidden = false;
  el("btn-stop-all").hidden = !running;
  el("all-run-dot").className = "rs-dot" + (failed ? " failed" : "");
  el("all-run-text").textContent = allRunStatusText();

  const focus = all.focus || items[0].package;
  el("all-run-items").innerHTML = items.map((item) => {
    const status = item.run ? item.run.status : item.status;
    const label = status === "starting" ? "运行中" : (RUN_STATUS[status] || status);
    return `
      <button class="all-run-item ${item.package === focus ? "active" : ""}"
              data-all-run="${esc(item.package)}">
        <span class="badge ${status === "starting" ? "running" : status}">${esc(label)}</span>
        <span class="all-run-name">${esc(item.label)}</span>
        <span class="all-run-meta">${esc(allRunMeta(item))}</span>
      </button>`;
  }).join("");

  const current = items.find((item) => item.package === focus) || items[0];
  const text = (current.run && current.run.output)
    || (current.error ? `启动失败：${current.error}` : "正在启动…");
  const output = el("run-output");
  if (output.textContent !== text) {
    const atBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
    output.textContent = text;
    if (atBottom) output.scrollTop = output.scrollHeight;
  }
}

async function runAll() {
  if (!RUN_ENABLED || allRunBusy()) return;

  // 每个包一份「全部可运行脚本」，一次并行各起一个 run
  const targets = DATA.packages
    .map((pkg) => ({
      package: pkg.name,
      label: pkg.label,
      paths: pkg.scripts.filter(selectable).map((script) => script.path),
    }))
    .filter((item) => item.paths.length);
  if (!targets.length) return;

  state.allRun = {
    focus: targets[0].package,
    items: targets.map((item) => ({
      ...item, runId: "", run: null, status: "starting", error: "",
    })),
  };
  state.drawerOpen = true;
  state.drawerView = "current";
  state.showDurations = false;
  renderDrawer();

  for (const item of state.allRun.items) {
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          package: item.package,
          paths: item.paths,
          title: `${item.label} 全量`,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        item.status = "error";
        item.error = data.detail || "服务端拒绝了这次运行";
      } else {
        item.runId = data.run_id;
        item.status = "running";
        pollAllRun(item);
      }
    } catch (error) {
      item.status = "error";
      item.error = String(error.message || error);
    }
    renderDrawer();
  }
}

async function pollAllRun(item) {
  try {
    const response = await fetch(`/api/runs/${item.runId}`);
    const run = await response.json();
    if (!response.ok) throw new Error(run.detail || "读取运行状态失败");
    item.run = run;
    item.status = run.status;

    if (run.status === "running") {
      setTimeout(() => pollAllRun(item), 500);
    } else {
      // 跑完一个包就刷新历史与基线；另一个包可能还在跑
      loadHistory(true);
      refreshScripts();
    }
  } catch (error) {
    item.status = "error";
    item.error = String(error.message || error);
  }
  renderDrawer();
}

el("btn-run-all").onclick = runAll;

el("btn-stop-all").onclick = async () => {
  const all = state.allRun;
  if (!all) return;
  for (const item of all.items) {
    if (item.runId && (!item.run || item.run.status === "running")) {
      await fetch(`/api/runs/${item.runId}/stop`, { method: "POST" });
    }
  }
};

el("all-run-items").addEventListener("click", (event) => {
  const button = event.target.closest("[data-all-run]");
  if (!button || !state.allRun) return;
  state.allRun.focus = button.dataset.allRun;
  renderDrawer();
});

/* ---------------- 历史 ---------------- */

async function loadHistory(force) {
  if (state.historyLoaded && !force) return;
  try {
    const response = await fetch("/api/history");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "读取历史失败");
    state.history = Array.isArray(data.runs) ? data.runs : [];
    state.historyLoaded = true;
    updateHistoryCount();
    if (state.drawerView === "history"
        || (state.drawerView === "current" && (currentState().run || state.allRun))) {
      renderDrawer();
    }
  } catch (error) {
    if (state.drawerView === "history") {
      el("history-list").innerHTML =
        `<div class="hd-empty">读取历史失败：${esc(error.message || error)}</div>`;
    }
  }
}

function packageHistory() {
  return state.history.filter((item) => item.package === state.active);
}

function renderHistory() {
  const runs = packageHistory();

  if (!runs.length) {
    el("history-list").innerHTML =
      '<div class="hd-empty">这个包还没有运行记录。跑一次之后会出现在这里。</div>';
    el("history-detail").innerHTML =
      '<div class="hd-empty">每次运行都会单独记一条（脚本清单 + 完整输出），服务重启后也不会丢。</div>';
    return;
  }

  el("history-list").innerHTML = runs.map((item) => `
    <div class="history-item ${item.run_id === state.historySelected ? "active" : ""}"
         data-history-id="${esc(item.run_id)}">
      <div class="hi-top">
        <span class="badge ${item.status}">${RUN_STATUS[item.status] || item.status}</span>
        <span class="hi-time">${esc(fmtTime(item.started_at))}</span>
      </div>
      <div class="hi-title">${esc(item.title)}</div>
      <div class="hi-meta" title="${item.coverage ? "这次运行同时生成了覆盖率数据" : ""}">${item.script_total} 脚本 · ${item.duration}s · 退出码 ${item.exit_code}${item.coverage ? " · 覆盖率" : ""}</div>
    </div>`).join("");

  if (state.historyDetail && state.historyDetail.package === state.active) {
    renderHistoryDetail();
  } else {
    el("history-detail").innerHTML =
      '<div class="hd-empty">点左边任意一条，查看这次跑了哪些脚本、和上一次比多了什么。</div>';
  }
}

function previousEntry(entry) {
  const runs = packageHistory();
  const index = runs.findIndex((item) => item.run_id === entry.run_id);
  return index >= 0 && index + 1 < runs.length ? runs[index + 1] : null;
}

function renderHistoryDetail() {
  const entry = state.historyDetail;
  if (!entry) return;

  const prev = previousEntry(entry);
  const prevSet = new Set(prev ? prev.scripts : []);
  const currentSet = new Set(entry.scripts);
  const removed = prev ? prev.scripts.filter((path) => !currentSet.has(path)) : [];

  const scripts = entry.scripts.map((path) => {
    const isNew = prev && !prevSet.has(path);
    return `<div class="hd-script">
      ${isNew ? '<span class="hd-mark">新增</span>' : ""}
      <span>${esc(path)}</span>
    </div>`;
  }).join("");

  const removedBlock = removed.length ? `
    <div class="hd-section">上次有、本次没有（${removed.length}）</div>
    <div class="hd-scripts">${removed.map((path) => `
      <div class="hd-script"><span class="hd-mark out">移出</span><span>${esc(path)}</span></div>
    `).join("")}</div>` : "";

  const compareTip = prev
    ? `对比 ${esc(fmtTime(prev.started_at))} 那次`
    : "这是该包的第一条记录，没有可对比的上一次";

  el("history-detail").innerHTML = `
    <div class="hd-head">
      <span class="badge ${entry.status}">${RUN_STATUS[entry.status] || entry.status}</span>
      <span class="hd-time">${esc(fmtTime(entry.started_at))}</span>
      <span class="hd-actions">
        <button class="copy-btn" data-copy="history-scripts" title="复制这次运行的脚本清单">复制脚本清单</button>
        <button class="copy-btn" data-copy="history-output" title="复制这次运行的完整输出">复制输出</button>
      </span>
    </div>
    <div class="hd-sub" style="margin-top:4px">${esc(entry.package_label)} · ${entry.script_total} 个脚本 · ${entry.duration}s · 退出码 ${entry.exit_code}</div>
    <div class="hd-sub">${esc(entry.title)} · ${compareTip}</div>
    <div class="hd-section">本次运行的脚本（${entry.script_total}）</div>
    <div class="hd-scripts">${scripts}</div>
    ${removedBlock}
    ${entry.results ? `
      <div class="hd-section">运行概览</div>
      <div class="hist-results">${renderResults(entry.results)}</div>` : ""}
    <div class="hd-section">完整输出</div>
    <pre class="hd-output"></pre>`;

  const pre = el("history-detail").querySelector(".hd-output");
  pre.textContent = entry.output || "（这次运行没有输出）";
}

async function selectHistory(runId) {
  state.historySelected = runId;
  renderHistory();
  el("history-detail").innerHTML = '<div class="hd-empty">加载中…</div>';

  try {
    const response = await fetch(`/api/history/${runId}`);
    const entry = await response.json();
    if (!response.ok) throw new Error(entry.detail || "读取失败");
    state.historyDetail = entry;
    if (state.active === entry.package) renderHistoryDetail();
  } catch (error) {
    el("history-detail").innerHTML =
      `<div class="hd-empty">读取失败：${esc(error.message || error)}</div>`;
  }
}

function openHistory() {
  state.drawerView = "history";
  state.drawerOpen = true;
  state.historyDetail = null;
  state.historySelected = null;
  renderDrawer();
  loadHistory();
}

/* ---------------- 覆盖率 ---------------- */

function covClass(percent) {
  if (percent >= 95) return "ok";
  if (percent >= 80) return "mid";
  return "low";
}

async function fetchCoverage(force) {
  if (!force && state.coverage && state.coverage.package === state.active) {
    return state.coverage;
  }
  const response = await fetch(`/api/coverage?package=${encodeURIComponent(state.active)}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "读取覆盖率失败");
  state.coverage = data;
  el("coverage-count").textContent = `${data.totals.percent}%`;
  return data;
}

async function prefetchCoverage() {
  try {
    await fetchCoverage(false);
    if (state.drawerView === "coverage") renderDrawer();
  } catch (error) {
    el("coverage-count").textContent = "—";
    el("coverage-count").title = String(error.message || error);
  }
}

async function loadCoverage(force) {
  el("coverage-list").innerHTML = '<div class="hd-empty">加载中…</div>';
  try {
    await fetchCoverage(force);
    renderDrawer();
  } catch (error) {
    el("coverage-list").innerHTML = `<div class="hd-empty">${esc(error.message || error)}</div>`;
    el("coverage-detail").innerHTML = "";
    el("coverage-count").textContent = "—";
  }
}

function renderCoverage() {
  const data = state.coverage;
  if (!data) return;
  const totals = data.totals;
  const root = pkgPrefix();

  const files = data.files.map((file) => {
    const short = file.path.startsWith(root) ? file.path.slice(root.length) : file.path;
    const branchTip = file.branches
      ? `分支 ${file.branches} · ${file.partial_branches} 处未走全`
      : "";
    return `
      <div class="cov-file ${file.path === state.coverageFile ? "active" : ""}"
           data-cov-path="${esc(file.path)}" title="${esc(file.path)}${branchTip ? " · " + branchTip : ""}">
        <div class="cov-file-top">
          <span class="cov-path">${esc(short)}</span>
          <span class="cov-pct ${covClass(file.percent)}">${file.percent}%</span>
        </div>
        <div class="cov-meta">${file.statements} 语句 · ${file.missing} 行未覆盖</div>
      </div>`;
  }).join("");

  el("coverage-list").innerHTML = `
    <div class="coverage-summary">
      <div class="cov-head">
        <span class="cov-percent">${totals.percent}%</span>
        <span class="cov-cap">总覆盖率</span>
      </div>
      <div class="cov-bar">
        <i class="${covClass(totals.percent)}" style="width:${Math.min(totals.percent, 100)}%"></i>
      </div>
      <div class="cov-line">${totals.statements} 语句 · ${totals.missing} 行未覆盖</div>
      <div class="cov-line">分支 ${totals.branch_percent}% · ${totals.partial_branches} 处未走全</div>
      ${RUN_ENABLED ? '<button class="cov-run" data-run-coverage="1">跑全量并统计</button>' : ""}
    </div>` + files;

  if (!state.coverageFile) {
    el("coverage-detail").innerHTML =
      '<div class="hd-empty">点左边任意一个文件，看哪些行没有跑到。</div>';
  } else {
    renderCoverageFile();
  }
}

async function selectCoverageFile(path) {
  state.coverageFile = path;
  renderCoverage();
  el("coverage-detail").innerHTML = '<div class="hd-empty">加载中…</div>';
  try {
    const response = await fetch(
      `/api/coverage/file?package=${encodeURIComponent(state.active)}` +
      `&path=${encodeURIComponent(path)}`,
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "读取失败");
    state.coverageDetail = data;
    renderCoverageFile();
  } catch (error) {
    el("coverage-detail").innerHTML = `<div class="hd-empty">${esc(error.message || error)}</div>`;
  }
}

function renderCoverageFile() {
  const data = state.coverageDetail;
  if (!data || data.path !== state.coverageFile) return;

  const root = pkgPrefix();
  const short = data.path.startsWith(root) ? data.path.slice(root.length) : data.path;

  if (!data.hunks.length) {
    el("coverage-detail").innerHTML = `
      <div class="hd-head">
        <span class="hd-time" title="${esc(data.path)}">${esc(short)}</span>
        <span class="cov-pct ok">100%</span>
        <span class="hd-actions">
          <button class="copy-btn" data-copy="path" title="复制文件路径">复制路径</button>
        </span>
      </div>
      <div class="hd-empty">这个文件没有未覆盖的行。</div>`;
    return;
  }

  const hunks = data.hunks.map((hunk, index) => `
    <div class="cov-hunk">
      <div class="cov-hunk-head">
        <span>第 ${hunk.start}–${hunk.end} 行</span>
        <button class="copy-btn" data-copy="hunk:${index}"
                title="复制这段代码（不含行号）">复制</button>
      </div>
      ${hunk.lines.map((line) => `
        <div class="cov-line-row ${line.missing ? "missing" : ""}">
          <span class="cov-line-no">${line.no}</span>
          <span class="cov-code">${esc(line.code)}</span>
        </div>`).join("")}
    </div>`).join("");

  el("coverage-detail").innerHTML = `
    <div class="hd-head">
      <span class="hd-time" title="${esc(data.path)}">${esc(short)}</span>
      <span class="cov-pct ${covClass(data.percent)}">${data.percent}%</span>
      <span class="hd-actions">
        <button class="copy-btn" data-copy="path" title="复制文件路径">复制路径</button>
        <button class="copy-btn" data-copy="all" title="复制全部上下文（带行号）">复制全部</button>
      </span>
    </div>
    <div class="hd-sub" style="margin-top:4px">${data.statements} 语句 · ${data.missing_lines.length} 行未覆盖</div>
    <div class="hd-section hd-section-row">
      <span>未覆盖行：${data.missing_lines.join(", ")}</span>
      <button class="copy-btn" data-copy="missing" title="复制未覆盖行号">复制行号</button>
    </div>
    ${hunks}`;
}

function openCoverage() {
  state.drawerView = "coverage";
  state.drawerOpen = true;
  state.coverageFile = null;
  state.coverageDetail = null;
  renderDrawer();
  loadCoverage(true);
}

function runCoverage() {
  const pkg = currentPkg();
  const paths = pkg.scripts
    .filter((script) => script.status !== "missing" && script.status !== "unreadable")
    .map((script) => script.path);
  if (!paths.length) {
    el("coverage-detail").innerHTML = '<div class="hd-empty">这个包没有可运行的脚本。</div>';
    return;
  }
  startRun(paths, `全量 ${paths.length} 个脚本（统计覆盖率）`, { coverage: true });
}

/* ---------------- 运行 ---------------- */

async function poll(pkgName, runId) {
  const target = state.pkg[pkgName];
  if (!target) return;
  try {
    const response = await fetch(`/api/runs/${runId}`);
    const run = await response.json();
    if (!response.ok) throw new Error(run.detail || "读取运行状态失败");

    if (target.run && target.run.run_id === runId) target.run = run;
    if (state.active === pkgName && state.drawerView === "current") renderDrawer();

    if (run.status === "running") {
      target.timer = setTimeout(() => poll(pkgName, runId), 400);
    } else {
      // 运行结束就清掉勾选：同一批脚本不该一直亮着，否则下次点「运行选中」
      // 会莫名其妙又跑一遍。只清发起这次运行的包，不碰另一个 tab。
      target.chosen.clear();
      if (state.active === pkgName) render();
      state.coverage = null;
      // 运行前服务端已经把这次脚本写回缓存，回来刷新一下「当前更新」
      setTimeout(() => {
        loadHistory(true);
        refreshScripts();
      }, 300);
    }
  } catch (error) {
    target.run = {
      ...(target.run || {}),
      status: "error",
      output: `读取运行状态失败：${error.message || error}`,
    };
    if (state.active === pkgName && state.drawerView === "current") renderDrawer();
  }
}

function setRunButtonsDisabled(disabled) {
  for (const button of document.querySelectorAll(".run-one")) {
    button.disabled = disabled;
  }
}

function runOneScript(button) {
  const path = button.dataset.runOne;
  if (!path || !RUN_ENABLED || button.disabled) return;
  const card = button.closest(".card");
  const name = card ? (card.querySelector(".name") || {}).textContent || path : path;
  setRunButtonsDisabled(true);
  startRun([path], `单独运行 ${name}`);
}

async function startRun(paths, title, options = {}) {
  if (!RUN_ENABLED || !paths.length) return;
  const pkg = currentPkg();
  const active = currentState();
  const coverage = !!options.coverage;

  clearTimeout(active.timer);
  state.drawerView = "current";
  state.drawerOpen = true;
  state.showDurations = false;
  state.allRun = null;
  active.run = {
    run_id: "",
    package: pkg.name,
    title,
    status: "running",
    output: "正在启动…\n",
    duration: 0,
    script_total: paths.length,
    log_available: false,
  };
  renderDrawer();

  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package: pkg.name, paths, title, coverage }),
    });
    const data = await response.json();
    if (!response.ok) {
      active.run = { ...active.run, status: "error", output: data.detail || "服务端拒绝了这次运行" };
      setRunButtonsDisabled(false);
      renderDrawer();
      return;
    }
    active.run.run_id = data.run_id;
    poll(pkg.name, data.run_id);
  } catch (error) {
    active.run = {
      ...active.run,
      status: "error",
      output: `请求 /api/run 失败：${error.message}\n确认面板服务还在运行（testlens serve）。`,
    };
    setRunButtonsDisabled(false);
    renderDrawer();
  }
}

el("btn-run").onclick = () => {
  const active = currentState();
  startRun([...active.chosen], `选中 ${active.chosen.size} 个脚本`);
};
el("btn-unpick").onclick = () => {
  currentState().chosen.clear();
  render();
};
el("btn-pick-all").onclick = toggleAll;
el("btn-pick-visible").onclick = toggleVisible;

// Ctrl/Cmd+A 全选当前筛选结果；Esc 先清搜索，再清勾选
document.addEventListener("keydown", (event) => {
  const target = event.target;
  const typing = target && (
    target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable
  );
  if (typing) return;

  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    toggleVisible();
    return;
  }
  if (event.key === "Escape") {
    if (state.query) {
      clearSearch();
    } else if (currentState().chosen.size) {
      currentState().chosen.clear();
      render();
    }
  }
});

/* ---------------- 文件名搜索 ---------------- */

function clearSearch() {
  el("search").value = "";
  state.query = "";
  renderContent();
  el("search").focus();
}

el("search").addEventListener("input", (event) => {
  state.query = event.target.value;
  renderContent();               // 只重绘列表，不碰勾选和其它视图
});
el("search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") clearSearch();
});
el("btn-search-clear").onclick = clearSearch;
el("btn-confirm").onclick = confirmAll;
el("btn-stop").onclick = async () => {
  const run = currentState().run;
  if (!run || !run.run_id) return;
  await fetch(`/api/runs/${run.run_id}/stop`, { method: "POST" });
};
el("btn-history").onclick = openHistory;
el("btn-coverage").onclick = openCoverage;
el("btn-collapse").onclick = () => {
  state.drawerOpen = false;
  renderDrawer();
};
el("drawer-handle").onclick = () => {
  state.drawerOpen = true;
  renderDrawer();
};
el("view-current").onclick = () => {
  state.drawerView = "current";
  renderDrawer();
};
el("view-history").onclick = () => {
  state.drawerView = "history";
  renderDrawer();
  loadHistory();
};
el("view-coverage").onclick = () => {
  state.drawerView = "coverage";
  renderDrawer();
  loadCoverage();
};

el("history-list").addEventListener("click", (event) => {
  const item = event.target.closest("[data-history-id]");
  if (item) selectHistory(item.dataset.historyId);
});

el("coverage-list").addEventListener("click", (event) => {
  const runBtn = event.target.closest("[data-run-coverage]");
  if (runBtn) {
    runCoverage();
    return;
  }
  const item = event.target.closest("[data-cov-path]");
  if (item) selectCoverageFile(item.dataset.covPath);
});



/* ---------------- 复制事件 ---------------- */

el("btn-copy-run").onclick = (event) => {
  const run = currentState().run;
  if (run) copyText(run.output || "", event.currentTarget);
};

el("coverage-detail").addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  const detail = state.coverageDetail;
  if (!button || !detail) return;

  const kind = button.dataset.copy;
  let text = "";
  if (kind === "path") {
    text = detail.path;
  } else if (kind === "missing") {
    text = detail.missing_lines.join(", ");
  } else if (kind === "all") {
    // 带行号，方便贴到 issue / 文档里讨论
    text = detail.hunks
      .map((hunk) => hunk.lines.map((line) => `${line.no}\t${line.code}`).join("\n"))
      .join("\n\n");
  } else if (kind.startsWith("hunk:")) {
    // 不带行号，可以直接粘到编辑器
    const hunk = detail.hunks[Number(kind.split(":")[1])];
    if (hunk) text = hunk.lines.map((line) => line.code).join("\n");
  }
  if (text) copyText(text, button);
});

el("history-detail").addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  const entry = state.historyDetail;
  if (!button || !entry) return;
  let text;
  if (button.dataset.copy === "history-scripts") {
    text = entry.scripts.join("\n");
  } else if (button.dataset.copy === "failures") {
    text = failureText(entry.results);
  } else {
    text = entry.output || "";
  }
  if (text) copyText(text, button);
});

/* ---------------- 配置来源 ---------------- */

/* 标题旁的小胶囊：这份面板读的是哪个配置。
   在仓库根跑 dev 会读到根的 testlens.toml（几个包都在里面），
   和应用自己的 testlens_config.py 不是同一份——包数量对不上时先看这里。 */
const CONFIG = DATA.config || {};
const configChip = el("config-source");
configChip.textContent = CONFIG.path
  ? `配置 ${CONFIG.path.split(/[\\/]/).pop()}`
  : "配置 无文件";
configChip.title = [
  CONFIG.path ? `配置：${CONFIG.path}` : "没有配置文件：包定义写在代码里（或由目录推断）",
  `仓库：${CONFIG.repo_root || "—"}`,
  `包：${CONFIG.package_count || DATA.packages.length} 个`,
  CONFIG.path ? "点击复制配置路径" : "",
].filter(Boolean).join("\n");
if (CONFIG.path) {
  // 可点 = 可聚焦：复制路径不该只有鼠标能用
  configChip.classList.add("clickable");
  configChip.setAttribute("role", "button");
  configChip.tabIndex = 0;
  const copyPath = () => copyText(CONFIG.path, configChip);
  configChip.onclick = copyPath;
  configChip.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      copyPath();
    }
  };
}

el("sub").textContent =
  `${DATA.changed} 个更新 · 扫描于 ${DATA.scanned_at.replace("T", " ").slice(0, 19)}`;

/* ---------------- 静态模式降级 ---------------- */

if (!RUN_ENABLED) {
  const notice = el("notice");
  notice.hidden = false;
  notice.innerHTML =
    "当前是静态页面（<code>file://</code>），浏览器不允许它执行本地命令。" +
    "启动面板服务后从 <code>http://127.0.0.1:8787</code> 打开，就能直接在这里运行测试：<code>testlens serve</code>";
  el("btn-run").title = "需要先启动 testlens serve";
  el("btn-confirm").title = "需要先启动 testlens serve";
  el("btn-history").disabled = true;
  el("btn-history").title = "运行历史在服务端，需要先启动 testlens serve";
  el("btn-coverage").disabled = true;
  el("btn-coverage").title = "覆盖率数据在服务端，需要先启动 testlens serve";
}

el("search").value = state.query;
render();
loadHistory();
prefetchCoverage();

// URL 直接指定了视图时，把对应数据拉一次（抽屉已经在 render 里打开了）
if (state.drawerView === "coverage") {
  loadCoverage().then(() => {
    if (requestedCovFile) selectCoverageFile(requestedCovFile);
  });
}
if (state.drawerView === "history") loadHistory(true);
