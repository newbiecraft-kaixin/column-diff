(() => {
  "use strict";

  const ALLOWED = [".xlsx", ".xlsm", ".csv"];

  const state = {
    threshold: 0.8,
    gpu: false,
    device: "",
    files: [],
    tasks: [],
    activeTaskId: null,
    uploads: [],
  };

  // 比对结果只对当前任务有效，任务/列一变动就作废
  let compareState = { taskId: null, data: null, loading: false, error: "" };
  // 已比对过的任务结果缓存，重复点开直接复用，避免重新请求导致闪烁
  const compareCache = new Map();

  const els = {
    taskList: document.getElementById("taskList"),
    tasksEmpty: document.getElementById("tasksEmpty"),
    taskCount: document.getElementById("taskCount"),
    fileList: document.getElementById("fileList"),
    filesEmpty: document.getElementById("filesEmpty"),
    fileCount: document.getElementById("fileCount"),
    detailBody: document.getElementById("detailBody"),
    newTaskBtn: document.getElementById("newTaskBtn"),
    autoGroupBtn: document.getElementById("autoGroupBtn"),
    clearAllBtn: document.getElementById("clearAllBtn"),
    clearTasksBtn: document.getElementById("clearTasksBtn"),
    confirmDlg: document.getElementById("confirmDlg"),
    pickerTitle: document.getElementById("pickerTitle"),
    pickerDesc: document.getElementById("pickerDesc"),
    sidebarToggle: document.getElementById("sidebarToggle"),
    sidebarBtn: document.getElementById("sidebarBtn"),
    aiBtn: document.getElementById("aiBtn"),
    aiDialog: document.getElementById("aiDialog"),
    aiProvider: document.getElementById("aiProvider"),
    aiKey: document.getElementById("aiKey"),
    aiKeyHint: document.getElementById("aiKeyHint"),
    aiModel: document.getElementById("aiModel"),
    aiBaseUrl: document.getElementById("aiBaseUrl"),
    aiStatus: document.getElementById("aiStatus"),
    aiTestBtn: document.getElementById("aiTestBtn"),
    aiSaveBtn: document.getElementById("aiSaveBtn"),
    aiClose: document.getElementById("aiClose"),
    emptyCreateBtn: document.getElementById("emptyCreateBtn"),
    pickBtn: document.getElementById("pickBtn"),
    fileInput: document.getElementById("fileInput"),
    thresholdInput: document.getElementById("thresholdInput"),
    thresholdValue: document.getElementById("thresholdValue"),
    deviceTag: document.getElementById("deviceTag"),
    statusText: document.getElementById("statusText"),
    statusDot: document.getElementById("statusDot"),
    picker: document.getElementById("picker"),
    pickerBody: document.getElementById("pickerBody"),
    pickerThreshold: document.getElementById("pickerThreshold"),
    pickerClose: document.getElementById("pickerClose"),
    pickerCount: document.getElementById("pickerCount"),
    pickerClear: document.getElementById("pickerClear"),
    pickerCreate: document.getElementById("pickerCreate"),
    dropmask: document.getElementById("dropmask"),
    toast: document.getElementById("toast"),
  };

  /* ---------- 工具 ---------- */

  const pad2 = (n) => String(n).padStart(2, "0");

  const esc = (value) =>
    String(value).replace(
      /[&<>"']/g,
      (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
    );

  const rowsOf = (file) => file.sheets.reduce((sum, sheet) => sum + sheet.rows, 0);

  const suffixOf = (name) => (name.split(".").pop() || "").toUpperCase();

  function fmtSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  let toastTimer = null;
  function toast(message) {
    els.toast.textContent = message;
    els.toast.classList.add("is-open");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove("is-open"), 3400);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  }

  const jsonOptions = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  /* ---------- 状态同步 ---------- */

  async function refresh() {
    const data = await api("/api/state");
    state.threshold = data.threshold;
    state.gpu = data.gpu;
    state.device = data.device;
    state.files = data.files;
    state.tasks = data.tasks;
    compareCache.clear();
    compareState = { taskId: compareState.taskId, data: null, loading: false, error: "" };

    if (!state.tasks.some((task) => task.id === state.activeTaskId)) {
      state.activeTaskId = state.tasks.length ? state.tasks[0].id : null;
    }
    render();

    const active = state.tasks.find((task) => task.id === state.activeTaskId);
    if (active) runCompare(active);
  }

  function render() {
    renderHeader();
    renderTasks();
    renderFiles();
    renderDetail();
    renderStatus();
  }

  function renderHeader() {
    if (document.activeElement !== els.thresholdInput) {
      const value = Math.round(state.threshold * 100);
      els.thresholdInput.value = String(value);
      els.thresholdValue.textContent = `${value}%`;
    }
    els.deviceTag.textContent = state.gpu ? "GPU" : "CPU";
    els.deviceTag.classList.toggle("is-gpu", state.gpu);
    els.deviceTag.title = state.device;
  }

  /* ---------- 左侧任务 ---------- */

  function renderTasks() {
    els.taskList.innerHTML = state.tasks
      .map((task, index) => {
        const active = task.id === state.activeTaskId ? "is-active" : "";
        const fileCount = new Set(task.columns.map((col) => col.file_id)).size;
        const failed = task.status === "failed";
        const pending = task.status === "pending";
        const flag = pending ? "🟡" : failed ? "❌" : "✅";
        const flagTitle = pending
          ? task.status_hint || "只有 1 列，待配对"
          : failed
            ? task.status_hint || "比对失败"
            : "比对可以执行";
        return `
          <li class="task-item ${active}" data-id="${task.id}" title="${esc(task.name)}">
            <span class="idx">${pad2(index + 1)}</span>
            <div class="task-main">
              <span class="task-name"><span class="task-flag" title="${esc(flagTitle)}">${flag}</span>${esc(task.name)}</span>
              <span class="task-meta">${fileCount} 个文件 · ${task.columns.length} 列${
                task.diff_rows == null ? "" : ` · 差 ${task.diff_rows} 行`
              }</span>
              <span class="task-report is-${task.report.state}" title="${esc(task.report.reason)}">${esc(
                task.report.reason
              )}</span>
            </div>
            <button class="task-del" type="button" title="删除任务">×</button>
          </li>`;
      })
      .join("");

    els.tasksEmpty.hidden = state.tasks.length > 0;
    els.taskCount.textContent = pad2(state.tasks.length);
  }

  function headerOptions(current) {
    const max = Math.max(8, current || 1);
    const options = [`<option value="0"${current ? "" : " selected"}>自动</option>`];
    for (let row = 1; row <= max; row += 1) {
      options.push(`<option value="${row}"${row === current ? " selected" : ""}>第 ${row} 行</option>`);
    }
    return options.join("");
  }

  function renderFiles() {
    const items = state.files.map(
      (file) => `
      <li class="file-item" data-id="${file.id}" title="${esc(file.name)}">
        <div>
          <span class="file-name">${esc(file.name)}</span>
          <span class="file-meta">${esc(suffixOf(file.name))} · ${file.sheets.length} 页 · ${rowsOf(file)} 行 · ${fmtSize(file.size)}</span>
          <label class="header-pick">表头
            <select class="header-select" data-id="${file.id}">${headerOptions(file.header_row)}</select>
          </label>
        </div>
        <button class="file-del" type="button" title="移除文件">×</button>
      </li>`
    );

    state.uploads.forEach((upload) => {
      items.push(`
        <li class="file-item">
          <div>
            <span class="file-name">${esc(upload.name)}</span>
            <span class="file-meta">解析中…</span>
          </div>
          <span></span>
        </li>`);
    });

    els.fileList.innerHTML = items.join("");
    els.fileCount.textContent = pad2(state.files.length);
    els.filesEmpty.hidden = state.files.length + state.uploads.length > 0;
  }

  /* ---------- 右侧任务详情 ---------- */

  function renderDetail() {
    const task = state.tasks.find((item) => item.id === state.activeTaskId);

    if (!task) {
      els.detailBody.innerHTML = `
        <div class="detail-empty">
          <strong>尚未选择任务</strong>
          <p>点击「＋ 新建任务」，勾选一列或多列作为基准列。<br>相似度达到阈值的列会自动归入同一任务。</p>
          <button class="ghost-btn" id="detailCreateBtn" type="button">＋ 新建任务</button>
        </div>`;
      const button = document.getElementById("detailCreateBtn");
      if (button) button.addEventListener("click", () => openPicker());
      return;
    }

    const fileCount = new Set(task.columns.map((col) => col.file_id)).size;
    const pinned = new Set(task.pinned || []);
    const rows = task.columns
      .map((col) => {
        const isSeed = col.uid === task.seed_uid;
        const percent = col.similarity * 100;
        const badges = [
          isSeed ? '<span class="badge">基准</span>' : "",
          pinned.has(col.uid) ? '<span class="badge badge-soft">手动</span>' : "",
        ].join("");
        return `
          <tr data-uid="${col.uid}">
            <td class="cell-file" title="${esc(col.file_name)} · 双击更换">${esc(col.file_name)}</td>
            <td class="cell-column" title="双击更换列">
              ${esc(`${col.sheet_index}-${col.column}`)}${badges}
              <span class="file-meta">第 ${col.sheet_index} 页 · ${esc(col.sheet_name)} · ${col.rows} 行</span>
            </td>
            <td>
              <span class="sim ${percent >= 99 ? "is-high" : ""}">${percent.toFixed(1)}%</span>
              <div class="sim-bar"><span style="width:${percent}%"></span></div>
            </td>
            <td class="cell-act">
              <button class="mini-btn" type="button" data-act="remove">移除</button>
            </td>
          </tr>`;
      })
      .join("");

    els.detailBody.innerHTML = `
      <div class="task-head">
        <div class="task-title">
          <label for="taskNameInput">任务名称</label>
          <input class="task-name-input" id="taskNameInput" value="${esc(task.name)}" maxlength="80">
          <p class="task-sub">${fileCount} 个文件 · ${task.columns.length} 列 · 阈值 ${Math.round(task.threshold * 100)}% · ${state.gpu ? "GPU" : "CPU"} 加速</p>
        </div>
        <div class="task-head-actions">
          <button class="ghost-btn ghost-btn-sm" id="reselectBtn" type="button" title="重新勾选本任务参与比对的列">重新选列</button>
          <button class="danger-btn" id="deleteTaskBtn" type="button">删除任务</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr><th>文件</th><th>列（页码-列名）</th><th>相似度</th><th></th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
        <div class="table-note">改名后立即保存；双击文件或列可原地更换（不删任务）；移除列只影响本任务。</div>
      </div>
      <div class="compare-panel">
        <div class="compare-actions">
          <button class="run-btn" id="runCompareBtn" type="button">执行比对</button>
          <button class="ghost-btn ghost-btn-sm" id="aiAnalyzeBtn" type="button" title="用配置好的大模型解读本次差异">AI 分析</button>
          <button class="ghost-btn ghost-btn-sm" id="exportReportBtn" type="button" title="导出单文件 HTML 报告，可直接打印为 PDF">导出报告</button>
          <span class="compare-hint">按行对齐 · 数据从第 2 行开始</span>
        </div>
        <div id="aiPanel"></div>
        <div id="diffArea"></div>
      </div>`;

    const nameInput = document.getElementById("taskNameInput");
    nameInput.addEventListener("change", () => {
      const name = nameInput.value.trim();
      if (!name || name === task.name) {
        nameInput.value = task.name;
        return;
      }
      renameTask(task.id, name);
    });

    document.getElementById("deleteTaskBtn").addEventListener("click", () => deleteTask(task));
    document.getElementById("reselectBtn").addEventListener("click", () => openPicker(task));
    const tbody = document.querySelector(".table-wrap tbody");
    tbody.addEventListener("click", (event) => {
      const button = event.target.closest('[data-act="remove"]');
      if (button) removeTaskColumn(task.id, button.closest("tr").dataset.uid);
    });
    tbody.addEventListener("dblclick", (event) => {
      const cell = event.target.closest("td.cell-file, td.cell-column");
      if (!cell) return;
      const row = cell.closest("tr");
      if (!row) return;
      openColumnMenu(cell, task, row.dataset.uid);
    });

    document.getElementById("runCompareBtn").addEventListener("click", () => runCompare(task));
    document.getElementById("aiAnalyzeBtn").addEventListener("click", () => generateAiReport(task));
    document.getElementById("exportReportBtn").addEventListener("click", () => exportReport(task));
    renderAiPanel(task);
    renderDiffArea();
  }

  function renderStatus() {
    let text = "等待导入文件";
    if (state.uploads.length) {
      text = `正在解析 ${state.uploads.length} 个文件…`;
    } else if (state.files.length) {
      const columns = state.tasks.reduce((sum, task) => sum + task.columns.length, 0);
      text = `${state.files.length} 个文件 · ${state.tasks.length} 个任务 · ${columns} 列`;
    }
    els.statusText.textContent = text;
    els.statusDot.classList.toggle("is-live", state.files.length > 0);
  }

  /* ---------- 任务操作 ---------- */

  async function createTasks(seeds) {
    try {
      els.picker.close();
      picked = [];
      const result = await api(
        "/api/tasks/batch",
        jsonOptions("POST", {
          columns: seeds.map(({ file_id, sheet_index, column }) => ({
            file_id,
            sheet_index,
            column,
          })),
        })
      );
      if (result.count) state.activeTaskId = result.created[0].id;
      await refresh();
      const skipped = result.skipped ? `，跳过 ${result.skipped} 个已在任务中的列` : "";
      toast(
        result.count
          ? `已创建 ${result.count} 个任务${skipped}`
          : `没有新建任务${skipped || "，请重新选择"}`
      );
    } catch (error) {
      toast(error.message);
    }
  }

  function askConfirm({ title, message, confirmText = "确定", danger = false, alertOnly = false }) {
    const dlg = els.confirmDlg;
    const ok = document.getElementById("confirmOk");
    const cancel = document.getElementById("confirmCancel");
    document.getElementById("confirmTitle").textContent = title;
    document.getElementById("confirmMsg").textContent = message;
    ok.textContent = alertOnly ? "知道了" : confirmText;
    ok.className = danger ? "danger-btn" : "run-btn";
    cancel.hidden = alertOnly;
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        ok.removeEventListener("click", onOk);
        cancel.removeEventListener("click", onCancel);
        dlg.removeEventListener("close", onClose);
        cancel.hidden = false;
        if (dlg.open) dlg.close();
        resolve(value);
      };
      const onOk = () => finish(true);
      const onCancel = () => finish(false);
      const onClose = () => finish(false);
      ok.addEventListener("click", onOk);
      cancel.addEventListener("click", onCancel);
      dlg.addEventListener("close", onClose);
      dlg.showModal();
    });
  }

  async function autoGroup() {
    if (!state.files.length) {
      toast("请先导入文件");
      return;
    }
    const percent = Math.round(state.threshold * 100);
    const go = await askConfirm({
      title: "自动分组并比对",
      message: `按当前阈值 ${percent}% 自动分组：相似列并入同一任务，其余列各自建任务，然后自动生成比对报告。`,
      confirmText: "开始分组",
    });
    if (!go) return;
    const originalText = els.autoGroupBtn.textContent;
    els.autoGroupBtn.disabled = true;
    els.autoGroupBtn.textContent = "分组中…";
    toast("正在计算所有列的相似度，文件或列较多时请稍候…");
    try {
      const result = await api("/api/auto-group", { method: "POST" });
      if (result.count) state.activeTaskId = result.created[0].id;
      await refresh();
      if (result.count) {
        const failed = state.tasks.filter((task) => task.status === "failed").length;
        const active = state.tasks.find((task) => task.id === state.activeTaskId);
        if (active) await runCompare(active);
        const skippedItems = (result.skipped_items || [])
          .slice(0, 5)
          .map((item) => `${item.name}（${item.reason}）`)
          .join("、");
        await askConfirm({
          alertOnly: true,
          title: "自动分组完成",
          message:
            `按阈值 ${Math.round(result.threshold * 100)}% 分成 ${result.count} 个任务` +
            `（共 ${result.columns} 列，${result.skipped} 列已跳过），` +
            `累计 ${result.diffs} 处差异` +
            (failed ? `；${failed} 个任务无法比对，见左侧 ❌ 与原因说明。` : "；全部可比对。") +
            (skippedItems
              ? ` 跳过的列：${skippedItems}${result.skipped > 5 ? " 等" : ""}。`
              : ""),
        });
      } else {
        await askConfirm({
          alertOnly: true,
          title: "没有找到可自动归组的列",
          message:
            `共 ${result.columns} 列，在当前阈值 ${percent}% 下没有任何两列足够相似` +
            `（例如单个文件里各页列名、内容都不同）。` +
            `可以调低顶部相似阈值后重试，或用「＋ 新建任务」手动勾选多列。`,
        });
      }
    } catch (error) {
      toast(error.message);
    } finally {
      els.autoGroupBtn.disabled = false;
      els.autoGroupBtn.textContent = originalText;
    }
  }

  async function renameTask(taskId, name) {
    try {
      await api(`/api/tasks/${taskId}`, jsonOptions("PATCH", { name }));
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  }

  async function deleteTask(task) {
    const go = await askConfirm({
      title: "删除任务",
      message: `确定删除任务「${task.name}」？文件会保留。`,
      confirmText: "删除",
      danger: true,
    });
    if (!go) return;
    try {
      await api(`/api/tasks/${task.id}`, { method: "DELETE" });
      if (state.activeTaskId === task.id) state.activeTaskId = null;
      await refresh();
      toast("任务已删除");
    } catch (error) {
      toast(error.message);
    }
  }

  async function removeTaskColumn(taskId, uid) {
    try {
      await api(`/api/tasks/${taskId}/columns/${uid}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  }

  async function replaceTaskColumn(taskId, uid, newUid) {
    try {
      await api(`/api/tasks/${taskId}/columns/${uid}`, jsonOptions("PATCH", { uid: newUid }));
      await refresh();
      toast("已更换列");
    } catch (error) {
      toast(error.message);
    }
  }

  /* ---------- 执行比对与差异表 ---------- */

  async function runCompare(task) {
    const cached = compareCache.get(task.id);
    if (cached) {
      // 再次点开同一任务直接复用结果，不重新请求、不闪
      compareState = { taskId: task.id, data: cached, loading: false, error: "" };
      renderDetail();
      return;
    }

    compareState = { taskId: task.id, data: null, loading: true, error: "" };
    renderDiffArea();
    try {
      const data = await api(`/api/tasks/${task.id}/compare`);
      compareCache.set(task.id, data);
      compareState = { taskId: task.id, data, loading: false, error: "" };
      const item = state.tasks.find((entry) => entry.id === task.id);
      if (item) item.diff_rows = data.diffs.length;
      updateTaskMeta(task.id);
    } catch (error) {
      compareState = { taskId: task.id, data: null, loading: false, error: error.message };
    }
    renderDiffArea();
  }

  function updateTaskMeta(taskId) {
    const task = state.tasks.find((item) => item.id === taskId);
    const node = els.taskList.querySelector(`.task-item[data-id="${taskId}"] .task-meta`);
    if (!task || !node) return;
    const fileCount = new Set(task.columns.map((col) => col.file_id)).size;
    node.textContent = `${fileCount} 个文件 · ${task.columns.length} 列${
      task.diff_rows == null ? "" : ` · 差 ${task.diff_rows} 行`
    }`;
  }

  async function exportReport(task) {
    try {
      const response = await fetch(`/api/tasks/${task.id}/report.html`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ai: task.ai_report || null }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || `导出失败（${response.status}）`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${task.name}-比对报告.html`;
      link.click();
      URL.revokeObjectURL(url);
      toast("已导出 HTML 报告，可用浏览器打开后打印为 PDF");
    } catch (error) {
      toast(error.message);
    }
  }

  /* ---------- AI ---------- */

  function mdToHtml(markdown) {
    const inline = (text) =>
      text
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    const lines = markdown.split(/\r?\n/);
    const out = [];
    let listTag = null;
    let inCode = false;
    const closeList = () => {
      if (listTag) {
        out.push(`</${listTag}>`);
        listTag = null;
      }
    };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (line.trim().startsWith("```")) {
        closeList();
        out.push(inCode ? "</code></pre>" : "<pre><code>");
        inCode = !inCode;
        continue;
      }
      if (inCode) {
        out.push(esc(line));
        continue;
      }
      if (!line.trim()) {
        closeList();
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        closeList();
        const level = heading[1].length;
        out.push(`<h${level}>${inline(esc(heading[2]))}</h${level}>`);
        continue;
      }
      const bullet = line.match(/^\s*[-*]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (bullet || numbered) {
        const tag = bullet ? "ul" : "ol";
        if (listTag !== tag) {
          closeList();
          out.push(`<${tag}>`);
          listTag = tag;
        }
        out.push(`<li>${inline(esc((bullet || numbered)[1]))}</li>`);
        continue;
      }
      closeList();
      out.push(`<p>${inline(esc(line))}</p>`);
    }
    closeList();
    if (inCode) out.push("</code></pre>");
    return out.join("\n");
  }

  function renderAiPanel(task) {
    const panel = document.getElementById("aiPanel");
    if (!panel) return;
    const ai = task.ai_report;
    if (!ai || !ai.markdown) {
      panel.innerHTML = "";
      return;
    }
    panel.innerHTML = `
      <section class="ai-card">
        <header class="ai-card-head">
          <h3>AI 分析</h3>
          <span class="ai-meta">${esc(ai.model || "")} · ${esc(ai.time || "")}</span>
          <div class="ai-card-actions">
            <button class="ghost-btn ghost-btn-sm" type="button" data-ai="copy">复制</button>
            <button class="ghost-btn ghost-btn-sm" type="button" data-ai="regen">重新生成</button>
          </div>
        </header>
        <div class="ai-body">${mdToHtml(ai.markdown)}</div>
        <p class="ai-note">AI 生成内容仅供参考，请人工复核。</p>
      </section>`;
    panel.querySelector('[data-ai="copy"]').addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(ai.markdown);
        toast("AI 分析已复制到剪贴板");
      } catch {
        toast("复制失败，请手动选中文本");
      }
    });
    panel.querySelector('[data-ai="regen"]').addEventListener("click", () => generateAiReport(task));
  }

  async function ensureAiConfigured() {
    const data = await api("/api/ai");
    state.aiSettings = data;
    return data.configured;
  }

  async function generateAiReport(task) {
    const button = document.getElementById("aiAnalyzeBtn");
    try {
      if (!(await ensureAiConfigured())) {
        toast("请先配置 AI 厂商和 API Key");
        openAiDialog();
        return;
      }
      if (button) {
        button.disabled = true;
        button.textContent = "分析中…";
      }
      const data = await api(`/api/tasks/${task.id}/ai-report`, { method: "POST" });
      task.ai_report = data;
      renderAiPanel(task);
      toast("AI 分析完成");
    } catch (error) {
      toast(error.message);
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "AI 分析";
      }
    }
  }

  function fillAiForm(data) {
    els.aiProvider.innerHTML = data.options
      .map((item) => `<option value="${esc(item.value)}">${esc(item.label)}</option>`)
      .join("");
    els.aiProvider.value = data.provider || "deepseek";
    const current =
      data.options.find((item) => item.value === els.aiProvider.value) || data.options[0];
    els.aiBaseUrl.value = data.base_url || current.base_url;
    els.aiModel.value = data.model || current.model;
    els.aiKey.value = "";
    els.aiKey.placeholder = data.has_key
      ? `已保存 ${data.key_hint}（留空保持不变）`
      : "sk-…";
    els.aiKeyHint.textContent = data.has_key ? `已保存 ${data.key_hint}` : "只保存在本机服务内存";
    els.aiStatus.textContent = data.configured ? "已配置" : "未配置";
  }

  async function openAiDialog() {
    try {
      fillAiForm(await api("/api/ai"));
      els.aiDialog.showModal();
    } catch (error) {
      toast(error.message);
    }
  }

  async function saveAiForm({ silent = false } = {}) {
    const payload = {
      provider: els.aiProvider.value,
      model: els.aiModel.value.trim(),
      base_url: els.aiBaseUrl.value.trim(),
    };
    if (els.aiKey.value.trim()) payload.api_key = els.aiKey.value.trim();
    const data = await api("/api/ai", jsonOptions("PUT", payload));
    state.aiSettings = data;
    fillAiForm(data);
    if (!silent) toast("AI 设置已保存");
    return data;
  }

  els.aiBtn.addEventListener("click", openAiDialog);
  els.aiClose.addEventListener("click", () => els.aiDialog.close());
  els.aiProvider.addEventListener("change", () => {
    const option = (state.aiSettings?.options || []).find(
      (item) => item.value === els.aiProvider.value
    );
    if (option) {
      els.aiBaseUrl.value = option.base_url;
      els.aiModel.value = option.model;
    }
  });
  els.aiSaveBtn.addEventListener("click", async () => {
    try {
      await saveAiForm();
      els.aiDialog.close();
    } catch (error) {
      toast(error.message);
    }
  });
  els.aiTestBtn.addEventListener("click", async () => {
    try {
      els.aiStatus.textContent = "测试中…";
      await saveAiForm({ silent: true });
      const result = await api("/api/ai/test", { method: "POST" });
      els.aiStatus.textContent = `连接成功 · ${result.model}`;
      toast(`连接成功：${result.model}`);
    } catch (error) {
      els.aiStatus.textContent = "连接失败";
      toast(error.message);
    }
  });

  function renderDiffArea() {
    const area = document.getElementById("diffArea");
    if (!area) return;

    if (compareState.taskId !== state.activeTaskId) {
      area.innerHTML = "";
      return;
    }
    if (compareState.loading) {
      area.innerHTML = '<div class="diff-empty">正在比对…</div>';
      return;
    }
    if (compareState.error) {
      area.innerHTML = `<div class="diff-empty">${esc(compareState.error)}</div>`;
      return;
    }
    const data = compareState.data;
    if (!data) {
      area.innerHTML = "";
      return;
    }
    const diffCount = data.diffs.length;
    const rate = data.rows ? ((data.rows - diffCount) / data.rows) * 100 : 100;
    let reason = `全部 ${data.rows} 行一致，没有差异`;
    if (diffCount) {
      const numbers = data.diffs.slice(0, 8).map((diff) => diff.row);
      const columns = [
        ...new Set(
          data.diffs.flatMap((diff) =>
            diff.differ.map((label) => {
              const [file, column] = label.split("｜");
              return `${file.replace(/\.[^.]+$/, "")}·${column}`;
            })
          )
        ),
      ];
      reason =
        `第 ${numbers.join("、")}${diffCount > numbers.length ? " 等" : ""} 行不一致` +
        (columns.length ? `，涉及列 ${columns.join("、")}` : "");
    }
    const card = `
      <div class="report-card">
        <div class="report-item"><span>参与列</span><strong>${data.columns.length}</strong></div>
        <div class="report-item"><span>数据行</span><strong>${data.rows}</strong></div>
        <div class="report-item ${diffCount ? "is-warn" : "is-ok"}"><span>差异行</span><strong>${diffCount}</strong></div>
        <div class="report-item ${diffCount ? "" : "is-ok"}"><span>一致率</span><strong>${rate.toFixed(1)}<em>%</em></strong></div>
        <div class="report-reason">${esc(reason)}</div>
      </div>`;
    if (!diffCount) {
      area.innerHTML = `${card}<div class="diff-empty is-ok">共 ${data.rows} 行，全部一致，没有差异。可点「导出报告」保存。</div>`;
      return;
    }

    const labels = data.columns.map((col) => col.label);
    const head = labels.map((label) => `<th>${esc(label)}</th>`).join("");
    const body = data.diffs
      .map((diff) => {
        const cells = labels
          .map((label) => {
            const value = diff.values[label];
            const text = value === null ? "<无数据>" : value === "" ? "<空>" : value;
            const cls = diff.differ.includes(label) ? "is-diff" : "";
            return `<td class="${cls}">${esc(text)}</td>`;
          })
          .join("");
        const differInline = diff.differ.map((label) => label.split("｜").pop()).join("、");
        const differData = esc(JSON.stringify(diff.differ));
        return `<tr><td class="diff-row">${diff.row}</td>${cells}<td class="diff-col"><span class="differ-inline">${esc(
          differInline
        )}</span><button class="differ-toggle" type="button" data-labels="${differData}">${
          diff.differ.length
        } 列 ▾</button></td></tr>`;
      })
      .join("");

    const summary = data.per_column
      .map((item) => `${item.label.split("｜").pop()}（${item.diffs} 行）`)
      .join(" · ");

    area.innerHTML = `${card}
      <div class="diff-head">
        <strong>差异 ${data.diffs.length} 行</strong>
        <span>共 ${data.rows} 行数据 · ${esc(summary)}</span>
      </div>
      <div class="diff-wrap">
        <table class="data diff-table">
          <thead><tr><th>行号</th>${head}<th>不同列</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>`;
    fitDifferCells(area);
  }

  /* ---------- 不同列：放不下时折叠成下拉 ---------- */

  function fitDifferCells(area) {
    area.querySelectorAll(".diff-col").forEach((cell) => {
      const inline = cell.querySelector(".differ-inline");
      if (!inline) return;
      cell.classList.toggle("is-overflow", inline.scrollWidth > inline.clientWidth + 1);
    });
  }

  let differMenu = null;

  function closeDifferMenu() {
    if (!differMenu) return;
    differMenu.remove();
    differMenu = null;
  }

  function openDifferMenu(button) {
    closeDifferMenu();
    let labels = [];
    try {
      labels = JSON.parse(button.dataset.labels || "[]");
    } catch (error) {
      labels = [];
    }
    const menu = document.createElement("div");
    menu.className = "col-menu differ-menu";
    menu.innerHTML =
      `<div class="col-menu-head">不同列 · ${labels.length}</div>` +
      labels
        .map((label) => {
          const [file, column] = label.split("｜");
          return `<div class="differ-opt"><span>${esc(
            file.replace(/\.[^.]+$/, "")
          )}</span><strong>${esc(column)}</strong></div>`;
        })
        .join("");
    document.body.appendChild(menu);
    const rect = button.getBoundingClientRect();
    const width = menu.offsetWidth;
    menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
    menu.style.top = `${rect.bottom + 6}px`;
    differMenu = menu;
  }

  document.addEventListener("click", (event) => {
    const toggle = event.target.closest(".differ-toggle");
    if (toggle) {
      event.stopPropagation();
      if (differMenu && differMenu.dataset.for === toggle.dataset.labels) {
        closeDifferMenu();
        return;
      }
      openDifferMenu(toggle);
      if (differMenu) differMenu.dataset.for = toggle.dataset.labels;
      return;
    }
    if (differMenu && !differMenu.contains(event.target)) closeDifferMenu();
  });

  window.addEventListener("scroll", closeDifferMenu, true);
  window.addEventListener("resize", () => {
    closeDifferMenu();
    const area = document.getElementById("diffArea");
    if (area) fitDifferCells(area);
  });

  /* ---------- 原地换列下拉 ---------- */

  let colMenu = null;

  function closeColumnMenu() {
    if (!colMenu) return;
    colMenu.remove();
    colMenu = null;
    document.removeEventListener("mousedown", onMenuOutside, true);
    window.removeEventListener("scroll", closeColumnMenu, true);
  }

  function onMenuOutside(event) {
    if (colMenu && !colMenu.contains(event.target)) closeColumnMenu();
  }

  function columnOptions(task) {
    const used = new Set(task.columns.map((col) => col.uid));
    const options = [];
    state.files.forEach((file) => {
      file.sheets.forEach((sheet) => {
        sheet.columns.forEach((column, index) => {
          const uid = `${file.id}-${sheet.index}-${index}`;
          options.push({
            uid,
            label: `${sheet.index}-${column}`,
            file: file.name,
            used: used.has(uid),
          });
        });
      });
    });
    return options;
  }

  function openColumnMenu(anchor, task, uid) {
    closeColumnMenu();
    const options = columnOptions(task);

    const menu = document.createElement("div");
    menu.className = "col-menu";
    menu.innerHTML = `
      <div class="col-menu-head">更换列 · 选一个</div>
      <input class="col-menu-search" type="search" placeholder="搜索列名 / 文件名">
      <div class="col-menu-list"></div>`;
    document.body.appendChild(menu);
    colMenu = menu;

    const list = menu.querySelector(".col-menu-list");
    const search = menu.querySelector(".col-menu-search");

    const paint = (keyword) => {
      const text = keyword.trim().toLowerCase();
      const rows = options.filter(
        (item) =>
          !text ||
          item.label.toLowerCase().includes(text) ||
          item.file.toLowerCase().includes(text)
      );
      list.innerHTML = rows.length
        ? rows
            .map(
              (item) => `
          <button class="col-opt ${item.uid === uid ? "is-current" : ""} ${item.used && item.uid !== uid ? "is-used" : ""}"
            type="button" data-uid="${item.uid}" ${item.used && item.uid !== uid ? "disabled" : ""}>
            <span class="col-opt-main">${esc(item.label)}</span>
            <span class="col-opt-file">${esc(item.file)}${item.used && item.uid !== uid ? " · 已在本任务" : ""}</span>
          </button>`
            )
            .join("")
        : '<p class="col-menu-tip">没有匹配的列</p>';
    };
    paint("");

    search.addEventListener("input", () => paint(search.value));
    list.addEventListener("click", (event) => {
      const button = event.target.closest(".col-opt");
      if (!button || button.disabled) return;
      const nextUid = button.dataset.uid;
      closeColumnMenu();
      if (nextUid !== uid) replaceTaskColumn(task.id, uid, nextUid);
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeColumnMenu();
    });

    const width = 320;
    const rect = anchor.getBoundingClientRect();
    menu.style.width = `${width}px`;
    menu.style.left = `${Math.min(Math.max(8, rect.left), window.innerWidth - width - 8)}px`;
    menu.style.top = `${rect.bottom + 8}px`;
    const menuRect = menu.getBoundingClientRect();
    if (menuRect.bottom > window.innerHeight - 8) {
      menu.style.top = `${Math.max(8, rect.top - menuRect.height - 8)}px`;
    }

    search.focus();
    setTimeout(() => document.addEventListener("mousedown", onMenuOutside, true), 0);
    window.addEventListener("scroll", closeColumnMenu, true);
  }

  /* ---------- 列选择弹窗 ---------- */

  let picked = []; // 本次勾选的列
  let editingTask = null; // 非空表示正在重新选列

  function paintPickerSelection() {
    els.pickerCount.textContent = `已选 ${picked.length} 列`;
    els.pickerCreate.disabled = picked.length === 0;
    els.pickerBody.querySelectorAll(".chip").forEach((chip) => {
      chip.classList.toggle(
        "is-selected",
        picked.some((item) => item.uid === chip.dataset.uid)
      );
    });
  }

  function togglePick(chip) {
    const uid = chip.dataset.uid;
    const index = picked.findIndex((item) => item.uid === uid);
    if (index >= 0) {
      picked.splice(index, 1);
    } else {
      picked.push({
        uid,
        file_id: chip.dataset.file,
        sheet_index: Number(chip.dataset.sheet),
        column: chip.dataset.column,
      });
    }
    paintPickerSelection();
  }

  function openPicker(task = null) {
    if (task && !Array.isArray(task.columns)) task = null; // 防止把点击事件当成任务传进来
    if (!state.files.length) {
      toast("请先导入文件");
      return;
    }
    editingTask = task || null;
    // 其它任务里的列：新建时不可选；重新选列时可勾选，保存后会从原任务移过来
    const used = new Set(
      state.tasks
        .filter((item) => !task || item.id !== task.id)
        .flatMap((item) => item.columns.map((col) => col.uid))
    );
    picked = task
      ? task.columns.map((col) => ({
          uid: col.uid,
          file_id: col.file_id,
          sheet_index: col.sheet_index,
          column: col.column,
        }))
      : [];

    els.pickerTitle.textContent = task ? "重新选择列" : "选择基准列";
    els.pickerDesc.innerHTML = task
      ? `勾选本任务要参与比对的列，取消勾选即移出；虚线列属于其它任务，勾选后会移入本任务（相似度阈值不参与，完全按你的选择）`
      : `可一次勾选多列，每个列生成一个任务（文件名-页码-列名）；相似度 ≥ <span id="pickerThreshold">${Math.round(
          state.threshold * 100
        )}%</span> 的列自动并入同一任务`;
    els.pickerThreshold = document.getElementById("pickerThreshold");
    if (els.pickerThreshold) {
      els.pickerThreshold.textContent = `${Math.round(state.threshold * 100)}%`;
    }
    els.pickerCreate.textContent = task ? "保存选择" : "创建任务";

    els.pickerBody.innerHTML = state.files
      .map((file) => {
        const sheets = file.sheets
          .map((sheet) => {
            const chips = sheet.columns
              .map((column, index) => {
                const uid = `${file.id}-${sheet.index}-${index}`;
                const elsewhere = used.has(uid);
                const disabled = elsewhere && !editingTask;
                const classes = elsewhere
                  ? editingTask
                    ? "chip is-elsewhere"
                    : "chip is-used"
                  : "chip";
                const hint = elsewhere
                  ? editingTask
                    ? "已在其它任务中，勾选后移入本任务"
                    : "已在某个任务中"
                  : "点击选择，可多选";
                return `<button class="${classes}" type="button"
                  data-uid="${uid}" data-file="${file.id}" data-sheet="${sheet.index}"
                  data-column="${esc(column)}" ${disabled ? "disabled" : ""}
                  title="${hint}">${sheet.index}-${esc(column)}</button>`;
              })
              .join("");
            return `<div class="pick-sheet"><span class="pick-sheet-label">第 ${sheet.index} 页</span>${chips || '<span class="pick-sheet-label">无列</span>'}</div>`;
          })
          .join("");
        return `<div class="pick-file"><h3>${esc(file.name)}<span>${file.sheets.length} 页</span></h3>${sheets}</div>`;
      })
      .join("");

    paintPickerSelection();
    els.picker.showModal();
  }

  async function saveTaskColumns(taskId, uids) {
    try {
      await api(`/api/tasks/${taskId}/columns`, jsonOptions("PUT", { uids }));
      els.picker.close();
      state.activeTaskId = taskId;
      await refresh();
      toast(`已更新任务：${uids.length} 列参与比对`);
    } catch (error) {
      toast(error.message);
    }
  }

  /* ---------- 上传 / 删除文件 ---------- */

  async function uploadFiles(fileList) {
    for (const file of fileList) {
      const suffix = `.${(file.name.split(".").pop() || "").toLowerCase()}`;
      if (!ALLOWED.includes(suffix)) {
        toast(`不支持的文件类型：${file.name}`);
        continue;
      }
      await uploadOne(file);
    }
  }

  async function uploadOne(file) {
    const pending = { name: file.name };
    state.uploads.push(pending);
    render();

    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch("/api/files", { method: "POST", body });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `上传失败（${response.status}）`);
    } catch (error) {
      toast(error.message);
    } finally {
      state.uploads = state.uploads.filter((item) => item !== pending);
      await refresh().catch((error) => toast(error.message));
    }
  }

  async function removeFile(fileId) {
    const file = state.files.find((item) => item.id === fileId);
    if (file && !window.confirm(`移除文件「${file.name}」？相关任务会同步清理。`)) return;
    try {
      await api(`/api/files/${fileId}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  }

  async function clearTasks() {
    if (!state.tasks.length) {
      toast("现在没有任务");
      return;
    }
    const go = await askConfirm({
      title: "清空任务",
      message: `清空全部 ${state.tasks.length} 个任务？已导入的文件会保留。`,
      confirmText: "清空任务",
      danger: true,
    });
    if (!go) return;
    try {
      await api("/api/tasks", { method: "DELETE" });
      state.activeTaskId = null;
      compareState = { taskId: null, data: null, loading: false, error: "" };
      await refresh();
      toast("已清空任务");
    } catch (error) {
      toast(error.message);
    }
  }

  async function clearAll() {
    if (!state.files.length && !state.tasks.length) {
      toast("现在就是空的");
      return;
    }
    const go = await askConfirm({
      title: "清空全部",
      message: "清空全部文件与任务？此操作不可撤销。",
      confirmText: "全部清空",
      danger: true,
    });
    if (!go) return;
    try {
      await api("/api/all", { method: "DELETE" });
      state.activeTaskId = null;
      compareState = { taskId: null, data: null, loading: false, error: "" };
      await refresh();
      toast("已清空");
    } catch (error) {
      toast(error.message);
    }
  }

  async function changeHeaderRow(fileId, headerRow) {
    const value = headerRow > 0 ? headerRow : null;
    try {
      await api(`/api/files/${fileId}`, jsonOptions("PATCH", { header_row: value }));
      await refresh();
      toast(value ? `已按第 ${value} 行作为表头重新解析` : "已恢复自动识别表头");
    } catch (error) {
      toast(error.message);
      await refresh();
    }
  }

  /* ---------- 事件绑定 ---------- */

  els.taskList.addEventListener("click", (event) => {
    const item = event.target.closest(".task-item");
    if (!item) return;
    const task = state.tasks.find((entry) => entry.id === item.dataset.id);
    if (!task) return;

    if (event.target.closest(".task-del")) {
      deleteTask(task);
      return;
    }

    state.activeTaskId = task.id;
    els.taskList.querySelectorAll(".task-item").forEach((node) => {
      node.classList.toggle("is-active", node.dataset.id === task.id);
    });
    renderDetail();
    if (compareState.taskId !== task.id) runCompare(task);
  });

  els.fileList.addEventListener("click", (event) => {
    const item = event.target.closest(".file-item");
    if (!item || !item.dataset.id) return;
    if (event.target.closest(".file-del")) removeFile(item.dataset.id);
  });

  els.fileList.addEventListener("change", (event) => {
    const select = event.target.closest(".header-select");
    if (!select) return;
    changeHeaderRow(select.dataset.id, Number(select.value));
  });

  els.pickerBody.addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip || chip.disabled) return;
    togglePick(chip);
  });

  els.pickerClear.addEventListener("click", () => {
    picked = [];
    paintPickerSelection();
  });

  els.pickerCreate.addEventListener("click", () => {
    if (!picked.length) return;
    if (editingTask) {
      saveTaskColumns(editingTask.id, picked.map((item) => item.uid));
    } else {
      createTasks([...picked]);
    }
  });

  els.pickerClose.addEventListener("click", () => els.picker.close());
  els.picker.addEventListener("close", () => {
    editingTask = null;
  });
  els.picker.addEventListener("click", (event) => {
    if (event.target === els.picker) els.picker.close();
  });

  [els.newTaskBtn, els.emptyCreateBtn].forEach((button) => {
    button.addEventListener("click", () => openPicker());
  });

  els.autoGroupBtn.addEventListener("click", autoGroup);

  function setSidebarCollapsed(collapsed) {
    document.body.classList.toggle("side-collapsed", collapsed);
    els.sidebarToggle.textContent = collapsed ? "⟩" : "⟨";
    els.sidebarToggle.title = collapsed ? "展开任务栏" : "收起任务栏";
    if (els.sidebarBtn) {
      els.sidebarBtn.textContent = collapsed ? "显示侧栏" : "隐藏侧栏";
      els.sidebarBtn.setAttribute("aria-pressed", collapsed ? "true" : "false");
    }
    localStorage.setItem("colcmp.sidebar", collapsed ? "1" : "0");
  }

  els.sidebarToggle.addEventListener("click", () => {
    setSidebarCollapsed(!document.body.classList.contains("side-collapsed"));
  });

  els.sidebarBtn.addEventListener("click", () => {
    setSidebarCollapsed(!document.body.classList.contains("side-collapsed"));
  });

  setSidebarCollapsed(localStorage.getItem("colcmp.sidebar") === "1");
  els.clearAllBtn.addEventListener("click", clearAll);
  els.clearTasksBtn.addEventListener("click", clearTasks);

  els.pickBtn.addEventListener("click", () => els.fileInput.click());

  els.fileInput.addEventListener("change", () => {
    uploadFiles([...els.fileInput.files]);
    els.fileInput.value = "";
  });

  let thresholdTimer = null;
  els.thresholdInput.addEventListener("input", () => {
    const value = Number(els.thresholdInput.value);
    els.thresholdValue.textContent = `${value}%`;
    if (els.picker.open) els.pickerThreshold.textContent = `${value}%`;
    clearTimeout(thresholdTimer);
    thresholdTimer = setTimeout(async () => {
      try {
        await api("/api/settings", jsonOptions("PATCH", { threshold: value / 100 }));
        await refresh();
      } catch (error) {
        toast(error.message);
      }
    }, 380);
  });

  /* ---------- 拖拽 ---------- */

  let dragDepth = 0;
  const hasFiles = (event) =>
    event.dataTransfer && [...event.dataTransfer.types].includes("Files");

  function hideMask() {
    dragDepth = 0;
    els.dropmask.classList.remove("is-open");
  }

  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    els.dropmask.classList.add("is-open");
  });

  window.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });

  window.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) hideMask();
  });

  window.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    hideMask();
    const files = [...event.dataTransfer.files];
    if (files.length) uploadFiles(files);
  });

  refresh().catch((error) => toast(error.message));
})();
