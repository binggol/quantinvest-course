// 通用页面刷新按钮逻辑. 页面放:
//   <div class="rfbar"><button id="rfbtn" onclick="doRefresh('repo')">🔄 刷新数据</button> <span id="rfst"></span></div>
//   <script src="/static/refresh.js"></script>
// kind ∈ rsrs/ipo/repo/runup (走通用 /api/refresh); 'advisor_pro' 特例走已有 /api/advisor-pro/request.
(function () {
  function el(id) { return document.getElementById(id); }
  window.doRefresh = function (kind) {
    if (kind === 'advisor_pro') return doRefreshAdvisorPro();
    var btn = el('rfbtn'), st = el('rfst');
    if (btn) btn.disabled = true; if (st) st.textContent = '已通知PC...';
    fetch('/api/refresh/' + kind, { method: 'POST' }).then(function (r) { return r.json(); }).then(function (r) {
      if (st) st.textContent = r.message || '';
      if (window._rfT) clearInterval(window._rfT);
      window._rfT = setInterval(function () { pollRefresh(kind); }, 8000);
    }).catch(function () { if (st) st.textContent = '请求失败'; if (btn) btn.disabled = false; });
  };
  function pollRefresh(kind) {
    fetch('/api/refresh/status').then(function (r) { return r.json(); }).then(function (s) {
      var st = el('rfst'); if (!st) return;
      if (s.kind && s.kind !== kind) return;
      if (s.msg) st.textContent = 'PC: ' + s.msg;
      if (s.state === 'done') { clearInterval(window._rfT); st.textContent = '✅ 完成, 刷新中...'; setTimeout(function () { location.reload(); }, 1500); }
      if (s.state === 'error') { clearInterval(window._rfT); var b = el('rfbtn'); if (b) b.disabled = false; }
    }).catch(function () { });
  }
  // 一键批量生成 研报/预测 (按策略顾问Pro篮子 买入+持有). which ∈ 'report' | 'forecast'
  window.batchGen = function (which, codes) {
    var btn = el('batchgen'), st = el('bst');
    var body = which === 'forecast' ? { forecast: true } : { report: true };
    if (Array.isArray(codes) && codes.length) body.codes = codes;
    if (btn) btn.disabled = true; if (st) st.textContent = '提交中...';
    fetch('/api/batch_gen', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); }).then(function (r) {
        if (st) st.textContent = r.message || '';
        if (!r.ok) { if (btn) btn.disabled = false; return; }
        if (window._bgT) clearInterval(window._bgT);
        window._bgT = setInterval(pollBatch, 10000);
      }).catch(function () { if (st) st.textContent = '请求失败'; if (btn) btn.disabled = false; });
  };
  // 页面若提供 loadHist() (如个股研报页), 每次轮询都刷新历史列表, 让已完成的研报实时出现, 无需手动刷新.
  function refreshHist() { if (typeof window.loadHist === 'function') { try { window.loadHist(); } catch (e) { } } }
  function pollBatch() {
    fetch('/api/batch_gen/status').then(function (r) { return r.json(); }).then(function (s) {
      var st = el('bst');
      if (st && s.msg) st.textContent = s.msg + (s.n ? ' (' + (s.i || 0) + '/' + s.n + ')' : '');
      refreshHist();
      if (s.state === 'done') {
        clearInterval(window._bgT); window._bgT = null;
        var b = el('batchgen'); if (b) b.disabled = false;
        if (st) st.textContent = '✅ ' + (s.msg || '批量完成') + ' — 结果见下方列表, 已自动打开最新一只';
        refreshHist();
        // 让页面把批量结果真正"呈现"出来 (研报页: 刷新历史列表 + 自动打开最新一只)
        if (typeof window.onBatchDone === 'function') { try { window.onBatchDone(); } catch (e) {} }
      } else if (s.state === 'error') {
        clearInterval(window._bgT); window._bgT = null;
        var b2 = el('batchgen'); if (b2) b2.disabled = false;
        if (st) st.textContent = '❌ ' + (s.msg || '批量失败');
      }
    }).catch(function () { });
  }
  // 跨页面切换/刷新后, 自动重挂仍在 PC 后台运行的任务的进度轮询 (进度状态由 PC 回写到状态文件, 服务端持久, 与前端 setInterval 无关).
  // 任何引入 refresh.js 的页面在加载时都会自动恢复显示后台进度, 无需停留在原页面.
  function resumeJobs() {
    // 批量生成研报/预测: PC 串行跑, state==='running' 说明还没跑完 -> 重挂轮询并禁用按钮
    fetch('/api/batch_gen/status').then(function (r) { return r.json(); }).then(function (s) {
      if (!s || s.state !== 'running') return;
      var st = el('bst'); if (st) st.textContent = (s.msg || '批量生成中…') + (s.n ? ' (' + (s.i || 0) + '/' + s.n + ')' : '');
      var b = el('batchgen'); if (b) b.disabled = true;
      refreshHist();
      if (window._bgT) clearInterval(window._bgT);
      window._bgT = setInterval(pollBatch, 10000);
    }).catch(function () { });
    // 通用刷新 (rsrs/ipo/repo/runup/...): state==='running' -> 按状态里的 kind 重挂轮询
    fetch('/api/refresh/status').then(function (r) { return r.json(); }).then(function (s) {
      if (!s || s.state !== 'running' || !s.kind) return;
      var st = el('rfst'); if (st) st.textContent = 'PC: ' + (s.msg || '处理中…');
      var b = el('rfbtn'); if (b) b.disabled = true;
      if (window._rfT) clearInterval(window._rfT);
      window._rfT = setInterval(function () { pollRefresh(s.kind); }, 8000);
    }).catch(function () { });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', resumeJobs);
  else resumeJobs();
  // track / advisor-pro: 复用已有重算(重算 regime_advisor_pro, 较重 ~1-2分钟)
  function doRefreshAdvisorPro() {
    var btn = el('rfbtn'), st = el('rfst');
    if (btn) btn.disabled = true; if (st) st.textContent = '已通知PC重算...';
    fetch('/api/advisor-pro/request', { method: 'POST' }).then(function (r) { return r.json(); }).then(function (r) {
      if (st) st.textContent = (r.message || '已通知PC重算(约1-2分钟)') + ' — 稍后手动刷新本页';
      setTimeout(function () { if (btn) btn.disabled = false; }, 60000);
    }).catch(function () { if (st) st.textContent = '请求失败'; if (btn) btn.disabled = false; });
  }
})();
