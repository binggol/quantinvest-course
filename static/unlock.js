function qvEsc(s){
  return String(s ?? "")
    .replace(/&/g,"&amp;")
    .replace(/</g,"&lt;")
    .replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}

function qvUnlockStatusColor(days,status){
  if(status==="high" || (Number.isInteger(days) && days >= 0 && days <= 30)) return "#f87171";
  if(status==="watch" || (Number.isInteger(days) && days >= 0 && days <= 90)) return "#fbbf24";
  return "#94a3b8";
}

function qvUnlockDaysText(days){
  if(!Number.isInteger(days)) return "";
  return days >= 0 ? `\u8ddd${days}\u5929` : `\u5df2\u8fc7${Math.abs(days)}\u5929`;
}

function qvUnlockOne(label,row){
  const date=row.unlock_date || row.list_date || row.date || "";
  const days=qvUnlockDaysText(row.days_to_unlock);
  const color=qvUnlockStatusColor(row.days_to_unlock,row.status || row.risk_level);
  const title=row.title || row.reason || row.event || row.name || "";
  const text=[label,date,days].filter(Boolean).join(" ");
  const tip=[title,row.ann_date || row.date || "",row.dataset || row.source || ""].filter(Boolean).join(" | ");
  return `<div title="${qvEsc(tip)}" style="color:${color};font-size:11px;line-height:1.35;white-space:nowrap">${qvEsc(text)}</div>`;
}

function formatUnlockInfo(rowOrInfo){
  const u=(rowOrInfo && rowOrInfo.unlock_info) ? rowOrInfo.unlock_info : (rowOrInfo || {});
  const none=!u || !u.label || u.label==="\u65e0\u89e3\u7981\u6570\u636e";
  const parts=[];
  (u.transfer || []).slice(0,2).forEach(x=>parts.push(qvUnlockOne("\u8be2\u8f6c/\u534f\u8f6c",x)));
  (u.placement || []).slice(0,2).forEach(x=>parts.push(qvUnlockOne("\u5b9a\u589e",x)));
  (u.other || []).slice(0,2).forEach(x=>parts.push(qvUnlockOne("\u5176\u4ed6\u89e3\u7981",x)));
  if(parts.length) return parts.join("");
  if(none) return '<span style="color:#64748b;font-size:11px">-</span>';
  const c=u.status==="high"?"#f87171":(u.status==="watch"?"#fbbf24":"#94a3b8");
  return `<span title="${qvEsc(u.label)}" style="color:${c};font-size:11px">${qvEsc(u.label)}</span>`;
}
