(function (global) {
  "use strict";

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char];
    });
  }

  function numberOrNull(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function metricText(value, percent) {
    const number = numberOrNull(value);
    if (number === null) return "—";
    return percent ? `${(number * 100).toFixed(4)}%` : number.toFixed(6);
  }

  function metricPair(current, baseline, percent, higherIsBetter) {
    const currentNumber = numberOrNull(current);
    const baselineNumber = numberOrNull(baseline);
    let state = "mine-metric-neutral";
    if (currentNumber !== null && baselineNumber !== null && currentNumber !== baselineNumber) {
      const improved = higherIsBetter ? currentNumber > baselineNumber : currentNumber < baselineNumber;
      state = improved ? "mine-metric-better" : "mine-metric-worse";
    }
    return `<span class="${state}">${metricText(current, percent)}</span>` +
      `<span class="mine-metric-base"> / ${metricText(baseline, percent)}</span>`;
  }

  function summaryFactors(factors, limit) {
    const values = Array.isArray(factors) ? factors : [];
    const shown = values.slice(0, limit || 6).map(escapeHtml).join(", ");
    return shown + (values.length > (limit || 6) ? ` …(${values.length})` : "");
  }

  function evaluatedFactors(run) {
    if (Array.isArray(run.evaluated_factors) && run.factor_count_exact === true) {
      return run.evaluated_factors;
    }
    return Array.isArray(run.factors) ? run.factors : [];
  }

  function factorCountText(run) {
    if (run.factor_count_exact === true) {
      const tested = Number(run.n_evaluated_factors || 0);
      const untested = Number(run.n_unevaluated_factors || 0);
      return untested > 0 ? `${tested}已测 + ${untested}未测` : `${tested}已测`;
    }
    return String(Number(run.n_factors || 0));
  }

  function fixedText(value, digits) {
    const number = numberOrNull(value);
    return number === null ? "—" : number.toFixed(digits == null ? 4 : digits);
  }

  function screenErrorText(error) {
    return {
      missing: "精确终筛产物缺失",
      scope_mismatch: "终筛范围不匹配",
      universe_mismatch: "股票池不匹配",
      workspace_mismatch: "候选身份不匹配",
      count_mismatch: "因子计数不一致",
      distinct_count_mismatch: "去重计数不一致",
      passed_factor_mismatch: "过关因子清单不一致",
      artifact_mismatch: "终筛归档身份不一致",
      inline_invalid: "本轮终筛快照无效"
    }[String(error || "")] || "精确终筛产物无效";
  }

  function candidateStatusText(candidate, screen) {
    const status = String(candidate.status || "pending");
    const reason = String(candidate.terminal_reason || "");
    if (status === "no_factors") {
      if (!screen || screen.valid !== true) return "终筛记录不完整";
      if (reason === "factor_analysis_no_pass") return "FDR与精确门交集为空";
      return "精确终筛无通过";
    }
    if (status === "completed") return "已完成生产门回测";
    if (status === "failed") return "评估失败";
    if (status === "running") return "评估中";
    return "待评估";
  }

  function factorFailureReasons(row) {
    if (row.pass === true) return "通过";
    const reasons = [];
    if (row.redundant_with) reasons.push(`与 ${String(row.redundant_with)} 冗余`);
    if (row.base_pass === false) reasons.push("基础增量门未过");
    if (row.decay_pass === false) reasons.push("衰减门未过");
    if (row.suspect === true) reasons.push("数值可疑");
    if (row.style_proxy === true) reasons.push("风格代理");
    const residIc = numberOrNull(row.resid_ic);
    const gain = numberOrNull(row.gain);
    if (residIc !== null && residIc <= 0) reasons.push("残差IC≤0");
    if (gain !== null && gain <= 0) reasons.push("增量≤0");
    return reasons.length ? reasons.join("；") : "联合门未过";
  }

  function renderExactFactorRows(screen) {
    const factors = Array.isArray(screen.factors) ? screen.factors : [];
    if (!factors.length) {
      return `<div class="mine-history-no-detail">该候选没有可展示的逐因子终筛行。</div>`;
    }
    let html = `<div class="mine-backtest-scroll"><table class="mine-screen-table">` +
      `<thead><tr><th>因子</th><th>结论 / 主要原因</th><th>IC60 / 增量</th>` +
      `<th>残差IC / 比率</th><th>最高相关 / 冗余对象</th>` +
      `<th>衰减：半衰期 / 保留</th><th>覆盖 / 风格R²</th></tr></thead><tbody>`;
    factors.forEach(function (row) {
      const passed = row.pass === true;
      const decisionClass = passed ? "mine-accepted" : "mine-rejected";
      const redundant = row.redundant_with ? ` / ${escapeHtml(row.redundant_with)}` : "";
      const decay = `${fixedText(row.half_life, 2)} / ${fixedText(row.decay_retention, 4)}`;
      const reason = escapeHtml(factorFailureReasons(row));
      html += `<tr><td class="mine-screen-factor">${escapeHtml(row.factor || "未命名")}</td>` +
        `<td><span class="mine-decision ${decisionClass}">${passed ? "通过" : "未过"}</span>` +
        `<span class="mine-screen-reason">${reason}</span></td>` +
        `<td>${fixedText(row.ic60, 4)} / ${fixedText(row.gain, 4)}</td>` +
        `<td>${fixedText(row.resid_ic, 4)} / ${fixedText(row.resid_ratio, 3)}</td>` +
        `<td>${fixedText(row.maxcorr, 3)}${redundant}</td>` +
        `<td class="${row.decay_pass === false ? "mine-metric-worse" : "mine-metric-neutral"}">${decay}</td>` +
        `<td>${fixedText(row.coverage, 3)} / ${fixedText(row.style_r2, 3)}</td></tr>`;
    });
    return html + "</tbody></table></div>";
  }

  function renderParetoCandidates(candidates) {
    if (!candidates.length) return "";
    const validScreens = candidates.filter(row => row.exact_screen && row.exact_screen.valid === true);
    const screened = validScreens.reduce((total, row) => total + Number(row.exact_screen.screened || 0), 0);
    const passed = validScreens.reduce((total, row) => total + Number(row.exact_screen.n_pass || 0), 0);
    let html = `<section class="mine-pareto-section"><div class="mine-pareto-title">` +
      `<b>生产门评估：</b>${candidates.length} 个 Pareto 候选，` +
      `${validScreens.length} 个已完成精确终筛，逐因子 ${passed}/${screened} 过关</div>`;
    candidates.forEach(function (candidate, index) {
      const screen = candidate.exact_screen || {};
      const metrics = candidate.metrics || {};
      const round = Number.isFinite(Number(candidate.backtest_round))
        ? Number(candidate.backtest_round)
        : (Number.isFinite(Number(candidate.research_round)) ? Number(candidate.research_round) : index + 1);
      const exact = screen.valid === true
        ? `exact ${Number(screen.n_pass || 0)}/${Number(screen.screened || 0)}`
        : screenErrorText(screen.error);
      const statusText = candidateStatusText(candidate, screen);
      const shortId = String(candidate.candidate_id || "").slice(0, 12);
      let body = `<div class="mine-candidate-metrics">研究回测：成本后年化超额 ` +
        `<b>${metricText(metrics.net_annualized_return, true)}</b> · IR ${fixedText(metrics.net_information_ratio, 3)} · ` +
        `最大回撤 ${metricText(metrics.max_drawdown, true)} · IC ${fixedText(metrics.ic, 4)} · ` +
        `RankIC ${fixedText(metrics.rank_ic, 4)}</div>`;
      if (screen.valid === true) {
        body += `<div class="mine-screen-meta">逐因子精确终筛 · base IC ${fixedText(screen.base_ic, 4)} · ` +
          `更新时间 ${escapeHtml(screen.updated || "未记录")}</div>` + renderExactFactorRows(screen);
      } else {
        body += `<div class="mine-run-warning"><b>终筛记录：</b>${escapeHtml(screenErrorText(screen.error))}</div>`;
      }
      if (candidate.error) {
        body += `<div class="mine-run-warning"><b>评估错误：</b>${escapeHtml(candidate.error)}</div>`;
      }
      html += `<details class="mine-pareto-candidate"><summary>` +
        `研究#${round} · 年化 ${metricText(metrics.net_annualized_return, true)} · ` +
        `IR ${fixedText(metrics.net_information_ratio, 3)} · ${escapeHtml(exact)} · ` +
        `${escapeHtml(statusText)} <span title="${escapeHtml(candidate.candidate_id || "")}">[${escapeHtml(shortId)}]</span>` +
        `</summary>${body}</details>`;
    });
    return html + "</section>";
  }

  function renderDetails(run) {
    const backtests = Array.isArray(run.backtests) ? run.backtests : [];
    const unevaluated = Array.isArray(run.unevaluated_factors) ? run.unevaluated_factors : [];
    const paretoCandidates = Array.isArray(run.pareto_candidates) ? run.pareto_candidates : [];
    const accepted = backtests.filter(row => row.accepted === true).length;
    const testedFactorCount = Number(run.n_evaluated_factors || 0);
    const untestedFactorCount = Number(run.n_unevaluated_factors || unevaluated.length || 0);
    const screen = run.n_eval == null
      ? "正交终筛未归档"
      : `正交终筛 ${Number(run.n_pass || 0)}/${Number(run.n_eval || 0)}`;

    if (!backtests.length && !unevaluated.length && !paretoCandidates.length) {
      return `<div class="mine-history-no-detail">逐次回测明细不可用（历史日志已清理或格式不支持）</div>`;
    }

    let body = "";
    if (backtests.length) {
      body += `<div class="mine-backtest-scroll"><table class="mine-backtest-table">` +
        `<thead><tr><th>次</th><th>RD判定</th><th>IC<br><small>当前 / 当时SOTA</small></th>` +
        `<th>成本后年化超额<br><small>当前 / 当时SOTA</small></th>` +
        `<th>最大回撤<br><small>当前 / 当时SOTA</small></th></tr></thead><tbody>`;
      backtests.forEach(function (row, index) {
        const acceptedClass = row.accepted === true ? "mine-accepted" :
          (row.accepted === false ? "mine-rejected" : "mine-unknown");
        const acceptedText = row.accepted === true ? "接纳" :
          (row.accepted === false ? "未接纳" : "未记录");
        const round = Number.isFinite(Number(row.round)) ? Number(row.round) : index + 1;
        const factors = Array.isArray(row.factors) && row.factors.length
          ? row.factors.map(escapeHtml).join(", ")
          : "未记录该次因子名";
        const evaluatedAt = row.evaluated_at
          ? `<span class="mine-evaluated-at">完成 ${escapeHtml(String(row.evaluated_at).split(" ").pop())} · </span>`
          : "";
        body += `<tr class="mine-backtest-metrics"><td>${round}</td>` +
          `<td><span class="mine-decision ${acceptedClass}">${acceptedText}</span></td>` +
          `<td>${metricPair(row.ic, row.sota_ic, false, true)}</td>` +
          `<td>${metricPair(row.annualized_return, row.sota_annualized_return, true, true)}</td>` +
          `<td>${metricPair(row.max_drawdown, row.sota_max_drawdown, true, true)}</td></tr>` +
          `<tr class="mine-backtest-factors"><td colspan="5">${evaluatedAt}<b>本次因子：</b>${factors}</td></tr>`;
      });
      body += "</tbody></table></div>";
    }

    if (unevaluated.length) {
      body += `<div class="mine-unevaluated"><b>未形成回测指标（${unevaluated.length}）：</b>` +
        `${unevaluated.map(escapeHtml).join(", ")}</div>`;
    }
    body += renderParetoCandidates(paretoCandidates);
    if (run.state === "partial" || run.state === "error") {
      body += `<div class="mine-run-warning"><b>运行状态：</b>${escapeHtml(run.outcome || run.error_code || "异常结束")}</div>`;
    }
    body += `<div class="mine-history-legend">“RD接纳”是该次内部迭代是否替换SOTA；不等于最终正交终筛过关。</div>`;

    const paretoSummary = paretoCandidates.length ? ` · Pareto生产门 ${paretoCandidates.length}候选` : "";
    const factorSummary = run.factor_count_exact === true
      ? ` · ${testedFactorCount}因子已回测${untestedFactorCount ? ` / ${untestedFactorCount}未回测` : ""}`
      : "";
    return `<details class="mine-history-detail"><summary>查看 ${backtests.length} 次回测${factorSummary} · ` +
      `RD接纳 ${accepted}/${backtests.length} · ${escapeHtml(screen)}${paretoSummary}</summary>${body}</details>`;
  }

  global.MineHistory = {escapeHtml, summaryFactors, evaluatedFactors, factorCountText, renderDetails};
})(window);
