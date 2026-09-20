/**
 * 量化验证页面（向量化回测 / 市场状态开关 / 参数扫描）
 *
 * 对应后端蓝图：/api/vector（vector_bt/routes.py）
 * 页面 DOM：index.html 中 id="vector-page"
 */
(function () {
  const API = '/api/vector';
  let chart = null;
  let booted = false;
  let sweepTimer = null;

  const $ = (id) => document.getElementById(id);
  const fmtPct = (v, digits = 2) => (v === null || v === undefined || isNaN(v)) ? '--' : (v * 100).toFixed(digits) + '%';
  const fmtNum = (v, digits = 2) => (v === null || v === undefined || isNaN(v)) ? '--' : Number(v).toFixed(digits);
  const pctCell = (v) => (v > 0 ? 'color:#c62828;' : (v < 0 ? 'color:#2e7d32;' : ''));

  async function getJSON(url, options) {
    const res = await fetch(url, options);
    const data = await res.json();
    if (!data.success) throw new Error(data.error || '请求失败');
    return data.data;
  }

  // ---------- 市场状态开关 ----------
  async function loadRegime() {
    const stateEl = $('vr-state');
    if (stateEl) stateEl.textContent = '加载中...';
    try {
      const d = await getJSON(`${API}/regime`);
      $('vr-date').textContent = d.date || '--';
      $('vr-ratio').textContent = fmtPct(d.limit_up_ma, 3);
      $('vr-threshold').textContent = fmtPct(d.threshold, 3);
      const active = !!d.active;
      stateEl.textContent = active ? '高活跃 · 可开仓' : '低活跃 · 建议空仓';
      stateEl.style.color = active ? '#c62828' : '#2e7d32';
      $('vr-advice').textContent = d.advice || '';
      return d;
    } catch (err) {
      stateEl.textContent = '加载失败';
      $('vr-advice').textContent = err.message;
      return null;
    }
  }

  // ---------- 策略清单 ----------
  async function loadStrategies() {
    const box = $('vector-strategy-list');
    try {
      const d = await getJSON(`${API}/strategies`);
      const list = d.strategies || [];
      box.innerHTML = list.map((name, i) => `
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
          <input type="checkbox" class="vector-strategy-cb" value="${name}" ${name.includes('涨停横盘') ? 'checked' : ''}>
          <span>${name}</span>
        </label>`).join('');
      const sel = $('vector-timing');
      if (sel && sel.options.length <= 1) {
        (d.timing_strategies || []).forEach((t) => {
          const opt = document.createElement('option');
          opt.value = t.id;
          opt.textContent = t.name;
          sel.appendChild(opt);
        });
        const comboBox = $('vector-combo-timings');
        if (comboBox) {
          comboBox.innerHTML = (d.timing_strategies || []).map((t) => `
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
              <input type="checkbox" class="vector-combo-timing-cb" value="${t.id}"
                ${t.id === 'bollinger' ? 'checked' : ''}>
              <span>${t.name}</span>
            </label>`).join('');
        }
      }
    } catch (err) {
      box.innerHTML = `<span style="color:#c00;">加载失败：${err.message}</span>`;
    }
  }

  function selectedStrategies() {
    return Array.from(document.querySelectorAll('.vector-strategy-cb:checked')).map((cb) => cb.value);
  }

  // ---------- 回测 ----------
  async function runBacktest() {
    const status = $('vector-status');
    const strategies = selectedStrategies();
    if (!strategies.length) { status.textContent = '请至少选择一个策略'; return; }
    const regime = $('vector-regime').value;
    const payload = {
      strategies,
      start: $('vector-start').value,
      end: $('vector-end').value,
      hold_period: parseInt($('vector-hold').value, 10) || 10,
      max_daily_buys: parseInt($('vector-buys').value, 10) || 8,
      stop_loss: (parseFloat($('vector-stop').value) || -7) / 100,
      take_profit: (parseFloat($('vector-take').value) || 21) / 100,
      regime_filter: regime,
      timing: $('vector-timing') ? ($('vector-timing').value || null) : null,
      regime_fixed: regime === 'fixed' ? 0.01557 : null,
      save: $('vector-save') ? $('vector-save').checked : false,
      backtest_name: `向量化回测 ${$('vector-start').value}~${$('vector-end').value}` +
        (regime === 'off' ? '（无过滤）' : regime === 'rolling' ? '（滚动过滤）' : '（固定过滤）'),
    };
    if (!payload.start || !payload.end) { status.textContent = '请选择回测区间'; return; }
    status.textContent = '回测中...（命中缓存时只需几秒；首次会先生成信号，可能较久）';
    $('vector-result').style.display = 'none';
    try {
      const d = await getJSON(`${API}/backtest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      renderRanking(d.ranking || []);
      renderEquity(d.curves || {});
      $('vector-result').style.display = 'block';
      status.textContent = `完成：${(d.ranking || []).length} 个策略`;
    } catch (err) {
      status.textContent = '失败：' + err.message;
    }
  }

  const RANK_COLS = [
    ['策略', (r) => r['策略']],
    ['累计收益', (r) => fmtPct(r['累计收益'])],
    ['年化收益', (r) => fmtPct(r['年化收益'])],
    ['基准年化', (r) => fmtPct(r['基准年化'])],
    ['超额年化', (r) => fmtPct(r['超额年化'])],
    ['信息比率', (r) => fmtNum(r['信息比率'])],
    ['夏普', (r) => fmtNum(r['夏普'])],
    ['最大回撤', (r) => fmtPct(r['最大回撤'])],
    ['交易笔数', (r) => r['交易笔数']],
    ['逐笔胜率', (r) => fmtPct(r['逐笔胜率'])],
  ];

  function renderRanking(rows) {
    const head = $('vector-result-head');
    const body = $('vector-result-body');
    head.innerHTML = RANK_COLS.map(([label]) =>
      `<th style="text-align:left;padding:8px;border:1px solid #e0e0e0;">${label}</th>`).join('');
    body.innerHTML = rows.map((r) => '<tr>' + RANK_COLS.map(([, getter]) => {
      const v = getter(r);
      const style = (String(v).includes('%') && r['超额年化'] !== undefined && String(v) === fmtPct(r['超额年化']))
        ? pctCell(r['超额年化']) : '';
      return `<td style="padding:8px;border:1px solid #e0e0e0;${style}">${v === null || v === undefined ? '--' : v}</td>`;
    }).join('') + '</tr>').join('');
  }

  function renderEquity(curves) {
    const ctx = $('vector-equity-chart');
    if (!ctx || typeof Chart === 'undefined') return;
    const names = Object.keys(curves);
    if (!names.length) return;
    const labels = curves[names[0]].dates;
    const palette = ['#1677ff', '#c62828', '#2e7d32', '#f9a825', '#6a1b9a', '#00838f'];
    const datasets = names.map((name, i) => ({
      label: name,
      data: curves[name].values,
      borderColor: palette[i % palette.length],
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.1,
    }));
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'bottom' } },
        scales: { x: { ticks: { maxTicksLimit: 10 } }, y: { ticks: { maxTicksLimit: 8 } } },
      },
    });
  }

  // ---------- 参数扫描 ----------
  async function runCombo() {
    const status = $('vector-combo-status');
    const strategies = selectedStrategies();
    if (!strategies.length) { status.textContent = '请先在上方选择选股策略'; return; }
    const timings = Array.from(document.querySelectorAll('.vector-combo-timing-cb:checked')).map((cb) => cb.value);
    const payload = {
      strategies,
      timings,
      start: $('vector-start').value,
      end: $('vector-end').value,
      signal_start: $('vector-start').value,
      signal_end: $('vector-end').value,
      window: 120,
      regime_filter: $('vector-regime').value,
      regime_fixed: $('vector-regime').value === 'fixed' ? 0.01557 : null,
      hold_period: parseInt($('vector-hold').value, 10) || 10,
      max_daily_buys: parseInt($('vector-buys').value, 10) || 8,
      stop_loss: (parseFloat($('vector-stop').value) || -7) / 100,
      take_profit: (parseFloat($('vector-take').value) || 21) / 100,
    };
    if (!payload.start || !payload.end) { status.textContent = '请先选择回测区间'; return; }
    const total = strategies.length * (timings.length + 1);
    status.textContent = `提交中...（共 ${total} 组，预计 ${Math.ceil(total * 45 / 60)} 分钟）`;
    $('vector-combo-result').style.display = 'none';
    try {
      const d = await getJSON(`${API}/combo`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      $('vector-combo-progress').style.display = 'block';
      pollCombo(d.task_id, total);
    } catch (err) {
      status.textContent = '失败：' + err.message;
    }
  }

  function pollCombo(taskId, total) {
    if (sweepTimer) clearInterval(sweepTimer);
    sweepTimer = setInterval(async () => {
      try {
        const d = await getJSON(`${API}/task/${taskId}`);
        if (d.status === 'running' || d.status === 'pending') {
          $('vector-combo-bar').style.width = '50%';
          return;
        }
        clearInterval(sweepTimer);
        sweepTimer = null;
        $('vector-combo-bar').style.width = '100%';
        if (d.status === 'completed') {
          const rows = d.result.rows || [];
          $('vector-combo-status').textContent = `完成：${rows.length} 组组合`;
          renderCombo(rows);
        } else {
          $('vector-combo-status').textContent = '失败：' + (d.error || '未知错误');
        }
      } catch (err) {
        clearInterval(sweepTimer);
        sweepTimer = null;
        $('vector-combo-status').textContent = '查询失败：' + err.message;
      }
    }, 5000);
  }

  const COMBO_COLS = [
    ['选股策略', (r) => r['选股策略']],
    ['择时策略', (r) => r['择时策略']],
    ['年化收益', (r) => fmtPct(r['年化收益'])],
    ['超额年化', (r) => fmtPct(r['超额年化'])],
    ['信息比率', (r) => fmtNum(r['信息比率'])],
    ['最大回撤', (r) => fmtPct(r['最大回撤'])],
    ['交易笔数', (r) => r['交易笔数']],
    ['逐笔胜率', (r) => fmtPct(r['逐笔胜率'])],
  ];

  function renderCombo(rows) {
    $('vector-combo-head').innerHTML = COMBO_COLS.map(([label]) =>
      `<th style="text-align:left;padding:8px;border:1px solid #e0e0e0;">${label}</th>`).join('');
    $('vector-combo-body').innerHTML = rows.map((r) => '<tr>' + COMBO_COLS.map(([, getter]) => {
      const v = getter(r);
      const style = String(v) === fmtPct(r['超额年化']) ? pctCell(r['超额年化']) : '';
      return `<td style="padding:8px;border:1px solid #e0e0e0;${style}">${v === null || v === undefined ? '--' : v}</td>`;
    }).join('') + '</tr>').join('');
    $('vector-combo-result').style.display = 'block';
  }

  async function runSweep() {
    const status = $('vector-sweep-status');
    const strategies = selectedStrategies();
    if (!strategies.length) { status.textContent = '请先在上方选择策略'; return; }
    const payload = {
      strategies,
      start: $('vector-start').value,
      end: $('vector-end').value,
    };
    if (!payload.start || !payload.end) { status.textContent = '请先选择回测区间'; return; }
    status.textContent = '提交中...';
    $('vector-sweep-result').style.display = 'none';
    try {
      const d = await getJSON(`${API}/sweep`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      status.textContent = '扫描中...（可离开本页，稍后回来查看）';
      $('vector-sweep-progress').style.display = 'block';
      pollSweep(d.task_id);
    } catch (err) {
      status.textContent = '失败：' + err.message;
    }
  }

  function pollSweep(taskId) {
    if (sweepTimer) clearInterval(sweepTimer);
    const bar = $('vector-sweep-bar');
    sweepTimer = setInterval(async () => {
      try {
        const d = await getJSON(`${API}/task/${taskId}`);
        if (d.status === 'running') {
          bar.style.width = '60%';
          return;
        }
        clearInterval(sweepTimer);
        sweepTimer = null;
        bar.style.width = '100%';
        if (d.status === 'completed') {
          $('vector-sweep-status').textContent = `完成：${(d.result.rows || []).length} 个参数组合`;
          renderSweep(d.result.rows || []);
        } else {
          $('vector-sweep-status').textContent = '失败：' + (d.error || '未知错误');
        }
      } catch (err) {
        clearInterval(sweepTimer);
        sweepTimer = null;
        $('vector-sweep-status').textContent = '查询失败：' + err.message;
      }
    }, 5000);
  }

  const SWEEP_COLS = [
    ['策略', (r) => r['策略']],
    ['止损', (r) => fmtPct(r['止损'], 0)],
    ['止盈', (r) => fmtPct(r['止盈'], 0)],
    ['持有期', (r) => r['持有期']],
    ['年化收益', (r) => fmtPct(r['年化收益'])],
    ['超额年化', (r) => fmtPct(r['超额年化'])],
    ['信息比率', (r) => fmtNum(r['信息比率'])],
    ['最大回撤', (r) => fmtPct(r['最大回撤'])],
    ['交易笔数', (r) => r['交易笔数']],
  ];

  function renderSweep(rows) {
    const sorted = rows.slice().sort((a, b) => (b['超额年化'] || 0) - (a['超额年化'] || 0)).slice(0, 30);
    $('vector-sweep-head').innerHTML = SWEEP_COLS.map(([label]) =>
      `<th style="text-align:left;padding:8px;border:1px solid #e0e0e0;">${label}</th>`).join('');
    $('vector-sweep-body').innerHTML = sorted.map((r) => '<tr>' + SWEEP_COLS.map(([, getter]) => {
      const v = getter(r);
      const style = String(v) === fmtPct(r['超额年化']) ? pctCell(r['超额年化']) : '';
      return `<td style="padding:8px;border:1px solid #e0e0e0;${style}">${v === null || v === undefined ? '--' : v}</td>`;
    }).join('') + '</tr>').join('');
    $('vector-sweep-result').style.display = 'block';
  }

  // ---------- 初始化 ----------
  function setDefaultDates(latestDate) {
    const end = latestDate || new Date().toISOString().slice(0, 10);
    const start = `${parseInt(end.slice(0, 4), 10) - 3}${end.slice(4)}`;
    if (!$('vector-end').value) $('vector-end').value = end;
    if (!$('vector-start').value) $('vector-start').value = start;
  }

  async function boot() {
    if (booted) return;
    booted = true;
    $('vector-regime-refresh').addEventListener('click', loadRegime);
    $('vector-run-btn').addEventListener('click', runBacktest);
    $('vector-sweep-btn').addEventListener('click', runSweep);
    if ($('vector-combo-btn')) $('vector-combo-btn').addEventListener('click', runCombo);
    await loadStrategies();
    const d = await loadRegime();
    setDefaultDates(d && d.date);
  }

  window.loadVectorPage = boot;
  document.addEventListener('click', (e) => {
    const item = e.target.closest && e.target.closest('.nav-item[data-page="vector"]');
    if (item) setTimeout(boot, 0);
  });
  if (document.readyState !== 'loading') {
    if (location.hash === '#vector') boot();
  } else {
    document.addEventListener('DOMContentLoaded', () => { if (location.hash === '#vector') boot(); });
  }
})();
