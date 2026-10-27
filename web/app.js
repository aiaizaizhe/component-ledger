/* 元件库 · 本地单机版前端（原生 JS，无依赖） */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const TYPE_LABEL = {
    new: '新增', in: '入库', out: '借出', return: '归还', adjust: '盘点',
    loss: '损耗', delete: '删除', revert: '撤回',
  };

  const state = {
    tab: 'stock',
    q: '',
    loc: 'all',
    cat: 'all',
    room: 'all',
    logFilter: 'all',
    components: [],
    loans: [],
    loanOpen: new Set(),
    locations: [],
    categories: [],
    rooms: [],
    batches: [],
    movements: [],
    losses: null,
    lossFilter: 'all',
    stats: {},
    meta: {},
    today: '',
    intake: { text: '', rows: [], batchName: '', locationId: '', roomId: '', categoryId: '', note: '' },
  };

  /* ------------------------------------------------------------------ */
  /* 小工具                                                              */
  /* ------------------------------------------------------------------ */
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const pad = (n) => String(n).padStart(2, '0');
  const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

  function nowLocalInput() {
    const d = new Date();
    return `${isoDate(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function addDays(n) {
    const d = new Date();
    d.setDate(d.getDate() + n);
    return isoDate(d);
  }

  // 服务端时间形如 "2026-10-22 23:01:00"
  const fmtDT = (s) => (s ? s.slice(5, 16) : '');
  const fmtD = (s) => (s ? s.slice(5, 10) : '');
  const todayInput = () => isoDate(new Date());

  function daysLeft(due) {
    if (!due) return null;
    const t = new Date(`${todayInput()}T00:00:00`);
    const d = new Date(`${due}T00:00:00`);
    if (Number.isNaN(d.getTime())) return null;
    return Math.round((d - t) / 86400000);
  }

  function dueText(loan) {
    const left = daysLeft(loan.due_date);
    if (left === null) return { text: '未设归还日期', cls: '' };
    if (left < 0) return { text: `已超期 ${-left} 天`, cls: 'late' };
    if (left === 0) return { text: '今天到期', cls: 'late' };
    if (left <= 2) return { text: `还有 ${left} 天`, cls: 'soon' };
    return { text: `还有 ${left} 天`, cls: 'due' };
  }

  let toastTimer = null;
  function toast(msg, isErr = false) {
    const el = $('#toast');
    el.textContent = msg;
    el.className = `toast show${isErr ? ' err' : ''}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = 'toast'; }, isErr ? 3800 : 2400);
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    let data = null;
    try { data = await res.json(); } catch (_) { data = {}; }
    if (!res.ok) {
      const err = new Error((data && data.error) || `请求失败 (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ------------------------------------------------------------------ */
  /* 数据                                                                */
  /* ------------------------------------------------------------------ */
  async function load() {
    const data = await api('/api/bootstrap');
    state.components = data.components;
    state.loans = data.loans;
    state.locations = data.locations;
    state.categories = data.categories || [];
    state.rooms = data.rooms || [];
    state.stats = data.stats;
    state.meta = data.meta || {};
    state.today = data.today || '';

    const stillThere = (list, value) => value === 'all' || value === 'none'
      || list.some((x) => String(x.id) === value);
    if (!stillThere(state.locations, state.loc)) state.loc = 'all';
    if (!stillThere(state.categories, state.cat)) state.cat = 'all';
    if (!stillThere(state.rooms, state.room)) state.room = 'all';

    render();
  }

  async function loadBatches() {
    const data = await api('/api/batches');
    state.batches = data.batches;
    renderBatches();
  }

  async function loadLog() {
    const data = await api('/api/movements?limit=500');
    state.movements = data.movements;
    if (state.tab === 'log') renderLog();
  }

  const componentById = (id) => state.components.find((c) => c.id === Number(id));

  /* ------------------------------------------------------------------ */
  /* 渲染总入口                                                          */
  /* ------------------------------------------------------------------ */
  function render() {
    renderStats();
    renderTabs();
    renderFoot();
    const isStock = state.tab === 'stock';
    $('#filterBar').classList.toggle('hidden', !isStock);
    if (isStock) renderFilters();
    if (state.tab === 'stock') renderStock();
    else if (state.tab === 'loans') renderLoans();
    else if (state.tab === 'intake') renderIntake();
    else if (state.tab === 'loss') renderLosses();
    else renderLog();
  }

  async function loadLosses() {
    const data = await api('/api/losses');
    state.losses = data;
    if (state.tab === 'loss') renderLosses();
  }

  function renderStats() {
    const s = state.stats;
    $('#stats').innerHTML = `
      <div class="stat"><b>${s.kinds || 0}</b><span>元件种类</span></div>
      <div class="stat"><b>${s.in_stock || 0}</b><span>在库总数</span></div>
      <div class="stat"><b>${s.out_units || 0}</b><span>外借未还 · ${s.borrowers || 0} 人</span></div>
      <div class="stat ${s.overdue ? 'alert' : ''}"><b>${s.overdue || 0}</b><span>超期未还</span></div>`;
  }

  function renderTabs() {
    $$('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === state.tab));
    const n = state.stats.open_loans || 0;
    $('#loanCount').textContent = n || '';
    $('#loanCount').classList.toggle('show', !!n);
  }

  function renderFoot() {
    const dir = state.meta.data_dir || '';
    $('#footInfo').textContent = `${state.stats.kinds || 0} 种元件` + (dir ? ` · 数据在 ${dir}` : '');
    $('#footInfo').title = state.meta.db_path || '';
  }

  function emptyState(title, desc, actions = '') {
    return `<div class="card empty">
      <div class="big">📦</div>
      <h3>${esc(title)}</h3>
      <p>${desc}</p>
      ${actions ? `<div class="empty-actions">${actions}</div>` : ''}
    </div>`;
  }

  const matches = (text) => !state.q || String(text).toLowerCase().includes(state.q.toLowerCase());

  /* ---------------------------- 筛选：分类 / 房间 / 位置 ---------------------------- */
  function optionsFor(list, allLabel, noneLabel, selected, noneCount) {
    let html = `<option value="all"${selected === 'all' ? ' selected' : ''}>${allLabel}</option>`;
    for (const item of list) {
      const value = String(item.id);
      html += `<option value="${value}"${selected === value ? ' selected' : ''}>`
        + `${esc(item.name)}（${item.count}）</option>`;
    }
    if (noneCount) {
      html += `<option value="none"${selected === 'none' ? ' selected' : ''}>`
        + `${noneLabel}（${noneCount}）</option>`;
    }
    return html;
  }

  function renderFilters() {
    const bar = $('#filterBar');
    if (!state.components.length) {
      bar.innerHTML = '';
      return;
    }
    const noneCat = state.components.filter((c) => !c.category_id).length;
    const noneRoom = state.components.filter((c) => !c.room_id).length;
    const noneLoc = state.components.filter((c) => !c.location_id).length;
    const active = state.cat !== 'all' || state.room !== 'all' || state.loc !== 'all';

    bar.innerHTML = `
      <select id="filterCat" aria-label="按分类筛选">
        ${optionsFor(state.categories, '全部分类', '未分类', state.cat, noneCat)}</select>
      <select id="filterRoom" aria-label="按物理储存筛选">
        ${optionsFor(state.rooms, '全部房间', '未分配房间', state.room, noneRoom)}</select>
      <select id="filterLoc" aria-label="按存放位置筛选">
        ${optionsFor(state.locations, '全部位置', '未分配位置', state.loc, noneLoc)}</select>
      ${active ? '<button type="button" class="btn sm" id="filterReset">清空筛选</button>' : ''}`;
  }

  function applyFilters(list) {
    return list.filter((c) => {
      const hit = (stateValue, actual) => {
        if (stateValue === 'all') return true;
        if (stateValue === 'none') return !actual;
        return String(actual) === stateValue;
      };
      return hit(state.cat, c.category_id) && hit(state.room, c.room_id) && hit(state.loc, c.location_id);
    });
  }

  function renderStock() {
    const list = applyFilters(state.components).filter((c) =>
      matches([c.name, c.spec, c.code, c.location, c.room, c.category].join(' ')));

    if (!state.components.length) {
      $('#view').innerHTML = emptyState(
        '还没有元件',
        '可以直接添加一个，也可以把采购清单一粘，让它自己整理入库。',
        `<button class="btn primary" id="emptyAdd" type="button">新增元件</button>
         <button class="btn sub" id="emptyIntake" type="button">粘贴清单入库</button>`,
      );
      const a = $('#emptyAdd'); if (a) a.onclick = () => openComponentSheet(null);
      const b = $('#emptyIntake'); if (b) b.onclick = () => switchTab('intake');
      return;
    }

    if (!list.length) {
      $('#view').innerHTML = emptyState('没找到匹配的元件', '换个关键词，或者把上面的筛选清掉。');
      return;
    }

    $('#view').innerHTML = list.map((c) => `
      <article class="card comp">
        <div class="comp-main">
          <div class="comp-title">
            <span class="name">${esc(c.name)}</span>
            ${c.category ? `<span class="tag">${esc(c.category)}</span>` : ''}
            ${c.low ? '<span class="tag low">库存偏低</span>' : ''}
          </div>
          ${c.spec ? `<div class="comp-spec">${esc(c.spec)}</div>` : ''}
          <div class="comp-meta">
            <span>${esc(c.code)}</span>
            ${c.room ? `<span class="tag room">🏠 ${esc(c.room)}</span>` : ''}
            ${c.location ? `<span class="tag loc">📍 ${esc(c.location)}</span>` : (c.room ? '' : '<span>未分配位置</span>')}
            ${c.out_qty ? `<span class="out">外借 ${c.out_qty} ${esc(c.unit)}</span>` : ''}
            ${c.lost ? `<span class="tag loss">损耗 ${c.lost}</span>` : ''}
            ${c.min_stock ? `<span>安全库存 ${c.min_stock}</span>` : ''}
          </div>
          <div class="mini-actions">
            <button class="link-btn" data-edit="${c.id}" type="button">编辑</button>
            <button class="link-btn" data-stockin="${c.id}" type="button">入库</button>
            <button class="link-btn" data-loss="${c.id}" type="button">损耗</button>
            ${c.undo ? `<button class="link-btn" data-undo="${c.id}" type="button"
              title="撤回 ${fmtDT(c.undo.ts)} 的${c.undo.type === 'in' ? '入库' : '盘点'} ${c.undo.qty > 0 ? '+' : ''}${c.undo.qty}">撤回</button>` : ''}
            <button class="link-btn danger" data-del="${c.id}" type="button">删除</button>
          </div>
        </div>
        <div class="comp-side">
          <div class="qty">
            <b class="${c.quantity ? '' : 'zero'}">${c.quantity}</b>
            <span>${esc(c.unit)}在库</span>
          </div>
          <button class="btn primary" data-borrow="${c.id}" type="button"
            ${c.quantity ? '' : 'disabled'}>借出</button>
        </div>
      </article>`).join('');
  }

  /* ---------------------------- 借还 ---------------------------- */
  function renderLoans() {
    if (!state.loans.length) {
      $('#view').innerHTML = emptyState(
        '现在没有外借的元件',
        '在库存里点「借出」，就能记下谁拿走了什么。',
        '<button class="btn sub" type="button" id="emptyStock">去库存看看</button>',
      );
      const b = $('#emptyStock'); if (b) b.onclick = () => switchTab('stock');
      return;
    }

    const groups = new Map();
    for (const l of state.loans) {
      if (!groups.has(l.borrower)) groups.set(l.borrower, []);
      groups.get(l.borrower).push(l);
    }
    const ordered = Array.from(groups.entries()).sort((a, b) => {
      const bad = (ls) => ls.filter((l) => l.overdue).length;
      return bad(b[1]) - bad(a[1]) || a[0].localeCompare(b[0], 'zh');
    });

    const openSet = state.loanOpen;
    const totalItems = state.loans.length;
    const totalLate = state.loans.filter((l) => l.overdue).length;
    const allOpen = ordered.every(([who]) => openSet.has(who));

    $('#view').innerHTML = `
      <div class="loans-bar">
        <span>${ordered.length} 人 · ${totalItems} 项未还${totalLate ? ` · <b>${totalLate} 项超期</b>` : ''}</span>
        <button class="link-btn" id="toggleAllLoans" type="button">${allOpen ? '全部收起' : '全部展开'}</button>
      </div>
      ${ordered.map(([who, loans]) => {
        const totalQty = loans.reduce((n, l) => n + l.remaining, 0);
        const late = loans.filter((l) => l.overdue).length;
        const open = openSet.has(who);
        return `<section class="group ${open ? 'open' : ''}" data-group="${esc(who)}">
          <button class="group-head" data-toggle-group="${esc(who)}" type="button"
                  aria-expanded="${open}">
            <span class="chev" aria-hidden="true">▸</span>
            <span class="who">${esc(who)}</span>
            <span class="sum">${loans.length} 项 · ${totalQty} 件</span>
            ${late ? `<span class="bad">${late} 项超期</span>` : ''}
          </button>
          ${open ? `<div class="loan-list">${loans.map((l) => {
            const due = dueText(l);
            return `<article class="card loan ${l.overdue ? 'overdue' : ''}">
              <div class="loan-main">
                <div class="loan-title">
                  ${esc(l.component_name)}
                  <span class="times">×${l.remaining}</span>
                </div>
                <div class="loan-meta">
                  <span>${fmtDT(l.taken_at)} 借出</span>
                  <span class="${due.cls}">${l.due_date ? `应还 ${fmtD(l.due_date)} · ${due.text}` : due.text}</span>
                  ${l.returned_qty ? `<span>已还 ${l.returned_qty}</span>` : ''}
                  ${l.purpose ? `<span>用途：${esc(l.purpose)}</span>` : ''}
                </div>
              </div>
              <button class="btn sub" data-return="${l.id}" type="button">归还</button>
            </article>`;
          }).join('')}</div>` : ''}
        </section>`;
      }).join('')}`;
  }

  /* ---------------------------- 入库 ---------------------------- */
  function renderIntake() {
    $('#view').innerHTML = `
      <div class="card intake">
        <h3>清单入库</h3>
        <p class="lead">粘文字、传图片、传 Excel / Word 都行。数量没写就按 1 件算，读完逐条核对再入库。</p>

        <div class="ocr-box" id="ocrBox">
          <input type="file" id="ocrFile"
                 accept=".png,.jpg,.jpeg,.webp,.bmp,image/png,image/jpeg,image/webp,image/bmp,.xlsx,.docx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                 hidden>
          <div class="ocr-head">
            <span class="ocr-ico">📎</span>
            <div class="ocr-copy">
              <b>图片 / Excel / Word</b>
              <span>拖进来、点这里选，或 Ctrl+V 粘贴截图，自动整理成清单</span>
            </div>
            <button class="btn sm" id="ocrPick" type="button">选择文件</button>
          </div>
          <div class="ocr-status hidden" id="ocrStatus"></div>
          <div class="ocr-thumb hidden" id="ocrThumb"></div>
        </div>

        <textarea id="intakeText" placeholder="例：&#10;金属膜电阻 10kΩ 1/4W 100&#10;STM32F103C8T6 5块&#10;瓷片电容 100nF, 200个&#10;杜邦线 40根">${esc(state.intake.text)}</textarea>
        <div class="examples">
          <button type="button" data-example="电阻 10k 100&#10;电容 100nF 200&#10;杜邦线 40根">示例：随手写</button>
          <button type="button" data-example="名称&#9;规格&#9;数量&#10;STM32F103C8T6&#9;Blue Pill&#9;5&#10;ESP32-C3&#9;WiFi+BLE&#9;10">示例：表格粘贴</button>
        </div>
        <div class="intake-actions">
          <button class="btn primary" id="btnParse" type="button">解析清单</button>
          <span class="hint" id="parseHint">粘贴后会自动解析</span>
        </div>
      </div>
      <div id="previewBox"></div>
      <div id="batchBox"></div>`;

    bindOcrEvents();
    replaceIntakeView();
  }

  function setOcrStatus(text, isErr = false) {
    const el = $('#ocrStatus');
    if (!el) return;
    if (!text) {
      el.classList.add('hidden');
      el.textContent = '';
      return;
    }
    el.classList.remove('hidden');
    el.textContent = text;
    el.classList.toggle('err', !!isErr);
  }

  function bindOcrEvents() {
    const box = $('#ocrBox');
    if (!box) return;
    const file = $('#ocrFile');
    const pick = () => file && file.click();

    $('#ocrPick').addEventListener('click', pick);
    box.addEventListener('click', (e) => {
      if (e.target === box || e.target.closest('.ocr-copy')) pick();
    });

    file.addEventListener('change', () => {
      if (file.files && file.files[0]) handleFile(file.files[0]);
      file.value = '';
    });

    ['dragenter', 'dragover'].forEach((ev) => box.addEventListener(ev, (e) => {
      e.preventDefault();
      box.classList.add('drag');
    }));
    ['dragleave', 'dragend'].forEach((ev) => box.addEventListener(ev, () => box.classList.remove('drag')));
    box.addEventListener('drop', (e) => {
      e.preventDefault();
      box.classList.remove('drag');
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) handleFile(f);
    });
  }

  const IMAGE_MIME = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.bmp': 'image/bmp',
  };

  async function handleFile(file) {
    if (!file) return;
    const name = (file.name || '').toLowerCase();
    const ext = name.slice(name.lastIndexOf('.'));
    const isImage = /^image\//.test(file.type) || !!IMAGE_MIME[ext];
    const isExcel = ext === '.xlsx' || /spreadsheetml\.sheet/.test(file.type || '');
    const isWord = ext === '.docx' || /wordprocessingml\.document/.test(file.type || '');

    if (!isImage && !isExcel && !isWord) {
      toast('支持图片、Excel（.xlsx）、Word（.docx）', true);
      return;
    }
    if (file.size > 15 * 1024 * 1024) {
      toast('文件太大了（最大 15MB）', true);
      return;
    }

    const thumb = $('#ocrThumb');
    if (thumb) {
      thumb.classList.remove('hidden');
      thumb.innerHTML = isImage
        ? `<img alt="待识别图片" src="${URL.createObjectURL(file)}">`
        : `<div class="ocr-file">📄 ${esc(file.name)}<span>${(file.size / 1024).toFixed(0)} KB</span></div>`;
    }
    const pickBtn = $('#ocrPick');
    if (pickBtn) pickBtn.disabled = true;
    setOcrStatus(isImage ? '正在识别图片…' : '正在读取文件…', false);

    try {
      const headers = isImage
        ? { 'Content-Type': IMAGE_MIME[ext] || file.type || 'image/png' }
        : { 'X-File-Name': encodeURIComponent(file.name) };
      const res = await fetch(isImage ? '/api/ocr' : '/api/import', {
        method: 'POST',
        headers,
        body: file,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `读取失败 (${res.status})`);

      state.intake.text = data.text;
      const ta = $('#intakeText');
      if (ta) ta.value = data.text;
      setOcrStatus((isImage ? `识别出 ${data.count} 行` : `读取到 ${data.count} 行`) + '，核对下面的清单再入库');
      toast(isImage ? '识别完成，已填进清单' : '文件读完了，已填进清单');
      doParse();
    } catch (err) {
      const msg = (err && err.name === 'TypeError')
        ? '上传失败：确认启动窗口还开着，然后重试'
        : err.message;
      setOcrStatus(msg, true);
      toast(msg, true);
    } finally {
      if (pickBtn) pickBtn.disabled = false;
    }
  }

  function replaceIntakeView() {
    // 只重画预览和入库单，避免打断正在输入的清单
    if (state.tab !== 'intake') return;
    const box = $('#previewBox');
    if (box) {
      box.innerHTML = previewHtml();
      bindPreviewEvents();
    }
    renderBatches();
  }

  function previewHtml() {
    const rows = state.intake.rows;
    if (!rows.length) return '';

    const totalQty = rows.reduce((n, r) => n + (Number(r.qty) || 0), 0);
    const newCount = rows.filter((r) => !r.component_id).length;

    return `<div class="card preview">
      <div class="preview-head">
        <div class="field">
          <label for="batchName">入库单名称</label>
          <input id="batchName" maxlength="60" value="${esc(state.intake.batchName)}" placeholder="例如：10月淘宝采购">
        </div>
        <div class="field">
          <label for="batchCat">分类</label>
          <select id="batchCat" data-empty="不分类">
            ${lookupOptions('categories', state.intake.categoryId, '不分类')}
          </select>
        </div>
        <div class="field">
          <label for="batchRoom">物理储存（房间）</label>
          <select id="batchRoom" data-empty="不指定">
            ${lookupOptions('rooms', state.intake.roomId, '不指定')}
          </select>
        </div>
        <div class="field">
          <label for="batchLoc">存放位置（柜 / 层）</label>
          <select id="batchLoc" data-empty="不指定">
            ${lookupOptions('locations', state.intake.locationId, '不指定')}
          </select>
        </div>
      </div>

      <div class="prow head">
        <span>名称</span><span>数量</span><span>入库到</span>
      </div>

      ${rows.map((r, i) => `
        <div class="prow" data-idx="${i}">
          <div class="pname">
            <input data-field="name" value="${esc(r.name)}" maxlength="80">
            <div class="pmeta">
              <span class="praw">
                ${r.qty_guessed ? '<span class="warn">没读到数量，按 1 件</span> · ' : ''}
                ${r.confidence === 'exact' ? '名称一致' : r.confidence === 'fuzzy' ? '名称相近' : ''}
                ${r.raw && r.raw !== r.name ? `原：${esc(r.raw.slice(0, 42))}` : ''}
              </span>
              <button class="link-btn danger" data-remove-row="${i}" type="button">移除</button>
            </div>
          </div>
          <div class="pqty">
            <input data-field="qty" type="number" min="1" step="1" value="${Number(r.qty) || 1}">
            <span class="unit">${esc(r.unit || '个')}</span>
          </div>
          <div class="ptarget">
            <select data-field="target">
              <option value="">＋ 新建元件</option>
              ${state.components.map((c) =>
                `<option value="${c.id}" ${String(r.component_id) === String(c.id) ? 'selected' : ''}>${esc(c.name)}（在库 ${c.quantity}）</option>`).join('')}
            </select>
            <span class="tag ${r.component_id ? 'merge' : 'newc'}">${r.component_id ? '合并入库' : '新建'}</span>
          </div>
        </div>`).join('')}

      <div class="preview-foot">
        <span class="sum" id="previewSummary">
          共 <b>${rows.length}</b> 种 / <b>${totalQty}</b> 件，其中新建 <b>${newCount}</b> 种
        </span>
        <button class="btn primary" id="btnCommit" type="button">确认入库</button>
      </div>
    </div>`;
  }

  function bindPreviewEvents() {
    const box = $('#previewBox');
    if (!box) return;
    const nameInput = $('#batchName', box);
    if (nameInput) nameInput.addEventListener('input', (e) => { state.intake.batchName = e.target.value; });

    const catSel = $('#batchCat', box);
    if (catSel) {
      bindLookupSelect(catSel, 'categories');
      catSel.addEventListener('change', (e) => { state.intake.categoryId = e.target.value === '__new' ? state.intake.categoryId : e.target.value; });
    }
    const roomSel = $('#batchRoom', box);
    if (roomSel) {
      bindLookupSelect(roomSel, 'rooms');
      roomSel.addEventListener('change', (e) => { state.intake.roomId = e.target.value === '__new' ? state.intake.roomId : e.target.value; });
    }
    const locSel = $('#batchLoc', box);
    if (locSel) {
      bindLookupSelect(locSel, 'locations');
      locSel.addEventListener('change', (e) => { state.intake.locationId = e.target.value === '__new' ? state.intake.locationId : e.target.value; });
    }

    $$('.prow[data-idx]', box).forEach((row) => {
      const idx = Number(row.dataset.idx);
      $$('input, select', row).forEach((input) => {
        input.addEventListener('input', (e) => {
          const field = e.target.dataset.field;
          if (field === 'name') state.intake.rows[idx].name = e.target.value;
          if (field === 'qty') {
            state.intake.rows[idx].qty = Number(e.target.value) || 0;
            updateSummary();
          }
        });
        if (input.tagName === 'SELECT') {
          input.addEventListener('change', (e) => {
            const val = e.target.value;
            state.intake.rows[idx].component_id = val ? Number(val) : null;
            replaceIntakeView();
          });
        }
      });
    });

    $$('[data-remove-row]', box).forEach((btn) => btn.addEventListener('click', (e) => {
      e.preventDefault();
      state.intake.rows.splice(Number(btn.dataset.removeRow), 1);
      replaceIntakeView();
    }));

    const btn = $('#btnCommit', box);
    if (btn) btn.addEventListener('click', commitBatch);
  }

  function updateSummary() {
    const el = $('#previewSummary');
    if (!el) return;
    const rows = state.intake.rows;
    const totalQty = rows.reduce((n, r) => n + (Number(r.qty) || 0), 0);
    const newCount = rows.filter((r) => !r.component_id).length;
    el.innerHTML = `共 <b>${rows.length}</b> 种 / <b>${totalQty}</b> 件，其中新建 <b>${newCount}</b> 种`;
  }

  async function doParse() {
    const text = state.intake.text.trim();
    if (!text) {
      state.intake.rows = [];
      replaceIntakeView();
      return;
    }
    const hint = $('#parseHint');
    if (hint) hint.textContent = '解析中…';
    try {
      const data = await api('/api/batches/parse', { method: 'POST', body: JSON.stringify({ text }) });
      state.intake.rows = data.items.map((it) => ({
        name: it.name, spec: it.spec, qty: it.qty, unit: it.unit,
        component_id: it.match_id, confidence: it.confidence,
        match_name: it.match_name, qty_guessed: it.qty_guessed, raw: it.raw,
      }));
      if (!state.intake.batchName) state.intake.batchName = `${state.today || todayInput()} 入库`;
      if (!state.intake.locationId && state.locations.length && state.loc !== 'none') {
        const current = state.loc !== 'all' ? state.loc : String(state.locations[0].id);
        if (state.locations.some((l) => String(l.id) === current)) state.intake.locationId = current;
      }
      if (hint) {
        hint.textContent = data.items.length
          ? `解析出 ${data.items.length} 种${data.merged ? `（合并了 ${data.merged} 行重复）` : ''}，核对一下再入库`
          : '没解析出内容，检查一下每行有没有名称';
      }
      replaceIntakeView();
    } catch (err) {
      if (hint) hint.textContent = '';
      toast(err.message, true);
    }
  }

  async function commitBatch() {
    const rows = state.intake.rows;
    if (!rows.length) return;
    const items = rows.map((r) => ({
      name: String(r.name || '').trim(),
      spec: r.spec || '',
      unit: r.unit || '个',
      qty: Number(r.qty) || 0,
      component_id: r.component_id || null,
    }));
    if (items.some((it) => !it.name)) { toast('有条目没写名称', true); return; }
    if (items.some((it) => it.qty < 1)) { toast('数量要大于 0', true); return; }

    const payload = {
      name: state.intake.batchName.trim() || `${state.today || todayInput()} 入库`,
      category_id: state.intake.categoryId || null,
      room_id: state.intake.roomId || null,
      location_id: state.intake.locationId || null,
      items,
    };

    const btn = $('#btnCommit');
    if (btn) { btn.disabled = true; btn.textContent = '入库中…'; }
    try {
      const result = await api('/api/batches', { method: 'POST', body: JSON.stringify(payload) });
      state.intake = {
        text: '', rows: [], batchName: '',
        categoryId: state.intake.categoryId, roomId: state.intake.roomId,
        locationId: state.intake.locationId, note: '',
      };
      await Promise.all([load(), loadBatches()]);
      if (state.tab === 'intake') {
        const ta = $('#intakeText');
        if (ta) ta.value = '';
        replaceIntakeView();
      }
      toast(`已入库：${result.created} 种新建 + ${result.merged} 种合并，共 ${result.total_qty} 件`);
      openBatchDetail(result.id);
    } catch (err) {
      toast(err.message, true);
      if (btn) { btn.disabled = false; btn.textContent = '确认入库'; }
    }
  }

  function renderBatches() {
    const box = $('#batchBox');
    if (!box) return;
    if (!state.batches.length) {
      box.innerHTML = '';
      return;
    }
    box.innerHTML = `<div class="batch-section">
      <h3>最近入库单</h3>
      <div class="card">
        ${state.batches.map((b) => `
          <div class="batchrow ${b.reverted ? 'is-reverted' : ''}" data-batch="${b.id}">
            <div class="b-main">
              <div class="b-name">${esc(b.name)}
                ${b.reverted ? '<span class="tag">已撤回</span>' : ''}</div>
              <div class="b-meta">
                <span>${fmtDT(b.created_at)}</span>
                ${b.room ? `<span>🏠 ${esc(b.room)}</span>` : ''}
                ${b.location ? `<span>📍 ${esc(b.location)}</span>` : ''}
              </div>
            </div>
            <div class="b-num">${b.item_count} 种 / ${b.total_qty} 件</div>
            ${b.reverted ? '' : `<button class="link-btn danger" data-revert-batch="${b.id}" type="button">撤回</button>`}
          </div>`).join('')}
      </div>
    </div>`;
  }

  /* ---------------------------- 损耗 ---------------------------- */
  function renderLosses() {
    if (!state.losses) {
      $('#view').innerHTML = '<div class="card empty"><h3>正在统计…</h3></div>';
      loadLosses().catch((err) => toast(err.message, true));
      return;
    }
    const L = state.losses;

    if (!L.total) {
      $('#view').innerHTML = emptyState(
        '还没有损耗记录',
        '坏掉、丢掉、烧掉的元件记在这里，库存会自动减掉；借出去还不回来的，归还时也能选「记为损耗」。',
        '<button class="btn primary" id="emptyLoss" type="button">记一笔损耗</button>',
      );
      const b = $('#emptyLoss'); if (b) b.onclick = () => openLossSheet(null);
      return;
    }

    const chips = [{ reason: 'all', qty: L.total }].concat(L.reasons)
      .map((r) => `<button type="button" data-loss-reason="${esc(r.reason)}"
        class="${state.lossFilter === r.reason ? 'active' : ''}">
        ${r.reason === 'all' ? '全部' : esc(r.reason)}<span class="n">${r.qty}</span></button>`).join('');

    const items = L.items.filter((it) => state.lossFilter === 'all'
      || it.reasons.some((r) => r.reason === state.lossFilter));

    const rows = items.map((it) => {
      const qty = state.lossFilter === 'all'
        ? it.qty
        : (it.reasons.find((r) => r.reason === state.lossFilter) || {}).qty || 0;
      return `<div class="logrow">
        <div class="when">${fmtDT(it.last_ts)}</div>
        <div class="what">
          <div class="l1"><b>${esc(it.name)}</b>
            ${it.code ? `<span class="tag">${esc(it.code)}</span>` : ''}</div>
          <div class="l2">${esc(it.reason_text)}</div>
        </div>
        <div class="loss-num">-${qty}</div>
      </div>`;
    }).join('');

    $('#view').innerHTML = `
      <div class="card loss-panel">
        <div class="cu-grid">
          <div class="cu"><b>${L.total}</b><span>累计损耗（件）</span></div>
          <div class="cu"><b>${L.month}</b><span>本月损耗</span></div>
          <div class="cu"><b>${L.components}</b><span>涉及元件</span></div>
          <div class="cu"><b>${L.from_loans}</b><span>借出时损耗</span></div>
        </div>
        <div class="filters" id="lossReasons">${chips}</div>
        <button class="btn primary block" id="addLoss" type="button">＋ 记一笔损耗</button>
      </div>
      <div class="card logcard">${rows}</div>`;
  }

  /* ---------------------------- 流水 ---------------------------- */
  function renderLog() {
    const filters = [['all', '全部'], ['in', '入库'], ['out', '借出'], ['return', '归还'],
                     ['adjust', '盘点'], ['loss', '损耗'], ['revert', '撤回']];
    const rows = state.movements.filter((m) => {
      if (state.logFilter !== 'all' && m.type !== state.logFilter) return false;
      if (!state.q) return true;
      return matches([m.component_name, m.component_code, m.actor, m.borrower, m.note,
                      m.reason, m.batch_name].join(' '));
    });

    $('#view').innerHTML = `
      <div class="filters">
        ${filters.map(([k, label]) =>
          `<button type="button" data-filter="${k}" class="${state.logFilter === k ? 'active' : ''}">${label}</button>`).join('')}
      </div>
      ${rows.length ? `<div class="card logcard">${rows.map(logRow).join('')}</div>`
        : emptyState('没有相关记录', '换个筛选条件试试。')}`;
  }

  function logRow(m) {
    const label = TYPE_LABEL[m.type] || m.type;
    const signed = (m.type === 'out' || m.type === 'loss') ? -m.qty : m.qty;
    const qty = signed ? `${signed > 0 ? '+' : ''}${signed}` : '';
    let who = '';
    if (m.type === 'out') who = m.borrower ? `借给 ${esc(m.borrower)}` : '';
    else if (m.type === 'return') who = m.borrower ? `${esc(m.borrower)} 归还` : '';
    else if (m.type === 'loss' && m.borrower) who = `${esc(m.borrower)} 借的，记为损耗`;
    const parts = [who,
                   m.reason ? `原因：${esc(m.reason)}` : '',
                   (m.batch_name && m.type !== 'revert') ? `入库单：${esc(m.batch_name)}` : '',
                   m.note ? esc(m.note) : '', m.component_code ? esc(m.component_code) : '']
      .filter(Boolean).join(' · ');
    return `<div class="logrow ${m.reverted ? 'reverted' : ''}">
      <div class="when">${fmtDT(m.ts)}</div>
      <div class="what">
        <div class="l1">
          <span class="badge ${esc(m.type)}">${esc(label)}</span>
          <b>${esc(m.component_name || '—')}</b>
          ${qty ? `<span>${qty}</span>` : ''}
          ${m.reverted ? '<span class="tag">已撤回</span>' : ''}
        </div>
        ${parts ? `<div class="l2">${parts}</div>` : ''}
      </div>
    </div>`;
  }

  /* ------------------------------------------------------------------ */
  /* 弹窗基础                                                            */
  /* ------------------------------------------------------------------ */
  let sheetCloseHook = null;

  function closeSheet(skipHook = false) {
    const hook = sheetCloseHook;
    sheetCloseHook = null;
    $('#modalRoot').innerHTML = '';
    document.body.style.overflow = '';
    if (hook && !skipHook) hook();
  }

  function mountSheet(html, onClose) {
    closeSheet(true);
    sheetCloseHook = onClose || null;
    const root = $('#modalRoot');
    root.innerHTML = `<div class="overlay"><div class="sheet" role="dialog" aria-modal="true">${html}</div></div>`;
    document.body.style.overflow = 'hidden';
    const overlay = $('.overlay', root);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeSheet(); });
    $$('[data-close]', root).forEach((b) => b.addEventListener('click', () => closeSheet()));
    const first = $('input:not([type=hidden]), textarea, select', root);
    if (first) setTimeout(() => first.focus({ preventScroll: true }), 60);
    return root;
  }

  function sheetHead(title, sub) {
    return `<div class="sheet-head">
      <div><h2>${esc(title)}</h2>${sub ? `<p class="sub">${esc(sub)}</p>` : ''}</div>
      <button class="x" data-close type="button" aria-label="关闭">✕</button>
    </div>`;
  }

  /* ---------------------------- 借出 ---------------------------- */
  function openBorrowSheet(cid) {
    const c = componentById(cid);
    if (!c) return;
    const last = localStorage.getItem('yj_borrower') || '';
    const names = Array.from(new Set(state.loans.map((l) => l.borrower))).filter((n) => n !== last).slice(0, 5);

    const root = mountSheet(`
      ${sheetHead(`借出：${c.name}`, `${c.code}${c.spec ? ' · ' + c.spec : ''} · 在库 ${c.quantity} ${c.unit}`)}
      <form id="borrowForm">
        <div class="field">
          <label for="bname">借用人 <span class="req">*</span></label>
          <input id="bname" required maxlength="40" placeholder="谁拿走了" value="${esc(last)}">
          ${names.length ? `<div class="chips">${names.map((n) => `<button type="button" data-name="${esc(n)}">${esc(n)}</button>`).join('')}</div>` : ''}
        </div>
        <div class="row2">
          <div class="field">
            <label for="bqty">数量</label>
            <input id="bqty" type="number" inputmode="numeric" min="1" max="${c.quantity}" value="1">
            <div class="hint">最多 ${c.quantity} ${esc(c.unit)}</div>
          </div>
          <div class="field">
            <label for="bdue">预计归还</label>
            <input id="bdue" type="date" value="" disabled data-default="${addDays(7)}">
            <label class="checkline">
              <input id="bforever" type="checkbox" checked> 永久借出（不设日期）
            </label>
          </div>
        </div>
        <div class="field">
          <label for="btaken">借出时间</label>
          <input id="btaken" type="datetime-local" value="${nowLocalInput()}">
          <div class="hint">补记以前的借出，把时间改成当时即可</div>
        </div>
        <div class="field">
          <label for="bpurpose">用途 / 备注</label>
          <input id="bpurpose" maxlength="100" placeholder="例如：电赛小车 / 课程实验">
        </div>
        <div class="sheet-foot">
          <button class="btn" type="button" data-close>取消</button>
          <button class="btn primary" type="submit">确认借出</button>
        </div>
      </form>`);

    $$('.chips button', root).forEach((b) => b.addEventListener('click', () => {
      $('#bname', root).value = b.dataset.name;
    }));

    const forever = $('#bforever', root);
    const dueInput = $('#bdue', root);
    forever.addEventListener('change', () => {
      dueInput.disabled = forever.checked;
      if (!forever.checked && !dueInput.value) dueInput.value = dueInput.dataset.default;
    });

    $('#borrowForm', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('button[type=submit]', root);
      const payload = {
        component_id: c.id,
        borrower: $('#bname', root).value.trim(),
        qty: Number($('#bqty', root).value || 1),
        due_date: forever.checked ? '' : dueInput.value,
        taken_at: $('#btaken', root).value,
        purpose: $('#bpurpose', root).value.trim(),
      };
      if (!payload.borrower) { toast('请填写借用人', true); $('#bname', root).focus(); return; }
      btn.disabled = true;
      btn.textContent = '提交中…';
      try {
        await api('/api/loans', { method: 'POST', body: JSON.stringify(payload) });
        localStorage.setItem('yj_borrower', payload.borrower);
        closeSheet();
        toast(`已记下：${payload.borrower} 借走 ${payload.qty} ${c.unit}`);
        await load();
        if (state.tab === 'loans') renderLoans();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.textContent = '确认借出';
      }
    });
  }

  /* ---------------------------- 归还 ---------------------------- */
  function openReturnSheet(lid) {
    const l = state.loans.find((x) => x.id === lid);
    if (!l) return;

    const root = mountSheet(`
      ${sheetHead(`归还：${l.component_name}`, `${l.borrower} 借走 ${l.qty} ${l.component_unit}，还剩 ${l.remaining}`)}
      <form id="returnForm">
        <div class="field">
          <label for="rqty">归还数量</label>
          <input id="rqty" type="number" inputmode="numeric" min="1" max="${l.remaining}" value="${l.remaining}">
          <div class="hint">可以只还一部分</div>
        </div>

        <div class="field">
          <label>这批怎么处理</label>
          <div class="mode-group">
            <label class="mode">
              <input type="radio" name="rmode" value="stock" checked>
              <span><b>完好入库</b><em>库存加回去</em></span>
            </label>
            <label class="mode">
              <input type="radio" name="rmode" value="loss">
              <span><b>记为损耗</b><em>库存不加，计入损耗统计</em></span>
            </label>
          </div>
        </div>

        <div class="field hidden" id="lossReasonField">
          <label for="rreason">损耗原因</label>
          <input id="rreason" list="lossReasons" maxlength="40" value="损坏">
        </div>

        <div class="field">
          <label for="rwhen">归还时间</label>
          <input id="rwhen" type="datetime-local" value="${nowLocalInput()}">
        </div>
        <div class="field">
          <label for="rnote">备注（可不填）</label>
          <input id="rnote" maxlength="200" placeholder="例如：外壳有轻微磨损">
        </div>
        <div class="sheet-foot">
          <button class="btn" type="button" data-close>取消</button>
          <button class="btn primary" type="submit">确认归还</button>
        </div>
      </form>
      <datalist id="lossReasons">
        <option>损坏</option><option>丢失</option><option>烧毁</option>
        <option>过期</option><option>其他</option>
      </datalist>`);

    $$('input[name=rmode]', root).forEach((radio) => radio.addEventListener('change', () => {
      const isLoss = $('input[name=rmode]:checked', root).value === 'loss';
      $('#lossReasonField', root).classList.toggle('hidden', !isLoss);
    }));

    $('#returnForm', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('button[type=submit]', root);
      const isLoss = $('input[name=rmode]:checked', root).value === 'loss';
      const payload = {
        qty: Number($('#rqty', root).value || l.remaining),
        returned_at: $('#rwhen', root).value,
        note: $('#rnote', root).value.trim(),
        mode: isLoss ? 'loss' : 'stock',
        reason: isLoss ? $('#rreason', root).value.trim() : '',
      };
      btn.disabled = true;
      btn.textContent = '提交中…';
      try {
        await api(`/api/loans/${l.id}/return`, { method: 'POST', body: JSON.stringify(payload) });
        closeSheet();
        toast(isLoss ? `已记为损耗 ${payload.qty} ${l.component_unit}` : `已归还 ${payload.qty} ${l.component_unit}`);
        await load();
        if (state.losses) loadLosses();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.textContent = '确认归还';
      }
    });
  }

  /* ---------------------------- 记损耗 ---------------------------- */
  function openLossSheet(cid) {
    const c = cid ? componentById(cid) : null;
    if (cid && !c) return;
    const options = state.components.map((x) =>
      `<option value="${x.id}"${c && c.id === x.id ? ' selected' : ''}>${esc(x.name)}（在库 ${x.quantity}）</option>`).join('');

    const root = mountSheet(`
      ${sheetHead(c ? `记损耗：${c.name}` : '记一笔损耗',
        c ? `当前在库 ${c.quantity} ${esc(c.unit)}` : '坏掉、丢掉、烧掉的元件，从这里减掉')}
      <form id="lossForm">
        ${c ? '' : `<div class="field"><label for="lcomp">元件</label><select id="lcomp">${options}</select></div>`}
        <div class="row2">
          <div class="field">
            <label for="lqty">损耗数量</label>
            <input id="lqty" type="number" inputmode="numeric" min="1" value="1">
          </div>
          <div class="field">
            <label for="lreason">原因</label>
            <input id="lreason" list="lossReasons" maxlength="40" value="损坏">
          </div>
        </div>
        <div class="field">
          <label for="lnote">备注（可不填）</label>
          <input id="lnote" maxlength="200" placeholder="例如：焊的时候烧了 / 找不到了">
        </div>
        <div class="sheet-foot">
          <button class="btn" type="button" data-close>取消</button>
          <button class="btn primary" type="submit">确认损耗</button>
        </div>
      </form>
      <datalist id="lossReasons">
        <option>损坏</option><option>丢失</option><option>烧毁</option>
        <option>过期</option><option>其他</option>
      </datalist>`);

    $('#lossForm', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('button[type=submit]', root);
      const targetId = c ? c.id : Number($('#lcomp', root).value);
      const qty = Number($('#lqty', root).value || 0);
      if (!targetId) { toast('先选一个元件', true); return; }
      if (qty < 1) { toast('数量要大于 0', true); return; }
      btn.disabled = true;
      btn.textContent = '保存中…';
      try {
        await api(`/api/components/${targetId}/loss`, {
          method: 'POST',
          body: JSON.stringify({
            qty,
            reason: $('#lreason', root).value.trim(),
            note: $('#lnote', root).value.trim(),
          }),
        });
        closeSheet();
        toast(`已记损耗 ${qty}`);
        await load();
        await loadLosses();
        if (state.movements.length) loadLog();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.textContent = '确认损耗';
      }
    });
  }

  /* ---------------------------- 新增 / 编辑元件 ---------------------------- */
  const LOOKUP_META = {
    categories: { label: '分类', example: '电子元器件组' },
    rooms: { label: '房间', example: '实验室 A / 仓库 B' },
    locations: { label: '位置', example: 'A柜2层' },
  };

  function lookupOptions(table, selected, emptyLabel) {
    const list = state[table] || [];
    const options = list.map((x) => `<option value="${x.id}"`
      + `${String(selected) === String(x.id) ? ' selected' : ''}>${esc(x.name)}</option>`).join('');
    const none = selected == null || selected === '' ? ' selected' : '';
    return `<option value=""${none}>${emptyLabel}</option>${options}`
      + `<option value="__new">＋ 新建${LOOKUP_META[table].label}…</option>`;
  }

  function askNameSheet(title, hint) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      const root = mountSheet(`
        ${sheetHead(title, hint || '')}
        <form id="nameForm">
          <div class="field">
            <label for="nw">名称</label>
            <input id="nw" maxlength="40" autocomplete="off">
          </div>
          <div class="sheet-foot">
            <button class="btn" type="button" data-close>取消</button>
            <button class="btn primary" type="submit">确定</button>
          </div>
        </form>`, () => finish(null));
      $('#nameForm', root).addEventListener('submit', (e) => {
        e.preventDefault();
        const value = $('#nw', root).value.trim();
        if (!value) { toast('名称不能为空', true); return; }
        closeSheet(true);
        finish(value);
      });
    });
  }

  async function createLookup(table, name) {
    const created = await api(`/api/${table}`, { method: 'POST', body: JSON.stringify({ name }) });
    const data = await api(`/api/${table}`);
    state[table] = data[table];
    return created;
  }

  // 下拉框最后一项是「＋ 新建…」，选中它就弹窗新建，建完自动选中
  function bindLookupSelect(select, table) {
    select.dataset.prev = select.value;
    select.addEventListener('change', async () => {
      if (select.value !== '__new') {
        select.dataset.prev = select.value;
        return;
      }
      const meta = LOOKUP_META[table];
      const name = await askNameSheet(`新建${meta.label}`, `例如：${meta.example}`);
      if (!name) {
        select.value = select.dataset.prev;
        return;
      }
      try {
        const created = await createLookup(table, name);
        select.innerHTML = lookupOptions(table, created.id, select.dataset.empty || '不指定');
        select.dataset.prev = String(created.id);
        toast(`${meta.label}「${created.name}」已添加`);
        // 让外面的 state 也跟着更新
        select.dispatchEvent(new Event('change', { bubbles: true }));
      } catch (err) {
        toast(err.message, true);
        select.value = select.dataset.prev;
      }
    });
  }

  function openComponentSheet(comp) {
    const isEdit = !!comp;
    const fromFilter = (value) => (value !== 'all' && value !== 'none' ? Number(value) : null);
    const c = comp || {
      code: '', name: '', spec: '', unit: '个', quantity: 0, min_stock: 0, note: '',
      category_id: fromFilter(state.cat),
      room_id: fromFilter(state.room),
      location_id: fromFilter(state.loc),
    };

    const root = mountSheet(`
      ${sheetHead(isEdit ? '编辑元件' : '新增元件', isEdit ? c.code : '只填名称也能存，分类和位置可以稍后补')}
      <form id="compForm">
        <div class="field">
          <label for="cname">名称 <span class="req">*</span></label>
          <input id="cname" required maxlength="80" value="${esc(c.name)}" placeholder="例如：STM32F103C8T6 最小系统板">
        </div>
        <div class="row2">
          <div class="field">
            <label for="cspec">型号 / 规格</label>
            <input id="cspec" maxlength="120" value="${esc(c.spec)}" placeholder="1/4W ±1%">
          </div>
          <div class="field">
            <label for="ccat">分类</label>
            <select id="ccat" data-empty="不分类">
              ${lookupOptions('categories', c.category_id, '不分类')}
            </select>
          </div>
        </div>
        <div class="row2">
          <div class="field">
            <label for="croom">物理储存（房间）</label>
            <select id="croom" data-empty="不指定">
              ${lookupOptions('rooms', c.room_id, '不指定')}
            </select>
          </div>
          <div class="field">
            <label for="cloc">存放位置（柜 / 层）</label>
            <select id="cloc" data-empty="不指定">
              ${lookupOptions('locations', c.location_id, '不指定')}
            </select>
          </div>
        </div>
        <div class="row2">
          <div class="field">
            <label for="ccode">编号</label>
            <input id="ccode" maxlength="40" value="${esc(c.code)}" placeholder="留空自动生成">
          </div>
          <div class="field">
            <label for="cunit">单位</label>
            <input id="cunit" maxlength="10" value="${esc(c.unit)}" placeholder="个">
          </div>
        </div>
        <div class="row2">
          <div class="field">
            <label for="cqty">${isEdit ? '在库数量' : '初始数量'}</label>
            <input id="cqty" type="number" inputmode="numeric" min="0" value="${c.quantity}">
          </div>
          <div class="field">
            <label for="cmin">安全库存</label>
            <input id="cmin" type="number" inputmode="numeric" min="0" value="${c.min_stock}">
          </div>
        </div>
        <div class="field">
          <label for="cnote">备注</label>
          <textarea id="cnote" maxlength="300" placeholder="采购日期、替代型号、注意事项…">${esc(c.note)}</textarea>
        </div>
        <div class="sheet-foot">
          <button class="btn" type="button" data-close>取消</button>
          <button class="btn primary" type="submit">${isEdit ? '保存修改' : '添加'}</button>
        </div>
      </form>`);

    bindLookupSelect($('#ccat', root), 'categories');
    bindLookupSelect($('#croom', root), 'rooms');
    bindLookupSelect($('#cloc', root), 'locations');

    $('#compForm', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('button[type=submit]', root);
      const pick = (el) => (el.value && el.value !== '__new' ? el.value : null);
      const payload = {
        name: $('#cname', root).value.trim(),
        spec: $('#cspec', root).value.trim(),
        category_id: pick($('#ccat', root)),
        room_id: pick($('#croom', root)),
        location_id: pick($('#cloc', root)),
        code: $('#ccode', root).value.trim(),
        quantity: Number($('#cqty', root).value || 0),
        min_stock: Number($('#cmin', root).value || 0),
        unit: $('#cunit', root).value.trim() || '个',
        note: $('#cnote', root).value.trim(),
      };
      if (!payload.name) { toast('名称不能为空', true); return; }
      btn.disabled = true;
      btn.textContent = '保存中…';
      try {
        await api(isEdit ? `/api/components/${c.id}` : '/api/components', {
          method: isEdit ? 'PATCH' : 'POST',
          body: JSON.stringify(payload),
        });
        closeSheet();
        toast(isEdit ? '已保存' : '已添加');
        await load();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.textContent = isEdit ? '保存修改' : '添加';
      }
    });
  }

  /* ---------------------------- 单个入库 / 盘点 ---------------------------- */
  function openStockSheet(cid) {
    const c = componentById(cid);
    if (!c) return;
    const root = mountSheet(`
      ${sheetHead(`入库：${c.name}`, `当前在库 ${c.quantity} ${c.unit}`)}
      <form id="stockForm">
        <div class="field">
          <label for="sQty">数量</label>
          <input id="sQty" type="number" inputmode="numeric" step="1" value="10">
          <div class="hint">填正数是入库；填负数（如 -2）是盘亏修正。</div>
        </div>
        <div class="field">
          <label for="sNote">备注</label>
          <input id="sNote" maxlength="200" placeholder="例如：淘宝补货 / 盘点修正">
        </div>
        <div class="sheet-foot">
          <button class="btn" type="button" data-close>取消</button>
          <button class="btn primary" type="submit">确定</button>
        </div>
      </form>`);

    $('#stockForm', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('button[type=submit]', root);
      const qty = Number($('#sQty', root).value || 0);
      if (!qty) { toast('数量不能为 0', true); return; }
      btn.disabled = true;
      btn.textContent = '提交中…';
      try {
        await api(`/api/components/${c.id}/${qty > 0 ? 'stock' : 'adjust'}`, {
          method: 'POST',
          body: JSON.stringify({ qty, note: $('#sNote', root).value.trim() }),
        });
        closeSheet();
        toast('库存已更新');
        await load();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.textContent = '确定';
      }
    });
  }

  /* ---------------------------- 删除 ---------------------------- */
  function openDeleteSheet(cid) {
    const c = componentById(cid);
    if (!c) return;
    const root = mountSheet(`
      ${sheetHead('删除元件', c.name)}
      <div class="callout danger">
        删除后这个元件会从库存里消失，历史流水仍保留。
        ${c.out_qty ? `<br><b>还有 ${c.out_qty} ${esc(c.unit)}在外借，得先还清。</b>` : ''}
      </div>
      <div class="sheet-foot">
        <button class="btn" type="button" data-close>算了</button>
        <button class="btn danger" id="doDel" type="button">确认删除</button>
      </div>`);
    $('#doDel', root).addEventListener('click', async () => {
      try {
        await api(`/api/components/${c.id}`, { method: 'DELETE' });
        closeSheet();
        toast('已删除');
        await load();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  /* ---------------------------- 入库单详情 ---------------------------- */
  async function openBatchDetail(id) {
    try {
      const b = await api(`/api/batches/${id}`);
      const total = b.items.reduce((n, i) => n + i.qty, 0);
      const where = [b.room, b.location].filter(Boolean).join(' · ');
      const root = mountSheet(`
        ${sheetHead(b.name, `${b.created_at.slice(0, 16)}${where ? ' · 放入 ' + where : ''}`)}
        ${b.reverted
          ? '<div class="callout warn">这张入库单已经撤回，加进去的数量都还原了。</div>'
          : `<div class="callout">这一单共 ${b.items.length} 种、${total} 件。发现录错了可以整单撤回。</div>`}
        <div>${b.items.map((i) => `
          <div class="result-item ${i.reverted ? 'is-reverted' : ''}">
            <div class="grow">
              <div>${esc(i.name)}</div>
              <div class="code">${esc(i.code || '')}${i.type === 'new' ? ' · 新建元件' : ' · 合并到已有元件'}</div>
            </div>
            <div class="n">+${i.qty}</div>
          </div>`).join('')}</div>
        <div class="sheet-foot">
          ${b.reverted ? '' : '<button class="btn danger" id="btnRevertBatch" type="button">撤回这单</button>'}
          <button class="btn" type="button" data-close>关闭</button>
        </div>
      `);
      const btn = $('#btnRevertBatch', root);
      if (btn) btn.addEventListener('click', () => revertBatch(b.id, b.name));
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function revertBatch(id, name) {
    const label = name || (state.batches.find((b) => b.id === id) || {}).name || '这张入库单';
    if (!confirm(`撤回「${label}」？\n\n这一单加进去的数量会减回库存；如果是这单新建的元件、而且还没动过，会一并移除。`)) {
      return;
    }
    try {
      const res = await api(`/api/batches/${id}/revert`, { method: 'POST' });
      closeSheet();
      const extra = res.deleted ? `，${res.deleted} 种新建的已移除` : '';
      toast(`已撤回：${res.adjusted + res.deleted} 种${extra}`);
      await Promise.all([load(), loadBatches()]);
      if (state.tab === 'intake') replaceIntakeView();
      if (state.tab === 'log' && state.movements.length) loadLog();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function openUndoSheet(cid) {
    const c = componentById(cid);
    if (!c || !c.undo) return;
    const u = c.undo;
    const label = u.type === 'in' ? '入库' : '盘点';
    const after = c.quantity - u.qty;
    const root = mountSheet(`
      ${sheetHead('撤回这次变动', c.name)}
      <div class="callout ${after < 0 ? 'danger' : ''}">
        ${fmtDT(u.ts)} 的${label}：<b>${u.qty > 0 ? '+' : ''}${u.qty} ${esc(c.unit)}</b><br>
        库存会从 <b>${c.quantity}</b> 变回 <b>${after}</b>。
        ${u.note ? `<br><span class="muted">备注：${esc(u.note)}</span>` : ''}
      </div>
      <div class="sheet-foot">
        <button class="btn" type="button" data-close>算了</button>
        <button class="btn danger" id="doUndo" type="button" ${after < 0 ? 'disabled' : ''}>确认撤回</button>
      </div>`);
    const btn = $('#doUndo', root);
    if (btn) btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = '撤回中…';
      try {
        const res = await api(`/api/components/${c.id}/revert-last`, { method: 'POST' });
        closeSheet();
        toast(`已撤回，库存现在是 ${res.quantity}`);
        await load();
        if (state.movements.length) loadLog();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.textContent = '确认撤回';
      }
    });
  }

  function openRenameSheet(table, item) {
    const label = LOOKUP_META[table].label;
    const root = mountSheet(`
      ${sheetHead(`重命名${label}`, item.name)}
      <form id="renameForm">
        <div class="field">
          <label for="rn">新名称</label>
          <input id="rn" maxlength="40" value="${esc(item.name)}" required>
        </div>
        <div class="sheet-foot">
          <button class="btn" type="button" data-close>取消</button>
          <button class="btn primary" type="submit">保存</button>
        </div>
      </form>`);
    $('#renameForm', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const name = $('#rn', root).value.trim();
      if (!name) return;
      try {
        await api(`/api/${table}/${item.id}`, { method: 'PATCH', body: JSON.stringify({ name }) });
        toast('已重命名');
        await load();
        await openSettings();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  /* ---------------------------- 设置 ---------------------------- */
  const MANAGED = [
    { table: 'categories', title: '分类', placeholder: '电子元器件组',
      hint: '把元件分几组，比如「电子元器件组」「电子板路组」' },
    { table: 'rooms', title: '物理储存（房间）', placeholder: '实验室 A',
      hint: '东西实际放在哪个房间或仓库' },
    { table: 'locations', title: '存放位置（柜 / 层）', placeholder: 'A柜2层',
      hint: '房间里的具体位置，比如哪个柜子、第几层' },
  ];

  async function openSettings() {
    const sections = MANAGED.map(({ table, title, placeholder, hint }) => {
      const items = state[table] || [];
      return `<div class="set-section">
        <h3>${title}（${items.length}）</h3>
        ${items.length ? items.map((it) => `
          <div class="set-row">
            <span class="grow">${esc(it.name)}<span class="cnt">${it.count} 种</span></span>
            <button class="link-btn" data-rename-k="${table}" data-id="${it.id}" type="button">重命名</button>
            <button class="link-btn danger" data-remove-k="${table}" data-id="${it.id}" type="button">删除</button>
          </div>`).join('')
          : `<div class="set-row"><span class="grow" style="color:var(--ink-3)">还没有${title}，先加一个，比如「${placeholder}」</span></div>`}
        <div class="add-loc">
          <input data-new-k="${table}" maxlength="40" placeholder="新${title}，例如：${placeholder}">
          <button class="btn primary sm" data-add-k="${table}" type="button">添加</button>
        </div>
        <div class="hint" style="margin-top:6px;font-size:11.5px;color:var(--ink-3)">
          ${hint}；删掉不会删元件，只是把这一项清空。
        </div>
      </div>`;
    }).join('');

    const root = mountSheet(`
      ${sheetHead('设置', '分类、物理储存、存放位置、数据文件')}
      ${sections}

      <div class="set-section">
        <h3>数据文件</h3>
        <div class="mono" id="dataPath">${esc(state.meta.db_path || '')}</div>
        <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
          <button class="btn sm" id="copyPath" type="button">复制路径</button>
          <a class="btn sm" href="/export/components.csv">导出库存 CSV</a>
          <a class="btn sm" href="/export/loans.csv">导出借还 CSV</a>
          <a class="btn sm" href="/export/movements.csv">导出流水 CSV</a>
        </div>
        <div class="hint" style="margin-top:8px;font-size:11.5px;color:var(--ink-3)">
          备份就是复制这个文件夹里的 components.db；换位置改 config.json 的 data_dir 后重启。
        </div>
      </div>

      <div class="set-section">
        <h3>数据清理</h3>
        <div class="set-row">
          <span class="grow">清理已删除的元件、已撤回的单子、旧流水，压缩数据库</span>
          <button class="btn sm" id="openCleanup" type="button">打开清理面板</button>
        </div>
      </div>

      <div class="sheet-foot"><button class="btn" type="button" data-close>关闭</button></div>
    `);

    root.addEventListener('click', async (e) => {
      const addBtn = e.target.closest('[data-add-k]');
      if (addBtn) {
        const table = addBtn.dataset.addK;
        const meta = MANAGED.find((m) => m.table === table);
        const input = $(`[data-new-k="${table}"]`, root);
        const name = input.value.trim();
        if (!name) { toast(`先写个${meta.title}名称`, true); return; }
        try {
          await api(`/api/${table}`, { method: 'POST', body: JSON.stringify({ name }) });
          toast(`${meta.title}已添加`);
          await load();
          await openSettings();
        } catch (err) {
          toast(err.message, true);
        }
        return;
      }

      const renameBtn = e.target.closest('[data-rename-k]');
      if (renameBtn) {
        const table = renameBtn.dataset.renameK;
        const item = (state[table] || []).find((x) => x.id === Number(renameBtn.dataset.id));
        if (item) openRenameSheet(table, item);
        return;
      }

      const delBtn = e.target.closest('[data-remove-k]');
      if (delBtn) {
        const table = delBtn.dataset.removeK;
        const label = LOOKUP_META[table].label;
        const item = (state[table] || []).find((x) => x.id === Number(delBtn.dataset.id));
        if (!item) return;
        if (!confirm(`删除${label}「${item.name}」？\n${item.count} 种元件会变成「未分配」，元件本身不会删。`)) return;
        try {
          await api(`/api/${table}/${item.id}`, { method: 'DELETE' });
          toast('已删除');
          await load();
          await openSettings();
        } catch (err) {
          toast(err.message, true);
        }
      }
    });

    $$('[data-new-k]', root).forEach((input) => input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        $(`[data-add-k="${input.dataset.newK}"]`, root).click();
      }
    }));

    $('#openCleanup', root).addEventListener('click', () => openCleanupSheet());

    $('#copyPath', root).addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(state.meta.db_path || '');
        toast('路径已复制');
      } catch (_) {
        toast('复制失败，手动选中上面的路径吧', true);
      }
    });
  }

  /* ---------------------------- 数据清理 ---------------------------- */
  function confirmSheet(title, message, okText = '确定', danger = false) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      const root = mountSheet(`
        ${sheetHead(title)}
        <div class="callout ${danger ? 'danger' : ''}">${message}</div>
        <div class="sheet-foot">
          <button class="btn" type="button" data-close>取消</button>
          <button class="btn ${danger ? 'danger' : 'primary'}" id="okBtn" type="button">${esc(okText)}</button>
        </div>`, () => finish(false));
      $('#okBtn', root).addEventListener('click', () => { closeSheet(true); finish(true); });
    });
  }

  function askWord(title, message, word) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      const root = mountSheet(`
        ${sheetHead(title)}
        <div class="callout danger">${message}</div>
        <form id="wordForm">
          <div class="field">
            <label for="cw">输入「${esc(word)}」继续</label>
            <input id="cw" autocomplete="off" placeholder="${esc(word)}">
          </div>
          <div class="sheet-foot">
            <button class="btn" type="button" data-close>取消</button>
            <button class="btn danger" type="submit">确认</button>
          </div>
        </form>`, () => finish(false));
      $('#wordForm', root).addEventListener('submit', (e) => {
        e.preventDefault();
        if ($('#cw', root).value.trim() !== word) { toast(`要输入「${word}」才行`, true); return; }
        closeSheet(true);
        finish(true);
      });
    });
  }

  async function runCleanup(payload) {
    const res = await api('/api/cleanup', { method: 'POST', body: JSON.stringify(payload) });
    toast(res.message || '完成');
    await load();
    await loadBatches();
    if (state.movements.length) loadLog();
    await openCleanupSheet();
  }

  async function openCleanupSheet() {
    let s;
    try {
      s = await api('/api/cleanup');
    } catch (err) {
      toast(err.message, true);
      return;
    }
    const kb = (n) => (n >= 1024 * 1024
      ? (n / 1024 / 1024).toFixed(1) + ' MB'
      : Math.max(1, Math.round(n / 1024)) + ' KB');

    const root = mountSheet(`
      ${sheetHead('数据清理', '只影响本机这份数据；清空前会自动备份')}
      <div class="cu-grid">
        <div class="cu"><b>${s.components}</b><span>元件</span></div>
        <div class="cu"><b>${s.deleted_components}</b><span>已删除</span></div>
        <div class="cu"><b>${s.batches}</b><span>入库单 / ${s.reverted_batches} 已撤回</span></div>
        <div class="cu"><b>${s.movements}</b><span>流水</span></div>
        <div class="cu"><b>${kb(s.db_bytes)}</b><span>数据库</span></div>
        <div class="cu"><b>${s.backup_count}</b><span>备份 ${kb(s.backup_bytes)}</span></div>
      </div>

      <div class="cu-row">
        <div class="grow"><b>清理已删除的元件</b><span>彻底删掉标记删除的元件；有借还记录的会跳过</span></div>
        <button class="btn sm" data-clean="purge_deleted" type="button"
          ${s.purgable_components ? '' : 'disabled'}>
          ${s.purgable_components ? `清理 ${s.purgable_components} 个` : '没有可清的'}</button>
      </div>

      <div class="cu-row">
        <div class="grow"><b>清理已撤回的入库单</b><span>连同它的流水一起删掉，不留记录</span></div>
        <button class="btn sm danger" data-clean="purge_reverted" type="button"
          ${s.reverted_batches ? '' : 'disabled'}>
          ${s.reverted_batches ? `清理 ${s.reverted_batches} 张` : '没有可清的'}</button>
      </div>

      <div class="cu-row cu-wrap">
        <div class="grow"><b>清理旧流水</b><span>只删操作记录，不动库存和借还</span></div>
        <div class="cu-inline">
          <span>保留最近</span>
          <input id="cuDays" type="number" min="1" value="180">
          <span>天</span>
          <button class="btn sm" data-clean="purge_movements" type="button">清理</button>
        </div>
      </div>

      <div class="cu-row">
        <div class="grow"><b>压缩数据库</b><span>回收删掉数据占用的空间（VACUUM）</span></div>
        <button class="btn sm" data-clean="vacuum" type="button">压缩</button>
      </div>

      <div class="cu-row cu-wrap">
        <div class="grow"><b>清理旧备份</b><span>备份目录里只保留最近几份</span></div>
        <div class="cu-inline">
          <span>保留</span>
          <input id="cuKeep" type="number" min="1" value="5">
          <span>个</span>
          <button class="btn sm" data-clean="purge_backups" type="button">清理</button>
        </div>
      </div>

      <div class="cu-row">
        <div class="grow"><b>清理临时文件</b><span>缓存和识别时留下的临时目录</span></div>
        <button class="btn sm" data-clean="purge_temp" type="button">清理</button>
      </div>

      <div class="cu-danger">
        <div class="grow"><b>清空所有数据</b><span>元件、借还、入库单、流水全部清空；位置保留；会先自动备份</span></div>
        <button class="btn sm danger" data-clean="reset_data" type="button">清空</button>
      </div>

      <div class="cu-danger">
        <div class="grow"><b>恢复出厂</b><span>连存放位置一起清空；会先自动备份</span></div>
        <button class="btn sm danger" data-clean="reset_all" type="button">恢复出厂</button>
      </div>

      <div class="sheet-foot"><button class="btn" type="button" data-close>关闭</button></div>
    `);

    $$('[data-clean]', root).forEach((btn) => btn.addEventListener('click', async () => {
      const action = btn.dataset.clean;
      try {
        if (action === 'purge_deleted') {
          const ok = await confirmSheet('清理已删除的元件',
            `彻底删掉 ${s.purgable_components} 个已删除的元件，删了恢复不了。`, '清理', true);
          if (ok) await runCleanup({ action });
        } else if (action === 'purge_reverted') {
          const ok = await confirmSheet('清理已撤回的入库单',
            `删掉 ${s.reverted_batches} 张已撤回的入库单和它们的流水记录。`, '清理', true);
          if (ok) await runCleanup({ action });
        } else if (action === 'purge_movements') {
          const days = Math.max(1, Number($('#cuDays', root).value) || 180);
          const ok = await confirmSheet('清理旧流水',
            `删除 ${days} 天之前的操作记录，库存和借还记录不受影响。`, '清理', true);
          if (ok) await runCleanup({ action, days });
        } else if (action === 'purge_backups') {
          const keep = Math.max(1, Number($('#cuKeep', root).value) || 5);
          const ok = await confirmSheet('清理旧备份',
            `${s.backup_dir} 里只保留最近 ${keep} 份备份。`, '清理', true);
          if (ok) await runCleanup({ action, keep });
        } else if (action === 'reset_data') {
          const ok = await askWord('清空所有数据',
            '元件、借还、入库单、流水都会清空（存放位置保留）。清空前会自动备份一份。', '清空');
          if (ok) await runCleanup({ action, confirm: '清空' });
        } else if (action === 'reset_all') {
          const ok = await askWord('恢复出厂',
            '连存放位置一起清空，等于一份全新的库。清空前会自动备份一份。', '清空');
          if (ok) await runCleanup({ action, confirm: '清空' });
        } else {
          await runCleanup({ action });
        }
      } catch (err) {
        toast(err.message, true);
      }
    }));
  }

  /* ------------------------------------------------------------------ */
  /* 事件                                                                */
  /* ------------------------------------------------------------------ */
  function switchTab(tab) {
    state.tab = tab;
    render();
    if (tab === 'log' && !state.movements.length) loadLog().catch((err) => toast(err.message, true));
    if (tab === 'intake' && !state.batches.length) loadBatches().catch(() => {});
    if (tab === 'loss') loadLosses().catch((err) => toast(err.message, true));
  }

  function bindEvents() {
    $('#tabs').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-tab]');
      if (btn) switchTab(btn.dataset.tab);
    });

    let searchTimer = null;
    $('#search').addEventListener('input', (e) => {
      clearTimeout(searchTimer);
      const value = e.target.value.trim();
      searchTimer = setTimeout(() => { state.q = value; render(); }, 140);
    });

    $('#filterBar').addEventListener('change', (e) => {
      const id = e.target.id;
      if (id === 'filterCat') state.cat = e.target.value;
      else if (id === 'filterRoom') state.room = e.target.value;
      else if (id === 'filterLoc') state.loc = e.target.value;
      else return;
      renderStock();
      renderFilters();
    });

    $('#filterBar').addEventListener('click', (e) => {
      if (e.target.id !== 'filterReset') return;
      state.cat = 'all';
      state.room = 'all';
      state.loc = 'all';
      renderStock();
      renderFilters();
    });

    $('#btnSettings').addEventListener('click', () => openSettings());
    $('#btnAdd').addEventListener('click', () => openComponentSheet(null));

    $('#view').addEventListener('click', (e) => {
      const t = e.target.closest('button');
      if (t) {
        if (t.dataset.borrow) return openBorrowSheet(Number(t.dataset.borrow));
        if (t.dataset.return) return openReturnSheet(Number(t.dataset.return));
        if (t.dataset.edit) return openComponentSheet(componentById(t.dataset.edit));
        if (t.dataset.stockin) return openStockSheet(Number(t.dataset.stockin));
        if (t.dataset.del) return openDeleteSheet(Number(t.dataset.del));
        if (t.dataset.undo) return openUndoSheet(Number(t.dataset.undo));
        if (t.dataset.loss) return openLossSheet(Number(t.dataset.loss));
        if ('toggleGroup' in t.dataset) {
          const who = t.dataset.toggleGroup;
          if (state.loanOpen.has(who)) state.loanOpen.delete(who);
          else state.loanOpen.add(who);
          return renderLoans();
        }
        if (t.id === 'toggleAllLoans') {
          const who = Array.from(new Set(state.loans.map((l) => l.borrower)));
          const allOpen = who.every((n) => state.loanOpen.has(n));
          state.loanOpen = allOpen ? new Set() : new Set(who);
          return renderLoans();
        }
        if (t.dataset.lossReason) {
          state.lossFilter = t.dataset.lossReason;
          return renderLosses();
        }
        if (t.id === 'addLoss') return openLossSheet(null);
        if (t.dataset.revertBatch) {
          const b = state.batches.find((x) => x.id === Number(t.dataset.revertBatch));
          return revertBatch(Number(t.dataset.revertBatch), b && b.name);
        }
        if (t.dataset.filter) { state.logFilter = t.dataset.filter; return renderLog(); }
        if (t.dataset.example) {
          state.intake.text = t.dataset.example;
          const ta = $('#intakeText');
          if (ta) ta.value = t.dataset.example;
          doParse();
          return;
        }
        if (t.id === 'btnParse') return doParse();
        if (t.dataset.batch) return openBatchDetail(Number(t.dataset.batch));
      }
      const row = e.target.closest('.batchrow[data-batch]');
      if (row) openBatchDetail(Number(row.dataset.batch));
    });

    // 清单输入：存草稿 + 自动解析
    let parseTimer = null;
    document.addEventListener('input', (e) => {
      if (e.target.id !== 'intakeText') return;
      state.intake.text = e.target.value;
      clearTimeout(parseTimer);
      const hint = $('#parseHint');
      if (hint) hint.textContent = e.target.value.trim() ? '停顿一下会自动解析…' : '粘贴后会自动解析';
      parseTimer = setTimeout(doParse, 700);
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeSheet();
    });

    // 在入库页 Ctrl+V 粘贴截图
    document.addEventListener('paste', (e) => {
      if (state.tab !== 'intake') return;
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const item of items) {
        if (item.type && item.type.indexOf('image/') === 0) {
          const f = item.getAsFile();
          if (f) {
            e.preventDefault();
            handleFile(f);
            return;
          }
        }
      }
    });
  }

  /* ------------------------------------------------------------------ */
  async function boot() {
    bindEvents();
    try {
      await load();
      await loadBatches();
    } catch (err) {
      $('#view').innerHTML = emptyState('连不上服务', '请确认启动窗口还开着，然后刷新页面。');
      toast(err.message, true);
    }
  }

  boot();
})();
