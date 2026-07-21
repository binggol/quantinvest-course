(function (global) {
  "use strict";

  const ADJUST_LABELS = Object.freeze({
    qfq: "前复权",
    hfq: "后复权",
    none: "不复权",
  });

  function normalizeAdjust(value) {
    const normalized = String(value || "").trim().toLowerCase();
    return normalized === "raw" ? "none" : normalized;
  }

  function adjustLabel(value) {
    const normalized = normalizeAdjust(value);
    return ADJUST_LABELS[normalized] || normalized || "未知复权口径";
  }

  function readQualityNotice(payload) {
    const raw = payload && payload.quality && payload.quality.ohlc_envelope_repairs;
    if (raw == null) return { ohlcEnvelopeRepairs: 0, qualityNotice: "" };
    const repairs = Number(raw);
    if (!Number.isInteger(repairs) || repairs < 0) throw new Error("行情质量统计格式错误");
    return {
      ohlcEnvelopeRepairs: repairs,
      qualityNotice: repairs > 0 ? `源数据高低价修正 ${repairs} 条` : "",
    };
  }

  function assertKlinePayload(data, requestedAdjust) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("行情接口返回格式错误");
    }
    if (data.source === "demo") {
      throw new Error("当前只有本地演示数据，不能用于核对真实价格");
    }

    const requested = normalizeAdjust(requestedAdjust || "qfq");
    const actual = normalizeAdjust(data.adjust);
    const echoedRequest = normalizeAdjust(data.adjust_requested);
    const adjustmentApplicable = data.adjustment_applicable !== false;
    if (!ADJUST_LABELS[requested]) {
      throw new Error(`不支持的复权口径：${requested || "空"}`);
    }
    if (!ADJUST_LABELS[actual]) {
      throw new Error("行情接口未标明有效的复权口径");
    }
    if (echoedRequest && echoedRequest !== requested) {
      throw new Error(`行情接口复权请求不一致：请求${adjustLabel(requested)}，接口记录${adjustLabel(echoedRequest)}`);
    }
    if (adjustmentApplicable && actual !== requested) {
      throw new Error(`请求${adjustLabel(requested)}，但数据源只返回${adjustLabel(actual)}，已停止绘图`);
    }

    const fields = ["dates", "open", "close", "low", "high", "volume"];
    for (const field of fields) {
      if (!Array.isArray(data[field])) throw new Error(`行情字段 ${field} 缺失`);
    }
    const count = data.dates.length;
    if (!count) throw new Error("行情接口未返回交易日期");
    if (fields.some(field => data[field].length !== count)) {
      throw new Error("行情字段长度不一致");
    }

    let previousDate = "";
    for (let index = 0; index < count; index += 1) {
      const date = String(data.dates[index] || "");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || (previousDate && date <= previousDate)) {
        throw new Error(`行情日期无效或未按升序排列：${date || `第 ${index + 1} 条`}`);
      }
      previousDate = date;

      const open = Number(data.open[index]);
      const close = Number(data.close[index]);
      const low = Number(data.low[index]);
      const high = Number(data.high[index]);
      const volume = Number(data.volume[index]);
      if (![open, close, low, high, volume].every(Number.isFinite)) {
        throw new Error(`行情包含无效数值：${date}`);
      }
      const tolerance = Math.max(1, Math.abs(high), Math.abs(low)) * 1e-8;
      if (high + tolerance < Math.max(open, close, low) || low - tolerance > Math.min(open, close, high)) {
        throw new Error(`行情高低价关系异常：${date}`);
      }
      if (volume < 0) throw new Error(`成交量为负数：${date}`);
    }

    const quality = readQualityNotice(data);
    return {
      adjust: actual,
      adjustLabel: adjustmentApplicable ? adjustLabel(actual) : "指数不适用复权",
      adjustmentApplicable,
      count,
      lastDate: data.dates[count - 1],
      source: String(data.source || "qlib"),
      ...quality,
    };
  }

  function assertOhlcValues(open, close, low, high, context) {
    const values = [open, close, low, high].map(Number);
    if (!values.every(Number.isFinite)) throw new Error(`K线包含无效数值：${context}`);
    const [normalizedOpen, normalizedClose, normalizedLow, normalizedHigh] = values;
    const tolerance = Math.max(1, Math.abs(normalizedHigh), Math.abs(normalizedLow)) * 1e-8;
    if (
      normalizedHigh + tolerance < Math.max(normalizedOpen, normalizedClose, normalizedLow) ||
      normalizedLow - tolerance > Math.min(normalizedOpen, normalizedClose, normalizedHigh)
    ) {
      throw new Error(`K线高低价关系异常：${context}`);
    }
  }

  function assertCompactPayload(kline, expectedAdjust) {
    if (!kline || typeof kline !== "object" || Array.isArray(kline)) {
      throw new Error("K线数据格式错误");
    }
    const dates = kline.dates;
    const ohlc = kline.ohlc;
    if (!Array.isArray(dates) || !Array.isArray(ohlc) || !dates.length) {
      throw new Error("K线日期或 OHLC 数据缺失");
    }
    if (dates.length !== ohlc.length) throw new Error("K线日期与 OHLC 长度不一致");

    const expected = normalizeAdjust(expectedAdjust || "qfq");
    const actual = normalizeAdjust(kline.adjust);
    if (!actual) throw new Error("K线数据未标明复权口径");
    if (actual !== expected) {
      throw new Error(`K线口径错误：需要${adjustLabel(expected)}，实际为${adjustLabel(actual)}`);
    }
    let previousDate = "";
    let nonDojiCount = 0;
    dates.forEach((rawDate, index) => {
      const date = String(rawDate || "");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || (previousDate && date <= previousDate)) {
        throw new Error(`K线日期无效或未按升序排列：${date || `第 ${index + 1} 条`}`);
      }
      previousDate = date;
      const row = ohlc[index];
      if (!Array.isArray(row) || row.length !== 4) throw new Error(`K线 OHLC 顺序或长度错误：${date}`);
      assertOhlcValues(row[0], row[1], row[2], row[3], date);
      if (Number(row[0]) !== Number(row[1])) nonDojiCount += 1;
    });
    if (dates.length >= 5 && nonDojiCount === 0) {
      throw new Error("K线开盘价全部等于收盘价，疑似开盘字段缺失");
    }
    return {
      adjust: actual,
      adjustLabel: adjustLabel(actual),
      count: dates.length,
      lastDate: dates[dates.length - 1],
      source: String(kline.source || ""),
      ...readQualityNotice(kline),
    };
  }

  function assertPointRows(points) {
    if (!Array.isArray(points) || !points.length) throw new Error("日K数据缺失");
    points.forEach((row, index) => {
      if (!row || typeof row !== "object") throw new Error(`日K记录格式错误：第 ${index + 1} 条`);
      const context = String(row.date || row.t || row.symbol || `第 ${index + 1} 条`);
      assertOhlcValues(row.open, row.close, row.low, row.high, context);
    });
    return { count: points.length };
  }

  global.QIKline = Object.freeze({
    adjustLabel,
    assertCompactPayload,
    assertPayload: assertKlinePayload,
    assertPointRows,
    normalizeAdjust,
  });
})(window);
