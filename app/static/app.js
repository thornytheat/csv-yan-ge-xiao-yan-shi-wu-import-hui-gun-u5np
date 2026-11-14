// 原生 JS：无框架、无打包器、无 CDN。仅做「表头自检」「重复导入验证」「提交防抖」三件事。
(function () {
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  window.postJSON = async function (url, payload) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await res.json(); } catch (e) { data = { detail: '响应不是 JSON' }; }
    if (!res.ok) {
      var msg = (data && (data.detail || data.message)) || ('请求失败：' + res.status);
      throw new Error(msg);
    }
    return data;
  };

  window.showError = function (host, err) {
    if (!host) return;
    host.hidden = false;
    host.textContent = '执行失败：' + (err && err.message ? err.message : String(err));
  };

  // ---- 表头自检（模块 2 的独立入口）
  var contractBtn = qs('#contract-btn');
  var contractExtraBtn = qs('#contract-extra-btn');
  var contractOut = qs('#contract-result');
  var contractErr = qs('#contract-error');

  async function checkHeader(headerText) {
    if (!contractOut) return;
    if (contractErr) contractErr.hidden = true;
    contractOut.innerHTML = '<span class="muted">检查中…</span>';
    var policy = qs('#extra_policy') ? qs('#extra_policy').value : 'reject';
    try {
      var data = await window.postJSON('/api/contract-check', {
        header: headerText,
        extra_policy: policy
      });
      var rows = ['<p><b>' + (data.ok ? '契约通过' : '契约未通过') + '</b>'
        + '<span class="muted">（策略：' + esc(data.extra_policy) + '，列顺序无关）</span></p>'];

      if (!data.ok) {
        rows.push('<table><thead><tr><th>错误</th><th>说明</th></tr></thead><tbody>');
        (data.errors || []).forEach(function (e) {
          rows.push('<tr><td><code>' + esc(e.code) + '</code></td><td>' + esc(e.message) + '</td></tr>');
        });
        rows.push('</tbody></table>');
      }
      if ((data.warnings || []).length) {
        rows.push('<table><thead><tr><th>告警</th><th>说明</th></tr></thead><tbody>');
        data.warnings.forEach(function (e) {
          rows.push('<tr><td><code>' + esc(e.code) + '</code></td><td>' + esc(e.message) + '</td></tr>');
        });
        rows.push('</tbody></table>');
      }
      rows.push('<p class="muted">解析到的列：' + esc((data.header || []).join(' | ')) + '</p>');
      contractOut.innerHTML = rows.join('');
    } catch (err) {
      contractOut.innerHTML = '';
      window.showError(contractErr, err);
    }
  }

  if (contractBtn) {
    contractBtn.addEventListener('click', function () {
      var input = qs('#header-input');
      checkHeader(input ? input.value : '');
    });
  }
  if (contractExtraBtn) {
    contractExtraBtn.addEventListener('click', function () {
      var input = qs('#header-input');
      if (input) input.value = 'site,category,order_id,order_date,amount,qty,note';
      checkHeader('site,category,order_id,order_date,amount,qty,note');
    });
  }

  // ---- 重复导入验证（幂等复核）
  qsa('[data-replay-sha]').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      var host = qs('#replay-result');
      var errHost = qs('#replay-error');
      if (errHost) errHost.hidden = true;
      btn.disabled = true;
      try {
        var data = await window.postJSON('/api/replay-check', { sha256: btn.getAttribute('data-replay-sha') });
        if (host) {
          host.hidden = false;
          host.innerHTML = '<b>' + esc(btn.getAttribute('data-batch')) + '</b> → '
            + '<span class="tag ' + (data.verdict === 'duplicate_file' ? 'warn' : 'ok') + '">'
            + esc(data.verdict) + '</span> ' + esc(data.message)
            + ' <span class="muted">当前明细 ' + esc(data.fact_rows) + ' 行</span>';
        }
      } catch (err) {
        window.showError(errHost, err);
      } finally {
        btn.disabled = false;
      }
    });
  });

  // ---- 上传表单：提交后禁用按钮并提示，避免重复点击触发两次上传
  var form = qs('#upload-form');
  if (form) {
    form.addEventListener('submit', function () {
      var btn = qs('#submit-btn');
      var hint = qs('#upload-hint');
      if (btn) { btn.disabled = true; btn.textContent = '校验中…'; }
      if (hint) hint.textContent = '正在探测编码、校验契约与逐行规则，请稍候。';
    });
  }

  // ---- 结果页：分段标签（聚合 / 探测证据 / 明细）
  qsa('[data-tab-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('.segmented, .tabs');
      if (group) {
        qsa('button', group).forEach(function (b) { b.classList.remove('active'); });
      }
      btn.classList.add('active');
      var host = btn.closest('.panel') || document;
      qsa('.tab-panel', host).forEach(function (p) {
        p.hidden = p.id !== btn.getAttribute('data-tab-target');
      });
    });
  });

  // ---- 复核页：搜索/筛选 + 选中批次在右侧看回执详情
  var reviewSearch = qs('#review-search');
  var reviewScope = qs('#review-scope');
  var reviewEncoding = qs('#review-encoding');
  var batchesTable = qs('#batches-table');
  var attemptsTable = qs('#attempts-table');
  var reviewStat = qs('#review-stat');

  function applyReviewFilters() {
    if (!batchesTable) return;
    var term = (reviewSearch ? reviewSearch.value : '').trim().toLowerCase();
    var scope = reviewScope ? reviewScope.value : '';
    var enc = reviewEncoding ? reviewEncoding.value : '';
    var shown = 0;
    var total = 0;
    qsa('tr[data-batch]', batchesTable).forEach(function (row) {
      total += 1;
      var text = (row.getAttribute('data-search') || '').toLowerCase();
      var ok = (!term || text.indexOf(term) >= 0)
        && (!enc || (row.getAttribute('data-encoding') || '').indexOf(enc) === 0)
        && (!scope || row.getAttribute('data-' + scope) === '1');
      row.hidden = !ok;
      if (ok) shown += 1;
    });
    if (attemptsTable) {
      qsa('tr[data-search]', attemptsTable).forEach(function (row) {
        var text = (row.getAttribute('data-search') || '').toLowerCase();
        row.hidden = !!term && text.indexOf(term) < 0;
      });
    }
    if (reviewStat) reviewStat.textContent = '共 ' + shown + ' / ' + total + ' 条批次';
  }

  [reviewSearch, reviewScope, reviewEncoding].forEach(function (el) {
    if (!el) return;
    el.addEventListener('input', applyReviewFilters);
    el.addEventListener('change', applyReviewFilters);
  });

  function showDetail(row) {
    var fields = { batch: '#detail-batch', filename: '#detail-filename', encoding: '#detail-encoding',
      rows: '#detail-rows', amount: '#detail-amount', time: '#detail-time', sha: '#detail-sha' };
    Object.keys(fields).forEach(function (key) {
      var el = qs(fields[key]);
      var value = row.getAttribute('data-' + key);
      if (el && value != null) el.textContent = value;
    });
    var link = qs('#detail-link');
    if (link) link.setAttribute('href', row.getAttribute('data-href') || '/workbench');
    var tag = qs('#detail-tag');
    if (tag) {
      tag.textContent = row.getAttribute('data-forced') === '1' ? 'forced 重灌'
        : (row.getAttribute('data-superseded') === '1' ? '已被替代' : '已入库');
    }
  }

  if (batchesTable) {
    qsa('tr[data-batch]', batchesTable).forEach(function (row) {
      row.addEventListener('click', function (ev) {
        if (ev.target.closest('a, button')) return;
        qsa('tr[data-batch]', batchesTable).forEach(function (r) { r.classList.remove('selected'); });
        row.classList.add('selected');
        showDetail(row);
      });
    });
  }
})();
