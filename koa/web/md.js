// KOA 웹 — 작은 마크다운 렌더러 (외부 라이브러리 없음, 오프라인).
// KOA 가 만드는 보고서(discover report.md, triage *.md, 조사 결과 report.md)에 쓰는 문법만 다룬다:
// 제목(#~####), 표(| a | b |, \| 이스케이프, 정렬 줄), 목록(-, *, 1.), 인용(>), 코드 블록(```), 가로줄(---),
// 인라인: `코드`, **굵게**, *기울임*, [글](http…), 빈 줄로 나뉜 문단.
// 모든 글자는 먼저 이스케이프한다 → 보고서 안의 HTML 은 글자로만 보인다 (링크는 http(s) 만).
(function (root) {
  "use strict";
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function inline(s) {
    // 코드 조각을 먼저 빼 두고(안의 * _ [ 를 건드리지 않게) 나머지를 바꾼 뒤 되돌린다
    const codes = [];
    s = String(s).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = esc(s)
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,:;!?]|$)/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => "<code>" + esc(codes[+i]) + "</code>");
  }

  // 표 한 줄 → 칸. 코드 조각 안의 | 와 \| 는 칸 구분이 아니다.
  function cells(line) {
    let t = line.trim();
    if (t.startsWith("|")) t = t.slice(1);
    if (t.endsWith("|") && !t.endsWith("\\|")) t = t.slice(0, -1);
    const out = []; let cur = "", code = false;
    for (let i = 0; i < t.length; i++) {
      const ch = t[i];
      if (ch === "\\" && t[i + 1] === "|") { cur += "|"; i++; continue; }
      if (ch === "`") code = !code;
      if (ch === "|" && !code) { out.push(cur.trim()); cur = ""; continue; }
      cur += ch;
    }
    out.push(cur.trim());
    return out;
  }
  const isTableRow = l => /^\s*\|.*\|\s*$/.test(l);
  const isAlign = l => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);

  function render(md) {
    const lines = String(md || "").replace(/\r\n?/g, "\n").split("\n");
    const out = []; let i = 0;
    const blockStart = l => /^(#{1,6})\s|^```|^\s*>|^\s*([-*]|\d+[.)])\s+|^\s*(-{3,}|\*{3,})\s*$/.test(l) || isTableRow(l);
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) { i++; continue; }
      let m;
      if ((m = l.match(/^```\s*(\S*)/))) {
        const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push("<pre><code>" + esc(buf.join("\n")) + "</code></pre>");
        continue;
      }
      if ((m = l.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/))) {
        const n = Math.min(m[1].length, 4);
        out.push(`<h${n} class="md-h">${inline(m[2])}</h${n}>`); i++; continue;
      }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
      if (isTableRow(l) && i + 1 < lines.length && isAlign(lines[i + 1])) {
        const head = cells(l), aligns = cells(lines[i + 1]).map(a => /^:-+:$/.test(a) ? "center" : /-:$/.test(a) ? "right" : "");
        i += 2;
        const rows = [];
        while (i < lines.length && isTableRow(lines[i])) rows.push(cells(lines[i++]));
        const al = j => aligns[j] ? ` style="text-align:${aligns[j]}"` : "";
        out.push('<div class="md-table"><table><thead><tr>' + head.map((c, j) => `<th${al(j)}>${inline(c)}</th>`).join("") + "</tr></thead><tbody>"
          + rows.map(r => "<tr>" + head.map((_, j) => `<td${al(j)}>${inline(r[j] == null ? "" : r[j])}</td>`).join("") + "</tr>").join("")
          + "</tbody></table></div>");
        continue;
      }
      if (/^\s*>/.test(l)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push("<blockquote>" + render(buf.join("\n")) + "</blockquote>");
        continue;
      }
      if ((m = l.match(/^(\s*)([-*]|\d+[.)])\s+/))) {
        const ordered = /\d/.test(m[2]), base = m[1].length, items = [];
        const itemRe = ordered ? /^(\s*)(\d+)[.)]\s+(.*)$/ : /^(\s*)[-*]\s+(.*)$/;
        let start = null;
        while (i < lines.length) {
          const x = lines[i], mm = x.match(itemRe);
          if (mm && mm[1].length <= base + 1) {
            if (ordered && start === null) start = +mm[2];
            items.push([ordered ? mm[3] : mm[2]]); i++; continue;
          }
          // 들여 쓴 줄(하위 목록·이어지는 글)은 앞 항목에 붙인다
          if (items.length && x.trim() && /^\s{2,}/.test(x)) { items[items.length - 1].push(x.replace(new RegExp("^\\s{0," + (base + 4) + "}"), "")); i++; continue; }
          break;
        }
        const li = it => {
          const first = inline(it[0]), rest = it.slice(1);
          return "<li>" + first + (rest.length ? render(rest.join("\n")) : "") + "</li>";
        };
        out.push(ordered ? `<ol${start && start !== 1 ? ` start="${start}"` : ""}>` + items.map(li).join("") + "</ol>" : "<ul>" + items.map(li).join("") + "</ul>");
        continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !(buf.length && blockStart(lines[i]))) buf.push(lines[i++].trim());
      if (!buf.length) { buf.push(lines[i++].trim()); }
      out.push("<p>" + buf.map(inline).join("<br>") + "</p>");
    }
    return out.join("\n");
  }

  const api = { render, inline, cells, esc };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KoaMD = api;
})(this);
