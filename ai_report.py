"""AI 分析与 HTML 报告生成。

- 兼容 OpenAI Chat Completions 协议的厂商都能用（DeepSeek / OpenAI / 通义 / 智谱 / Moonshot）
- API Key 只保存在进程内存里，不落盘
- HTML 报告为单文件，浏览器可直接打印成 PDF
"""

from __future__ import annotations

import html
import re
from datetime import datetime

import requests

# provider -> (base_url, 默认模型, 显示名)
PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "DeepSeek"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OpenAI"),
    "dashscope": (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "通义千问",
    ),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "智谱 GLM"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "Moonshot"),
    "custom": ("", "", "自定义（OpenAI 兼容）"),
}

AI_CONFIG: dict[str, str] = {"provider": "", "api_key": "", "base_url": "", "model": ""}

MAX_DIFF_ROWS = 200
MAX_CELL_CHARS = 80


class AiError(RuntimeError):
    """可直接展示给用户的错误。"""


def ai_settings_public() -> dict:
    key = AI_CONFIG["api_key"]
    hint = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else ("已保存" if key else "")
    provider = AI_CONFIG["provider"]
    base_url = AI_CONFIG["base_url"] or PROVIDERS.get(provider, ("", "", ""))[0]
    model = AI_CONFIG["model"] or PROVIDERS.get(provider, ("", "", ""))[1]
    return {
        "provider": provider,
        "options": [
            {"value": key, "label": name, "base_url": base, "model": model}
            for key, (base, model, name) in PROVIDERS.items()
        ],
        "base_url": base_url,
        "model": model,
        "has_key": bool(key),
        "key_hint": hint,
        "configured": bool(key and provider),
    }


def save_ai_settings(payload: dict) -> dict:
    provider = (payload.get("provider") or "").strip()
    if provider not in PROVIDERS:
        raise AiError("请选择厂商")
    base_url, default_model, _ = PROVIDERS[provider]
    base_url = (payload.get("base_url") or base_url).strip().rstrip("/")
    if not base_url.startswith("http"):
        raise AiError("Base URL 需要以 http(s):// 开头")
    api_key = (payload.get("api_key") or "").strip()
    if api_key:
        AI_CONFIG["api_key"] = api_key
    elif not AI_CONFIG["api_key"]:
        raise AiError("请填写 API Key")
    AI_CONFIG.update(
        provider=provider,
        base_url=base_url,
        model=(payload.get("model") or default_model).strip() or default_model,
    )
    return ai_settings_public()


def _clip(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").strip()
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "…"


def _col_label(col: dict) -> str:
    """列显示名；同一页出现重名列时补上列索引，避免分不清是哪一列。"""
    suffix = f"（第{col['col_index'] + 1}列）" if col.get("column_dup") else ""
    return f"{col['column']}{suffix}"


def build_prompt(result: dict) -> list[dict]:
    labels = [col["label"] for col in result["columns"]]
    total = result["rows"]
    diffs = result["diffs"]
    rate = (1 - len(diffs) / total) * 100 if total else 100.0

    stats = "\n".join(
        f"- {col['file_name']} / 第{col['sheet_index']}页 / {_col_label(col)}：{stat['diffs']} 行不同"
        for col, stat in zip(result["columns"], result["per_column"])
    )

    rows = []
    for diff in diffs[:MAX_DIFF_ROWS]:
        cells = "；".join(
            f"{label}=\"{_clip(diff['values'][label])}\"" for label in labels
        )
        rows.append(f"- 第{diff['row']}行（不同列：{'、'.join(diff['differ'])}）：{cells}")
    more = (
        f"\n（仅列出前 {MAX_DIFF_ROWS} 行差异，共 {len(diffs)} 行）"
        if len(diffs) > MAX_DIFF_ROWS
        else ""
    )

    user = f"""比对任务：{result['task']['name']}
参与列 {len(labels)} 个，按行号对齐，数据行 {total} 行，差异 {len(diffs)} 行，一致率 {rate:.1f}%。

各列差异数：
{stats}

差异明细：
{chr(10).join(rows) if rows else "（无差异）"}{more}

请输出简体中文 Markdown 报告，结构如下：
## 总体结论
一句话说明是否可直接采用，以及风险等级。
## 差异分析
按列归类，说明差异集中在哪些列、呈现什么模式（个别行/整列缺失/格式不一致等）。
## 重点行
列出最需要人工核实的行号，并说明原因（只引用上面给出的数据，不要编造）。
## 可能原因
结合常见场景推测（录入错误、版本不同步、格式差异、缺失值等），标注为推测。
## 建议动作
给出 3-6 条可执行的处理建议。
注意：数据按行号对齐，若行数或顺序不同可能产生错位，请在报告中提示该局限。"""
    return [
        {
            "role": "system",
            "content": "你是资深数据核对分析师，只依据用户提供的比对结果作答，不编造数据。",
        },
        {"role": "user", "content": user},
    ]


def request_analysis(result: dict) -> dict:
    content, model = _chat(build_prompt(result), timeout=120)
    return {
        "markdown": content,
        "model": model,
        "provider": AI_CONFIG["provider"],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _chat(messages: list[dict], timeout: int = 60) -> tuple[str, str]:
    if not AI_CONFIG["api_key"] or not AI_CONFIG["provider"]:
        raise AiError("还没有配置 AI：请点右上角「AI 设置」填写厂商和 API Key")
    base_url = AI_CONFIG["base_url"] or PROVIDERS[AI_CONFIG["provider"]][0]
    model = AI_CONFIG["model"] or PROVIDERS[AI_CONFIG["provider"]][1]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {AI_CONFIG['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise AiError(f"调用 AI 失败：{exc}") from exc

    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = body.get("error", {}).get("message") or body.get("message") or ""
        except ValueError:
            detail = response.text[:200]
        raise AiError(f"AI 返回 {response.status_code}：{detail or '请检查 Key、模型名或余额'}")

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise AiError("AI 返回内容无法解析") from exc
    return content.strip(), model


def test_connection() -> dict:
    content, model = _chat(
        [{"role": "user", "content": "只回复两个字：可用"}], timeout=30
    )
    return {"ok": True, "model": model, "reply": content[:40]}


# ---------- Markdown -> HTML（够用即可，全部先转义） ----------


def _inline(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def md_to_html(markdown: str) -> str:
    out: list[str] = []
    list_tag: str | None = None
    in_code = False

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            close_list()
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        if not line.strip():
            close_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(html.escape(heading.group(2)))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet or numbered:
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            body = (bullet or numbered).group(1)
            out.append(f"<li>{_inline(html.escape(body))}</li>")
            continue
        close_list()
        out.append(f"<p>{_inline(html.escape(line))}</p>")
    close_list()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


REPORT_CSS = """
:root {
  --paper: #f3eee4; --paper-raised: #fbf8f1; --ink: #1c1914;
  --ink-soft: #5c5548; --line: #d8cfbd; --accent: #bf3f26; --ok: #2f6b46;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 40px 28px 64px; background: var(--paper); color: var(--ink);
  font: 15px/1.75 "Noto Sans SC", "Microsoft YaHei", system-ui, sans-serif;
}
.page { max-width: 1060px; margin: 0 auto; }
header.top { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px;
  padding-bottom: 18px; border-bottom: 2px solid var(--ink); }
.brand { font: 600 12px/1 "Georgia", serif; letter-spacing: .28em; text-transform: uppercase; color: var(--ink-soft); }
h1 { margin: 8px 0 0; font: 600 30px/1.25 "Songti SC", "SimSun", Georgia, serif; letter-spacing: .02em; }
.meta { text-align: right; font-size: 12px; color: var(--ink-soft); }
.meta div + div { margin-top: 4px; }
.print-btn {
  position: fixed; right: 24px; top: 24px; padding: 10px 16px; border: 1px solid var(--ink);
  border-radius: 8px; background: var(--paper-raised); color: var(--ink); cursor: pointer;
  font-size: 13px; box-shadow: 0 8px 20px -14px rgba(28,25,20,.8);
}
.summary { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 14px; margin: 26px 0 30px; }
.summary div { background: var(--paper-raised); border: 1px solid var(--line); border-left: 3px solid var(--accent);
  border-radius: 8px; padding: 14px 16px; }
.summary span { display: block; font-size: 11px; letter-spacing: .12em; color: var(--ink-soft); }
.summary strong { display: block; margin-top: 6px; font: 600 24px/1.2 "Georgia", serif; }
section { margin-bottom: 34px; }
h2 { font: 600 20px/1.3 "Songti SC", "SimSun", Georgia, serif; padding-bottom: 8px;
  border-bottom: 1px solid var(--line); }
table { width: 100%; border-collapse: collapse; background: var(--paper-raised); font-size: 13px; }
th, td { padding: 9px 12px; border: 1px solid var(--line); text-align: left; vertical-align: top; }
th { background: #efe8da; font-weight: 600; white-space: nowrap; }
td.rowno { width: 64px; color: var(--ink-soft); text-align: right; }
tr.diff td.hit { background: #fbe3dd; box-shadow: inset 2px 0 0 var(--accent); }
td.none { color: var(--ink-soft); }
.ai { background: var(--paper-raised); border: 1px solid var(--line); border-left: 3px solid var(--accent);
  border-radius: 10px; padding: 20px 24px; }
.ai h2 { margin-top: 0; border-bottom: 1px solid var(--line); }
.ai pre { background: #efe8da; padding: 12px; border-radius: 6px; overflow: auto; }
.ai code { background: #efe8da; padding: 1px 5px; border-radius: 4px; font-size: .92em; }
footer { margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--line);
  font-size: 12px; color: var(--ink-soft); }
@media print {
  body { padding: 0; background: #fff; }
  .print-btn { display: none; }
  .summary div, table, .ai { break-inside: avoid; }
  th, td { background: #fff !important; }
}
"""


def render_report_html(result: dict, ai: dict | None = None) -> str:
    labels = [col["label"] for col in result["columns"]]
    total = result["rows"]
    diffs = result["diffs"]
    rate = (1 - len(diffs) / total) * 100 if total else 100.0
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    name = html.escape(result["task"]["name"])

    stat_rows = "\n".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td class='rowno'>{}</td><td class='rowno'>{}</td></tr>".format(
            html.escape(col["file_name"]),
            col["sheet_index"],
            html.escape(_col_label(col)),
            col["rows"],
            stat["diffs"],
        )
        for col, stat in zip(result["columns"], result["per_column"])
    )

    head = "".join(f"<th>{html.escape(label)}</th>" for label in labels)
    body_rows = []
    for diff in diffs:
        cells = []
        for label in labels:
            value = diff["values"][label]
            text = "<span class='none'>&lt;无数据&gt;</span>" if value is None else html.escape(str(value))
            cls = " class='hit'" if label in diff["differ"] else ""
            cells.append(f"<td{cls}>{text}</td>")
        hit = html.escape("、".join(diff["differ"]))
        body_rows.append(
            f"<tr class='diff'><td class='rowno'>{diff['row']}</td>{''.join(cells)}<td>{hit}</td></tr>"
        )
    diff_table = (
        f"<table><thead><tr><th>行号</th>{head}<th>不同列</th></tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
        if diffs
        else "<p>所有参与列逐行一致，没有发现差异。</p>"
    )

    ai_block = ""
    if ai and ai.get("markdown"):
        ai_block = f"""
<section>
  <div class="ai">
    <h2>AI 分析</h2>
    {md_to_html(ai["markdown"])}
    <p style="font-size:12px;color:var(--ink-soft)">由 {html.escape(ai.get('model', ''))} 生成 · {html.escape(ai.get('time', ''))} · 仅供参考，请人工复核</p>
  </div>
</section>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · 比对报告</title>
<style>{REPORT_CSS}</style>
</head>
<body>
<button class="print-btn" onclick="window.print()">打印 / 存为 PDF</button>
<div class="page">
  <header class="top">
    <div>
      <div class="brand">Column Diff Report</div>
      <h1>{name}</h1>
    </div>
    <div class="meta">
      <div>生成时间 {created}</div>
      <div>相似阈值 {round(result['task']['threshold'] * 100)}% · 按行对齐 · 数据从第 {result['start_row']} 行起</div>
    </div>
  </header>

  <div class="summary">
    <div><span>参与列</span><strong>{len(labels)}</strong></div>
    <div><span>数据行</span><strong>{total}</strong></div>
    <div><span>差异行</span><strong>{len(diffs)}</strong></div>
    <div><span>一致率</span><strong>{rate:.1f}%</strong></div>
  </div>

  <section>
    <h2>参与列</h2>
    <table>
      <thead><tr><th>文件</th><th>页码</th><th>列名</th><th>数据行</th><th>差异行</th></tr></thead>
      <tbody>{stat_rows}</tbody>
    </table>
  </section>
{ai_block}
  <section>
    <h2>差异明细</h2>
    {diff_table}
  </section>

  <footer>
    本报告由「列比对 · COLUMN DIFF」生成。数据按行号对齐，若各文件行数或顺序不同，结果可能错位，请以原始表格为准。
  </footer>
</div>
</body>
</html>"""
