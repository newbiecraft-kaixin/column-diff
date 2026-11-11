"""列比对 MCP 服务（stdio）。

把本机的比对程序暴露成 MCP 工具，供 Claude / Cursor / Trae 等客户端调用。

用法：
1. 先启动网页服务：python app.py（默认 http://127.0.0.1:5000）
2. 在 MCP 客户端里添加 stdio 服务：
   command = python
   args = ["C:\\Users\\KaiXin\\Desktop\\比对程序\\mcp_server.py"]
   可选环境变量 COLCMP_API 指定接口地址，默认 http://127.0.0.1:5000
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

API = os.environ.get("COLCMP_API", "http://127.0.0.1:5000").rstrip("/")
REPORT_DIR = Path(__file__).resolve().parent / "reports"

mcp = FastMCP("column-diff")


def _get(path: str) -> dict:
    response = requests.get(f"{API}{path}", timeout=30)
    return _unwrap(response)


def _post(path: str, payload: dict | None = None) -> dict:
    response = requests.post(f"{API}{path}", json=payload or {}, timeout=180)
    return _unwrap(response)


def _unwrap(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:500]}
    if response.status_code >= 400:
        raise RuntimeError(data.get("error") or f"接口返回 {response.status_code}")
    return data


@mcp.tool()
def list_tasks() -> list[dict]:
    """列出当前所有比对任务：任务名、参与列数、差异行数、状态。"""
    tasks = _get("/api/state")["tasks"]
    return [
        {
            "id": task["id"],
            "name": task["name"],
            "columns": len(task["columns"]),
            "diff_rows": task["diff_rows"],
            "status": task["status"],
            "hint": task.get("status_hint", ""),
            "report": task.get("report", {}).get("reason", ""),
            "has_ai_report": bool(task.get("ai_report")),
        }
        for task in tasks
    ]


@mcp.tool()
def get_report(task_id: str, max_rows: int = 50) -> dict:
    """获取某个任务的比对结果：概览、各列差异数、差异明细（默认最多 50 行）。"""
    result = _get(f"/api/tasks/{task_id}/compare")
    diffs = result["diffs"]
    return {
        "task": result["task"]["name"],
        "columns": [col["label"] for col in result["columns"]],
        "rows": result["rows"],
        "diff_count": len(diffs),
        "per_column": [
            {"column": col["label"], "diffs": stat["diffs"]}
            for col, stat in zip(result["columns"], result["per_column"])
        ],
        "diffs": diffs[: max(1, min(max_rows, 500))],
        "truncated": len(diffs) > max_rows,
    }


@mcp.tool()
def ai_analyze(task_id: str) -> dict:
    """调用网页里配置好的大模型，生成该任务的中文分析报告（Markdown）。"""
    return _post(f"/api/tasks/{task_id}/ai-report")


@mcp.tool()
def export_html(task_id: str) -> dict:
    """导出单文件 HTML 报告到本地 reports 目录，返回文件路径。"""
    response = requests.post(f"{API}/api/tasks/{task_id}/report.html", json={}, timeout=120)
    if response.status_code >= 400:
        raise RuntimeError(_unwrap(response).get("error", "导出失败"))
    task_name = get_report(task_id).get("task", "report")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"{task_name}-比对报告.html"
    target.write_bytes(response.content)
    return {"path": str(target), "bytes": len(response.content)}


@mcp.tool()
def import_files(paths: list[str]) -> dict:
    """导入本机的 Excel/CSV 文件（绝对路径列表），返回解析出的文件与列数。"""
    files = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise RuntimeError(f"文件不存在：{path}")
        with path.open("rb") as handle:
            response = requests.post(
                f"{API}/api/files",
                files={"file": (path.name, handle)},
                timeout=300,
            )
        files.append(_unwrap(response))
    state = _get("/api/state")
    return {"imported": files, "files": len(state["files"]), "tasks": len(state["tasks"])}


@mcp.tool()
def auto_group() -> dict:
    """按当前相似阈值自动分组：相似列并入同一任务，其余列各自建任务。"""
    return _post("/api/auto-group")


if __name__ == "__main__":
    mcp.run()
