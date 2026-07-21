(function(){
  const cache = new Map();
  const STYLE_ID = "money-flow-spark-style";

  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
    }[c]));
  }

  function codeForApi(code){
    const raw = String(code || "").trim();
    const six = (raw.match(/\d{6}/) || [""])[0];
    if (!six) return raw;
    if (/^(sh|sz|bj)/i.test(raw)) return raw;
    if (/\.SH$/i.test(raw)) return "sh" + six;
    if (/\.SZ$/i.test(raw)) return "sz" + six;
    if (/\.BJ$/i.test(raw)) return "bj" + six;
    return (six[0] === "6" ? "sh" : ((six[0] === "4" || six[0] === "8") ? "bj" : "sz")) + six;
  }

  function flowColor(v){
    const n = Number(v);
    return n > 0 ? "#22c55e" : (n < 0 ? "#ef4444" : "#94a3b8");
  }

  function kColor(row){
    if (!row || !Number.isFinite(row.open) || !Number.isFinite(row.close)) return "#94a3b8";
    return row.close >= row.open ? "#ef4444" : "#22c55e";
  }

  function encFlows(flows){
    try { return encodeURIComponent(JSON.stringify(flows || [])); }
    catch(e){ return "%5B%5D"; }
  }

  function decFlows(raw){
    try { return JSON.parse(decodeURIComponent(raw || "%5B%5D")); }
    catch(e){ return []; }
  }

  function ensureStyle(){
    if (document.getElementById(STYLE_ID)) return;
    const st = document.createElement("style");
    st.id = STYLE_ID;
    st.textContent = `
      .mf-spark{width:320px;height:118px;margin-top:3px;display:block}
      .mf-spark svg{width:320px;height:118px;display:block;overflow:visible}
      .mf-spark .mf-loading{color:#64748b;font-size:10px}
      .mf-spark text{font-family:Arial, sans-serif}
    `;
    document.head.appendChild(st);
  }

  function sparkHtml(code, moneyOutflow){
    const flows = (moneyOutflow && moneyOutflow.outflow_10d_daily) || [];
    if (!code || !flows.length) return "";
    return `<span class="mf-spark" data-code="${esc(code)}" data-flows="${encFlows(flows)}"><span class="mf-loading">K线/资金流加载中...</span></span>`;
  }

  async function loadKline(code){
    const key = codeForApi(code);
    if (cache.has(key)) return cache.get(key);
    const p = fetch(`/api/kline?code=${encodeURIComponent(key)}&days=15&adjust=qfq&refresh=0`, {cache: "no-store"})
      .then(async r => {
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
        if (!window.QIKline) throw new Error("K线校验组件未加载");
        window.QIKline.assertPayload(data, "qfq");
        return data;
      })
      .catch(e => ({message: e.message || String(e)}));
    cache.set(key, p);
    return p;
  }

  function valueAt(arr, idx){
    const v = Array.isArray(arr) ? Number(arr[idx]) : NaN;
    return Number.isFinite(v) ? v : NaN;
  }

  function renderSpark(el, kline, flows){
    const contract = window.QIKline.assertPayload(kline, "qfq");
    const dates = Array.isArray(kline.dates) ? kline.dates : [];
    const flowByDate = new Map((flows || []).map(x => [String(x.date || "").slice(0, 10), Number(x.outflow_net_yi)]));
    const flowDates = (flows || []).map(x => String(x.date || "").slice(0, 10)).filter(Boolean).slice(-15);
    const rows = flowDates.map(d => {
      const idx = dates.indexOf(d);
      return {
        date: d,
        open: valueAt(kline.open, idx),
        high: valueAt(kline.high, idx),
        low: valueAt(kline.low, idx),
        close: valueAt(kline.close, idx),
        flow: flowByDate.get(d),
      };
    });
    const fallbackDates = dates.slice(-15);
    const fallback = fallbackDates.map(d => {
      const idx = dates.indexOf(d);
      return {
        date: d,
        open: valueAt(kline.open, idx),
        high: valueAt(kline.high, idx),
        low: valueAt(kline.low, idx),
        close: valueAt(kline.close, idx),
        flow: flowByDate.get(d),
      };
    });
    const data = rows.some(x => Number.isFinite(x.close)) ? rows : fallback;
    if (!data.length || !data.some(x => Number.isFinite(x.close))) {
      el.innerHTML = '<span class="mf-loading">无K线</span>';
      return;
    }

    const W = 320, H = 118, L = 26, R = 8, T = 10, MID = 70, B = 16;
    const n = data.length;
    const prices = data.flatMap(x => [x.high, x.low, x.open, x.close]).filter(Number.isFinite);
    const mn = Math.min(...prices), mx = Math.max(...prices);
    const span = (mx - mn) || Math.max(1, Math.abs(mx) * 0.02);
    const maxAbsFlow = Math.max(0.01, ...data.map(x => Math.abs(Number(x.flow) || 0)));
    const step = n <= 1 ? 0 : (W - L - R) / (n - 1);
    const candleW = Math.max(5, Math.min(13, step * 0.58));
    const x = i => L + (n <= 1 ? 0 : i * step);
    const yPrice = v => T + (mx - v) / span * (MID - T - 6);
    const barBase = H - B;
    const barScale = (H - B - MID - 7) / maxAbsFlow;

    const candles = data.map((d, i) => {
      if (![d.open, d.high, d.low, d.close].every(Number.isFinite)) return "";
      const cx = x(i);
      const color = kColor(d);
      const yH = yPrice(d.high), yL = yPrice(d.low), yO = yPrice(d.open), yC = yPrice(d.close);
      const top = Math.min(yO, yC);
      const bodyH = Math.max(1.2, Math.abs(yC - yO));
      const title = `${d.date} O:${d.open.toFixed(2)} H:${d.high.toFixed(2)} L:${d.low.toFixed(2)} C:${d.close.toFixed(2)} 净流出:${Number.isFinite(Number(d.flow)) ? Number(d.flow).toFixed(2) : "-"}亿`;
      return `<g><title>${esc(title)}</title><line x1="${cx.toFixed(1)}" y1="${yH.toFixed(1)}" x2="${cx.toFixed(1)}" y2="${yL.toFixed(1)}" stroke="${color}" stroke-width="1"/><rect x="${(cx-candleW/2).toFixed(1)}" y="${top.toFixed(1)}" width="${candleW.toFixed(1)}" height="${bodyH.toFixed(1)}" rx="1" fill="${color}" fill-opacity="0.86" stroke="${color}"/></g>`;
    }).join("");

    const bars = data.map((d, i) => {
      const v = Number(d.flow) || 0;
      const h = Math.max(1, Math.abs(v) * barScale);
      const bx = x(i) - candleW / 2, by = v >= 0 ? barBase - h : barBase;
      const title = `${d.date} 净流出 ${Number.isFinite(v) ? v.toFixed(2) : "-"}亿`;
      return `<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${candleW.toFixed(1)}" height="${h.toFixed(1)}" rx="1.5" fill="${flowColor(v)}"><title>${esc(title)}</title></rect>`;
    }).join("");
    const labels = data.map((d, i) => i === 0 || i === data.length - 1 ? `<text x="${x(i).toFixed(1)}" y="${H-2}" fill="#64748b" font-size="9" text-anchor="${i === 0 ? "start" : "end"}">${esc(d.date.slice(5))}</text>` : "").join("");
    const last = data[data.length - 1] || {};
    const lastFlow = Number(last.flow);
    const lastClose = Number(last.close);
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="15日前复权K线与资金净流出${contract.qualityNotice ? `，${contract.qualityNotice}` : ""}">
      <title>15日前复权K线与资金净流出${contract.qualityNotice ? ` · ${contract.qualityNotice}` : ""}</title>
      <line x1="${L}" y1="${MID}" x2="${W-R}" y2="${MID}" stroke="#1e293b"/>
      <line x1="${L}" y1="${barBase}" x2="${W-R}" y2="${barBase}" stroke="#334155"/>
      ${candles}
      ${bars}
      <text x="2" y="13" fill="#cbd5e1" font-size="10">K</text>
      ${contract.ohlcEnvelopeRepairs ? `<text x="14" y="13" fill="#fbbf24" font-size="9">修${contract.ohlcEnvelopeRepairs}</text>` : ""}
      <text x="2" y="${barBase + 3}" fill="#94a3b8" font-size="10">流</text>
      <text x="${W-R}" y="12" fill="${flowColor(lastFlow)}" font-size="10" text-anchor="end">${Number.isFinite(lastFlow) ? lastFlow.toFixed(2) + "亿" : ""}</text>
      <text x="${W-R}" y="25" fill="#cbd5e1" font-size="10" text-anchor="end">${Number.isFinite(lastClose) ? lastClose.toFixed(2) : ""}</text>
      ${labels}
    </svg>`;
  }

  async function hydrate(){
    ensureStyle();
    const nodes = Array.from(document.querySelectorAll(".mf-spark:not([data-loaded])"));
    for (const el of nodes) {
      el.setAttribute("data-loaded", "1");
      const code = el.getAttribute("data-code");
      const flows = decFlows(el.getAttribute("data-flows"));
      const kline = await loadKline(code);
      if (!kline || kline.message || !Array.isArray(kline.dates)) {
        el.innerHTML = '<span class="mf-loading">K线缺失</span>';
        continue;
      }
      renderSpark(el, kline, flows);
    }
  }

  let timer = null;
  function schedule(){
    clearTimeout(timer);
    timer = setTimeout(hydrate, 80);
  }

  window.moneyFlowSparkHtml = sparkHtml;
  window.hydrateMoneyFlowSparks = hydrate;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", schedule);
  else schedule();
  new MutationObserver(schedule).observe(document.documentElement, {childList: true, subtree: true});
})();
