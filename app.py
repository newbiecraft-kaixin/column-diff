"""列比对工具 - 后端服务（Flask）。

流程：
1. 拖拽上传 Excel / CSV，解析每一页（工作表）的列
2. 点击任意一列作为任务基准，GPU 快速筛出相似度 ≥ 阈值的列，
   并入同一个任务，任务名取基准列（页码-列名）
3. 任务支持改名、删除、移除成员列；阈值调整后自动重算任务成员
"""

from __future__ import annotations

import re
import tempfile
import uuid
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path

import openpyxl
from flask import Flask, jsonify, request, send_file, send_from_directory
from openpyxl.styles import Alignment, Font, PatternFill

from excel_compare import (
    MISSING,
    EXCEL_SUFFIXES,
    compare_columns,
    load_columns,
    preview_rows,
    sheet_names,
)
from ai_report import (
    AiError,
    ai_settings_public,
    request_analysis,
    render_report_html,
    save_ai_settings,
    test_connection,
)
from similarity import CUDA_AVAILABLE, device_name, encode, similarities, similarity_matrix

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="colcmp_upload_"))
ALLOWED_SUFFIXES = EXCEL_SUFFIXES | {".csv"}

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 单文件 64MB

FILES: dict[str, dict] = {}  # file_id -> {"path": Path, "meta": dict, "data": {}}
TASKS: dict[str, dict] = {}  # task_id -> {"id", "name", "seed_uid", "threshold", "columns"}
SETTINGS = {"threshold": 0.8}

MAX_SET_ROWS = 20000  # 集合筛选最多返回的命中行数
MAX_SET_MISSING = 200  # 集合筛选最多返回的未命中元素数

_CODES: dict[str, object] = {}  # uid -> 编码后的 numpy 数组
_SIM_CACHE: dict[str, tuple[frozenset, dict[str, float]]] = {}
_MATRIX_CACHE: tuple[frozenset, dict[str, int], object] | None = None
_COLUMNS_CACHE: list[dict] | None = None
_INDEX_CACHE: dict[str, dict] | None = None
_SIGNATURE_CACHE: frozenset | None = None


# ---------- 列信息 ----------


def _uid(file_id: str, sheet_index: int, col_index: int) -> str:
    return f"{file_id}-{sheet_index}-{col_index}"


def _columns() -> list[dict]:
    """展开当前所有文件的全部列（结构不变时复用缓存）。"""
    global _COLUMNS_CACHE
    if _COLUMNS_CACHE is not None:
        return _COLUMNS_CACHE

    result: list[dict] = []
    for file_id, item in FILES.items():
        meta = item["meta"]
        for sheet in meta["sheets"]:
            bases = [re.sub(r"\(\d+\)$", "", name) for name in sheet["columns"]]
            counts = Counter(bases)
            for col_index, column in enumerate(sheet["columns"]):
                result.append(
                    {
                        "uid": _uid(file_id, sheet["index"], col_index),
                        "file_id": file_id,
                        "file_name": meta["name"],
                        "sheet_index": sheet["index"],
                        "sheet_name": sheet["name"],
                        "col_index": col_index,
                        # 同一页出现重名列时，界面要额外标出列索引
                        "column_dup": counts[bases[col_index]] > 1,
                        "column": column,
                        "rows": sheet["rows"],
                    }
                )
    _COLUMNS_CACHE = result
    return result


def _column(uid: str) -> dict | None:
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        _INDEX_CACHE = {column["uid"]: column for column in _columns()}
    return _INDEX_CACHE.get(uid)


def _find_column(file_id: str, sheet_index: int, column: str) -> dict | None:
    return next(
        (
            item
            for item in _columns()
            if item["file_id"] == file_id
            and item["sheet_index"] == sheet_index
            and item["column"] == column
        ),
        None,
    )


def _task_name(seed: dict) -> str:
    """任务默认名：文件名-第N页(页名)-列名；同页重名列再补列索引。"""
    suffix = ""
    if seed.get("column_dup"):
        suffix = f'-第{seed["col_index"] + 1}列'
    return (
        f'{seed["file_name"]}-第{seed["sheet_index"]}页'
        f'({seed["sheet_name"]})-{seed["column"]}{suffix}'
    )


def _new_task(seed: dict) -> dict:
    task = {
        "id": uuid.uuid4().hex[:12],
        "name": _task_name(seed),
        "seed_uid": seed["uid"],
        "threshold": SETTINGS["threshold"],
        "columns": _members(seed["uid"], SETTINGS["threshold"]),
        "excluded": set(),
        "pinned": set(),
    }
    TASKS[task["id"]] = task
    return task


def _sheet_values(item: dict, sheet_index: int, sheet_name: str) -> list[list]:
    """按页缓存列数据（每页只解析一次，且不做列表拷贝）。"""
    cache = item.setdefault("data", {})
    if sheet_index not in cache:
        sheet = next(
            (s for s in item["meta"]["sheets"] if s["index"] == sheet_index), None
        )
        header_row = sheet.get("header_row", 1) if sheet else 1
        loaded = load_columns(item["path"], None, sheet_name, header_row=header_row)
        cache[sheet_index] = list(loaded.values())
    return cache[sheet_index]


def _column_values(column: dict) -> list:
    return _sheet_values(FILES[column["file_id"]], column["sheet_index"], column["sheet_name"])[
        column["col_index"]
    ]


# ---------- 集合筛选 ----------


def _norm_value(value) -> str:
    """归一化成可比较的文本：去首尾空白，数字去掉多余的 .0。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _header_row(column: dict) -> int:
    sheet = next(
        (
            item
            for item in FILES[column["file_id"]]["meta"]["sheets"]
            if item["index"] == column["sheet_index"]
        ),
        None,
    )
    return sheet.get("header_row", 1) if sheet else 1


def _column_ref(ref: dict) -> dict | None:
    """解析前端传来的列引用：优先 uid，兼容 file_id/sheet_index/column。"""
    if not isinstance(ref, dict):
        return None
    uid = ref.get("uid")
    if uid:
        column = _column(uid)
        if column is not None:
            return column
    try:
        sheet_index = int(ref.get("sheet_index"))
    except (TypeError, ValueError):
        return None
    return _find_column(ref.get("file_id"), sheet_index, ref.get("column"))


def _column_brief(column: dict) -> dict:
    return {
        "uid": column["uid"],
        "file_id": column["file_id"],
        "file_name": column["file_name"],
        "sheet_index": column["sheet_index"],
        "sheet_name": column["sheet_name"],
        "column": column["column"],
        "col_index": column["col_index"],
        "column_dup": column["column_dup"],
        "rows": column["rows"],
    }


def _codes(uid: str):
    cached = _CODES.get(uid)
    if cached is not None:
        return cached
    column = _column(uid)
    if column is None:
        raise KeyError(uid)
    codes = encode(_column_values(column))
    _CODES[uid] = codes
    return codes


# ---------- 相似分组 ----------


def _signature() -> frozenset:
    global _SIGNATURE_CACHE
    if _SIGNATURE_CACHE is None:
        _SIGNATURE_CACHE = frozenset(column["uid"] for column in _columns())
    return _SIGNATURE_CACHE


def _seed_similarities(seed_uid: str, others: list[dict]) -> dict[str, float]:
    cached = _SIM_CACHE.get(seed_uid)
    signature = _signature()
    if cached is not None and cached[0] == signature:
        return cached[1]

    values = similarities(_codes(seed_uid), [_codes(item["uid"]) for item in others])
    sims = {item["uid"]: float(value) for item, value in zip(others, values)}
    _SIM_CACHE[seed_uid] = (signature, sims)
    return sims


def _sim_matrix() -> tuple[dict[str, int], object]:
    """所有列两两相似度矩阵（自动分组用，一次算完并缓存）。"""
    global _MATRIX_CACHE
    signature = _signature()
    if _MATRIX_CACHE is not None and _MATRIX_CACHE[0] == signature:
        return _MATRIX_CACHE[1], _MATRIX_CACHE[2]

    columns = _columns()
    index = {column["uid"]: position for position, column in enumerate(columns)}
    matrix = similarity_matrix([_codes(column["uid"]) for column in columns])
    _MATRIX_CACHE = (signature, index, matrix)
    return index, matrix


def _members(
    seed_uid: str,
    threshold: float,
    excluded: set[str] | None = None,
    pinned: set[str] | None = None,
    align_rows: bool = False,
) -> list[dict] | None:
    """按阈值筛出与基准列相似的列。

    excluded 为用户手动移除、不再自动加回的列；
    pinned 为用户手动换入的列，即使相似度不达标也保留；
    align_rows 为 True 时只自动并入行数与基准列相同的列（自动分组用），
    避免把行数不同的列并进来导致整个任务无法按行号对齐。
    """
    seed = _column(seed_uid)
    if seed is None:
        return None

    excluded = excluded or set()
    pinned = pinned or set()
    others = [item for item in _columns() if item["uid"] != seed_uid]
    sims = _seed_similarities(seed_uid, others)

    members = [] if seed_uid in excluded else [dict(seed, similarity=1.0)]
    matched = []
    for item in others:
        if item["uid"] in excluded:
            continue
        if item["uid"] not in pinned and align_rows and item["rows"] != seed["rows"]:
            continue
        score = sims.get(item["uid"], 0.0)
        if item["uid"] in pinned or (score > 0 and score >= threshold):
            matched.append(dict(item, similarity=round(score, 4)))

    matched.sort(key=lambda item: item["similarity"], reverse=True)
    return members + matched


def _refresh_tasks() -> None:
    """按当前阈值重算所有任务成员；基准列已被删除的任务直接丢弃。"""
    for task_id in list(TASKS):
        task = TASKS[task_id]
        members = _members(
            task["seed_uid"],
            SETTINGS["threshold"],
            task.get("excluded", set()),
            task.get("pinned", set()),
            task.get("align_rows", False),
        )
        if members is None:
            TASKS.pop(task_id, None)
            continue
        changed = [col["uid"] for col in task["columns"]] != [col["uid"] for col in members]
        task["threshold"] = SETTINGS["threshold"]
        task["columns"] = members
        if changed:
            task.pop("diff_rows", None)  # 成员变了，旧的差异行数作废


_REPORT_CACHE: dict[str, tuple[tuple, dict]] = {}


def _task_report(task: dict) -> dict:
    """任务级报告：能否比对、差多少行、为什么不对（左侧任务列表直接展示）。"""
    signature = tuple(col["uid"] for col in task["columns"]) + (round(task["threshold"], 3),)
    cached = _REPORT_CACHE.get(task["id"])
    if cached and cached[0] == signature:
        return cached[1]
    if len(task["columns"]) < 2:
        # 只有一列不是数据错误，属于「还没配上对」，用中性状态提示
        report = {
            "state": "pending",
            "rows": None,
            "diffs": None,
            "reason": "只有 1 列，还没有可对比的列：点「重新选列」再勾一列，或降低阈值后重新分组",
        }
        _REPORT_CACHE[task["id"]] = (signature, report)
        return report
    try:
        result = _compare_task(task)
        diffs = result["diffs"]
        rows = result["rows"]
        if not diffs:
            report = {"state": "ok", "rows": rows, "diffs": 0, "reason": f"{rows} 行全部一致"}
        else:
            numbers = [diff["row"] for diff in diffs]
            shown = "、".join(str(number) for number in numbers[:6])
            if len(numbers) > 6:
                shown += " 等"
            columns = sorted(
                {
                    f'{label.split("｜")[0].rsplit(".", 1)[0]}·{label.split("｜")[-1]}'
                    for diff in diffs
                    for label in diff["differ"]
                }
            )
            report = {
                "state": "diff",
                "rows": rows,
                "diffs": len(diffs),
                "reason": f"第 {shown} 行不一致"
                + (f"，涉及列 {'、'.join(columns)}" if columns else ""),
            }
    except CompareError as error:
        report = {"state": "failed", "rows": None, "diffs": None, "reason": str(error)}
    _REPORT_CACHE[task["id"]] = (signature, report)
    return report


def _task_public(task: dict) -> dict:
    report = _task_report(task)
    state = report["state"]
    ai = task.get("ai_report")
    if ai and ai.get("members") != [col["uid"] for col in task["columns"]]:
        ai = None  # 列变了，旧的分析作废
    return {
        "id": task["id"],
        "name": task["name"],
        "seed_uid": task["seed_uid"],
        "threshold": task["threshold"],
        "columns": task["columns"],
        "pinned": sorted(task.get("pinned", set())),
        "diff_rows": report["diffs"],
        "status": state if state in ("failed", "pending") else "ok",
        "status_hint": report["reason"] if state in ("failed", "pending") else "",
        "report": report,
        "ai_report": ai,
    }


def _clear_caches() -> None:
    global _COLUMNS_CACHE, _INDEX_CACHE, _SIGNATURE_CACHE, _MATRIX_CACHE
    _CODES.clear()
    _SIM_CACHE.clear()
    _MATRIX_CACHE = None
    _REPORT_CACHE.clear()
    _COLUMNS_CACHE = None
    _INDEX_CACHE = None
    _SIGNATURE_CACHE = None


# ---------- 比对与报告 ----------


class CompareError(Exception):
    """比对条件不满足（列数/行数不合格）。"""


def _compare_task(task: dict) -> dict:
    """按行对齐比较任务里的所有列，返回差异明细。"""
    frames = []
    for col in task["columns"]:
        frames.append(
            {
                "uid": col["uid"],
                "file_id": col["file_id"],
                "file_name": col["file_name"],
                "sheet_index": col["sheet_index"],
                "sheet_name": col["sheet_name"],
                "column": col["column"],
                "col_index": col["col_index"],
                "column_dup": col["column_dup"],
                "values": _column_values(col),
            }
        )
    if len(frames) < 2:
        raise CompareError("任务里只有 1 列：其它文件/页没有对应列，无法比对")

    lengths = {len(frame["values"]) for frame in frames}
    if len(lengths) > 1:
        detail = "、".join(
            f'{frame["file_name"]}（第{frame["sheet_index"]}页·{frame["column"]}）{len(frame["values"])} 行'
            for frame in frames
        )
        raise CompareError(f"参与比对的列行数不一致，按行号无法对齐：{detail}")

    labels: list[str] = []
    data: dict[str, list] = {}
    for index, frame in enumerate(frames):
        suffix = f'·第{frame["col_index"] + 1}列' if frame["column_dup"] else ""
        label = f'{frame["file_name"]}｜第{frame["sheet_index"]}页-{frame["column"]}{suffix}'
        if label in data:
            label = f"{label}#{index + 1}"
        labels.append(label)
        data[label] = frame["values"]

    # 行号以第一列（基准列）所在工作表实际表头行 +1 为准
    seed = frames[0]
    sheet = next(
        (
            s
            for s in FILES[seed["file_id"]]["meta"]["sheets"]
            if s["index"] == seed["sheet_index"]
        ),
        None,
    )
    start_row = (sheet.get("header_row", 1) if sheet else 1) + 1
    diffs = compare_columns(data, start_row=start_row)

    per_column = {label: 0 for label in labels}
    detail = []
    for diff in diffs:
        values = {}
        for label in labels:
            raw = diff.values[label]
            if raw is MISSING:
                values[label] = None
            elif raw is None:
                values[label] = ""
            else:
                values[label] = str(raw).strip()
        # 两边都是空/无数据的行不算差异（台账末尾常见的空白行）
        if all(value in (None, "") for value in values.values()):
            continue
        for label in diff.differ_columns:
            per_column[label] += 1
        detail.append({"row": diff.row, "values": values, "differ": list(diff.differ_columns)})

    return {
        "task": {"id": task["id"], "name": task["name"], "threshold": task["threshold"]},
        "columns": [
            {
                "label": labels[index],
                "uid": frame["uid"],
                "file_name": frame["file_name"],
                "sheet_index": frame["sheet_index"],
                "sheet_name": frame["sheet_name"],
                "column": frame["column"],
                "col_index": frame["col_index"],
                "column_dup": frame["column_dup"],
                "rows": len(frame["values"]),
            }
            for index, frame in enumerate(frames)
        ],
        "rows": max((len(frame["values"]) for frame in frames), default=0),
        "start_row": start_row,
        "diffs": detail,
        "per_column": [{"label": label, "diffs": per_column[label]} for label in labels],
    }


# ---------- 文件解析 ----------


def _sheet_names(path: Path) -> list[str | None]:
    names = sheet_names(path)
    return list(names) if names else [None]


def _detect_header_row(path: Path, sheet: str | None = None) -> int:
    """自动识别表头行：取前几行里「非空单元格多、且基本都是文本」的一行。

    台账第 1 行常是合并大标题（非空单元格很少），真正的表头紧随其后；
    数据行文本占比可能也很高，所以得分接近时取更早的那一行。
    """
    scored: list[tuple[int, float]] = []
    for index, row in enumerate(preview_rows(path, sheet, limit=8), start=1):
        cells = [cell for cell in row if cell not in (None, "")]
        if len(cells) < 2:
            continue
        texts = sum(1 for cell in cells if isinstance(cell, str))
        ratio = texts / len(cells)
        if ratio < 0.6:
            continue
        scored.append((index, len(cells) * ratio))

    if not scored:
        return 1
    best = max(score for _, score in scored)
    for index, score in scored:
        if score >= best * 0.9:
            return index
    return 1


def _describe(
    path: Path, header_row: int | None = None
) -> tuple[list[dict], dict[int, list[list]]]:
    """解析文件结构，同时把已经读出来的列数据带回去，避免二次解析。

    header_row 为 None 时按页自动识别表头行。
    """
    sheets: list[dict] = []
    parsed: dict[int, list[list]] = {}
    cells = 0
    for index, sheet_name in enumerate(_sheet_names(path), start=1):
        row = header_row or _detect_header_row(path, sheet_name)
        try:
            columns = load_columns(path, None, sheet_name, header_row=row)
        except ValueError:
            columns = {}
        values = list(columns.values())
        cells += sum(len(column) for column in values)
        sheets.append(
            {
                "index": index,
                "name": sheet_name or path.stem,
                "columns": list(columns),
                "rows": len(values[0]) if values else 0,
                "header_row": row,
            }
        )
        parsed[index] = values
    # 数据量特别大时不再常驻内存，改为用到时按页解析
    if cells > 4_000_000:
        parsed.clear()
    return sheets, parsed


# ---------- 页面与文件接口 ----------


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "templates", "index.html")


@app.get("/api/state")
def get_state():
    return jsonify(
        {
            "threshold": SETTINGS["threshold"],
            "gpu": CUDA_AVAILABLE,
            "device": device_name(),
            "files": [item["meta"] for item in FILES.values()],
            "tasks": [_task_public(task) for task in TASKS.values()],
        }
    )


@app.post("/api/files")
def upload_file():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "没有收到文件"}), 400

    file_name = Path(upload.filename).name
    suffix = Path(file_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return jsonify({"error": f"不支持的文件类型: {suffix or '未知'}"}), 400

    file_id = uuid.uuid4().hex
    target = UPLOAD_DIR / f"{file_id}{suffix}"
    upload.save(target)

    try:
        sheets, parsed = _describe(target)
    except Exception as exc:
        target.unlink(missing_ok=True)
        return jsonify({"error": f"解析失败: {exc}"}), 400

    meta = {
        "id": file_id,
        "name": file_name,
        "size": target.stat().st_size,
        "header_row": None,  # None 表示自动识别（每页可能不同）
        "sheets": sheets,
    }
    FILES[file_id] = {"path": target, "meta": meta, "data": parsed}

    _clear_caches()
    # 导入后不自动归组，等用户点「自动分组」确认
    return jsonify(meta), 201


@app.delete("/api/files/<file_id>")
def remove_file(file_id: str):
    item = FILES.pop(file_id, None)
    if item is None:
        return jsonify({"error": "文件不存在"}), 404
    item["path"].unlink(missing_ok=True)

    _clear_caches()
    for task_id in list(TASKS):
        task = TASKS[task_id]
        if task["seed_uid"].startswith(f"{file_id}-"):
            TASKS.pop(task_id, None)
            continue
        task["columns"] = [col for col in task["columns"] if col["file_id"] != file_id]
        prefix = f"{file_id}-"
        task["excluded"] = {uid for uid in task.get("excluded", set()) if not uid.startswith(prefix)}
        task["pinned"] = {uid for uid in task.get("pinned", set()) if not uid.startswith(prefix)}

    return jsonify({"ok": True})


@app.patch("/api/files/<file_id>")
def update_file(file_id: str):
    """调整表头所在行，重新解析该文件（列名和数据范围都会随之变化）。"""
    item = FILES.get(file_id)
    if item is None:
        return jsonify({"error": "文件不存在"}), 404

    payload = request.get_json(silent=True) or {}
    header_row = payload.get("header_row")
    if header_row is None:
        header_row = None  # 恢复自动识别
    elif not isinstance(header_row, int) or not 1 <= header_row <= 50:
        return jsonify({"error": "表头行需为 1~50 的整数"}), 400

    try:
        sheets, parsed = _describe(item["path"], header_row)
    except Exception as exc:
        return jsonify({"error": f"解析失败: {exc}"}), 400

    item["meta"]["header_row"] = header_row
    item["meta"]["sheets"] = sheets
    item["data"] = parsed

    _clear_caches()
    _refresh_tasks()
    return jsonify(item["meta"])


# ---------- 任务接口 ----------


@app.post("/api/tasks")
def create_task():
    payload = request.get_json(silent=True) or {}
    seed = _find_column(
        payload.get("file_id"),
        payload.get("sheet_index"),
        payload.get("column"),
    )
    if seed is None:
        return jsonify({"error": "找不到指定的列"}), 404

    return jsonify(_task_public(_new_task(seed))), 201


@app.post("/api/tasks/batch")
def create_tasks():
    """一次按多个基准列建任务（每个选中的列一个任务，成员由阈值自动归组）。"""
    payload = request.get_json(silent=True) or {}
    wanted = payload.get("columns")
    if not isinstance(wanted, list) or not wanted:
        return jsonify({"error": "没有选择列"}), 400

    # 已归入任务的列不再重复建任务，避免同一组出现多个任务
    assigned = {col["uid"] for task in TASKS.values() for col in task["columns"]}

    created = []
    skipped = 0
    for item in wanted:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            sheet_index = int(item.get("sheet_index"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        seed = _find_column(item.get("file_id"), sheet_index, item.get("column"))
        if seed is None or seed["uid"] in assigned:
            skipped += 1
            continue

        task = _new_task(seed)
        assigned.update(col["uid"] for col in task["columns"])
        created.append(task)

    return (
        jsonify(
            {
                "created": [_task_public(task) for task in created],
                "count": len(created),
                "skipped": skipped,
            }
        ),
        201,
    )


@app.patch("/api/tasks/<task_id>")
def update_task(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404

    payload = request.get_json(silent=True) or {}
    if "name" not in payload:
        return jsonify({"error": "没有需要修改的字段"}), 400

    name = str(payload["name"]).strip()
    if not name:
        return jsonify({"error": "任务名不能为空"}), 400

    task["name"] = name[:80]
    return jsonify(_task_public(task))


@app.delete("/api/tasks/<task_id>")
def delete_task(task_id: str):
    if TASKS.pop(task_id, None) is None:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"ok": True})


@app.put("/api/tasks/<task_id>/columns")
def set_task_columns(task_id: str):
    """重新指定任务包含哪些列，用于纠正自动分组（可把列从其它任务移过来）。"""
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404

    payload = request.get_json(silent=True) or {}
    raw = payload.get("uids")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "至少选择一列"}), 400

    uids: list[str] = []
    for item in raw:
        uid = str(item)
        if uid not in uids:
            uids.append(uid)
    for uid in uids:
        if _column(uid) is None:
            return jsonify({"error": f"列不存在：{uid}"}), 400

    wanted = set(uids)

    # 一列只属于一个任务：被选走的列从其它任务里摘掉
    for other_id in list(TASKS):
        if other_id == task_id:
            continue
        other = TASKS[other_id]
        if not any(col["uid"] in wanted for col in other["columns"]):
            continue
        for col in other["columns"]:
            if col["uid"] in wanted:
                other.setdefault("excluded", set()).add(col["uid"])
                other.setdefault("pinned", set()).discard(col["uid"])
        if other["seed_uid"] in wanted:
            remaining = [col["uid"] for col in other["columns"] if col["uid"] not in wanted]
            if not remaining:
                TASKS.pop(other_id, None)
                continue
            old_seed = _column(other["seed_uid"])
            other["seed_uid"] = remaining[0]
            new_seed = _column(remaining[0])
            if old_seed and new_seed and other["name"] == _task_name(old_seed):
                other["name"] = _task_name(new_seed)

    # 基准列被取消勾选时，换成本次选择的第一个列，任务名同步
    if task["seed_uid"] not in wanted:
        old_seed = _column(task["seed_uid"])
        task["seed_uid"] = uids[0]
        new_seed = _column(uids[0])
        if old_seed and new_seed and task["name"] == _task_name(old_seed):
            task["name"] = _task_name(new_seed)

    # 选中的列固定保留，未选中的列排除，避免按阈值重算时又加回来
    task["excluded"] = set()
    task["pinned"] = {uid for uid in uids if uid != task["seed_uid"]}
    for col in _columns():
        if col["uid"] not in wanted:
            task["excluded"].add(col["uid"])

    _refresh_tasks()
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务已不存在"}), 404
    return jsonify(_task_public(task))


@app.delete("/api/tasks/<task_id>/columns/<uid>")
def remove_task_column(task_id: str, uid: str):
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404

    remaining = [col for col in task["columns"] if col["uid"] != uid]
    if len(remaining) == len(task["columns"]):
        return jsonify({"error": "该列不在任务中"}), 404

    task["columns"] = remaining
    task.setdefault("excluded", set()).add(uid)  # 调整阈值时不再自动加回
    task.setdefault("pinned", set()).discard(uid)
    return jsonify(_task_public(task))


@app.patch("/api/tasks/<task_id>/columns/<uid>")
def replace_task_column(task_id: str, uid: str):
    """原地换列：把任务里已有的列换成另一个文件/列，不用删了重建。"""
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404

    payload = request.get_json(silent=True) or {}
    new_uid = str(payload.get("uid") or "").strip()
    if not new_uid:
        return jsonify({"error": "缺少目标列"}), 400

    current = {col["uid"] for col in task["columns"]}
    if uid not in current:
        return jsonify({"error": "该列不在任务中"}), 404
    if new_uid == uid:
        return jsonify(_task_public(task))
    if new_uid in current:
        return jsonify({"error": "目标列已在任务中"}), 400

    old = _column(uid)
    new = _column(new_uid)
    if old is None or new is None:
        return jsonify({"error": "目标列不存在"}), 404

    excluded = task.setdefault("excluded", set())
    pinned = task.setdefault("pinned", set())
    excluded.discard(new_uid)

    if uid == task["seed_uid"]:
        # 换掉的是基准列：任务跟着换基准，名称同步成新列的「文件名-页码-列名」
        auto_name = _task_name(old)
        if task["name"] == auto_name:
            task["name"] = _task_name(new)
        task["seed_uid"] = new_uid
        pinned.discard(new_uid)
    else:
        excluded.add(uid)
        pinned.discard(uid)
        pinned.add(new_uid)  # 手选的列不受阈值影响，始终保留

    _refresh_tasks()
    return jsonify(_task_public(task))


@app.delete("/api/tasks")
def clear_tasks():
    """清空全部任务，保留已导入的文件。"""
    TASKS.clear()
    return jsonify({"ok": True})


@app.delete("/api/all")
def clear_all():
    """一键清空：移除全部文件与任务。"""
    for item in FILES.values():
        item["path"].unlink(missing_ok=True)
    FILES.clear()
    TASKS.clear()
    _clear_caches()
    return jsonify({"ok": True})


@app.post("/api/auto-group")
def auto_group():
    """一键自动分组：把尚未归组的列按当前阈值聚类，每类建一个任务。"""
    threshold = SETTINGS["threshold"]
    _refresh_tasks()  # 新导入的相似列并入已有任务
    columns = _columns()

    assigned: set[str] = set()
    for task in TASKS.values():
        assigned.update(col["uid"] for col in task["columns"])
        assigned.update(task.get("excluded", set()))

    created = []
    skipped = 0
    skipped_items: list[dict] = []
    if columns:
        index, matrix = _sim_matrix()  # 所有列一次性算完，避免逐列重复计算
    else:
        index, matrix = {}, None
    for seed in columns:
        if seed["uid"] in assigned:
            continue
        row = matrix[index[seed["uid"]]]
        group = [
            item["uid"]
            for item in columns
            if item["uid"] != seed["uid"]
            and item["uid"] not in assigned
            and item["rows"] == seed["rows"]  # 行数不同无法按行号对齐，不自动并入
            and row[index[item["uid"]]] > 0
            and row[index[item["uid"]]] >= threshold
        ]
        if not group:
            skipped += 1
            if len(skipped_items) < 20:
                # 区分原因：相似度不够 / 没有同名列 / 行数不同
                others = [item for item in columns if item["uid"] != seed["uid"]]
                same_rows = [item for item in others if item["rows"] == seed["rows"]]
                if any(row[index[item["uid"]]] > 0 for item in same_rows):
                    reason = "相似度不足"
                elif any(item["column"] == seed["column"] for item in same_rows):
                    reason = "同名但内容差异过大"
                elif same_rows:
                    reason = "无同名列"
                else:
                    reason = "行数不同或无同名列"
                skipped_items.append(
                    {"name": f'{seed["file_name"]}·{seed["column"]}', "reason": reason}
                )
            continue

        assigned.update(group)
        assigned.add(seed["uid"])
        task = {
            "id": uuid.uuid4().hex[:12],
            "name": _task_name(seed),
            "seed_uid": seed["uid"],
            "threshold": threshold,
            "columns": [],
            "excluded": set(),
            "pinned": set(),
            "align_rows": True,
        }
        TASKS[task["id"]] = task
        created.append(task)

    _refresh_tasks()

    # 自动跑一遍比对，把差异行数带到任务列表上
    total_diffs = 0
    for task in created:
        try:
            result = _compare_task(task)
        except CompareError:
            continue
        task["diff_rows"] = len(result["diffs"])
        total_diffs += task["diff_rows"]

    return jsonify(
        {
            "created": [_task_public(task) for task in created],
            "count": len(created),
            "skipped": skipped,
            "skipped_items": skipped_items,
            "columns": len(columns),
            "threshold": threshold,
            "diffs": total_diffs,
        }
    )


@app.get("/api/tasks/<task_id>/compare")
def compare_task(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    try:
        result = _compare_task(task)
    except CompareError as exc:
        return jsonify({"error": str(exc)}), 409
    task["diff_rows"] = len(result["diffs"])
    return jsonify(result)


@app.post("/api/set-filter")
def set_filter():
    """集合筛选：把 M 中每个属于集合 N 的元素按文件-页码-列列出所在行号。"""
    payload = request.get_json(silent=True) or {}
    base_refs = payload.get("base") if isinstance(payload.get("base"), list) else []
    target_refs = payload.get("target") if isinstance(payload.get("target"), list) else []

    base_columns: list[dict] = []
    for ref in base_refs:
        column = _column_ref(ref)
        if column and all(item["uid"] != column["uid"] for item in base_columns):
            base_columns.append(column)

    target_columns: list[dict] = []
    for ref in target_refs:
        column = _column_ref(ref)
        if column and all(item["uid"] != column["uid"] for item in target_columns):
            target_columns.append(column)

    if not base_columns:
        return jsonify({"error": "请先选择基准集合 N 的列"}), 400
    if not target_columns:
        return jsonify({"error": "请选择要查找的范围 M 的列"}), 400

    # N：多列数据值合并去重（不含列名，忽略空值）
    base_values: list[str] = []
    base_seen: set[str] = set()
    for column in base_columns:
        for raw in _column_values(column):
            key = _norm_value(raw)
            if key and key not in base_seen:
                base_seen.add(key)
                base_values.append(key)

    # M：逐行检查，值属于 N 就记录它的文件/页/列/行号
    occurrences: list[dict] = []
    found: set[str] = set()
    matched_by_column: dict[str, int] = {}
    truncated = False
    for column in target_columns:
        start_row = _header_row(column) + 1
        matched = 0
        for index, raw in enumerate(_column_values(column)):
            key = _norm_value(raw)
            if not key or key not in base_seen:
                continue
            matched += 1
            found.add(key)
            if len(occurrences) >= MAX_SET_ROWS:
                truncated = True
                continue
            occurrences.append(
                {
                    "value": key,
                    "uid": column["uid"],
                    "file_id": column["file_id"],
                    "file_name": column["file_name"],
                    "sheet_index": column["sheet_index"],
                    "sheet_name": column["sheet_name"],
                    "column": column["column"],
                    "col_index": column["col_index"],
                    "column_dup": column["column_dup"],
                    "row": start_row + index,
                }
            )
        matched_by_column[column["uid"]] = matched

    missing = [value for value in base_values if value not in found]
    return jsonify(
        {
            "base": {
                "columns": [_column_brief(column) for column in base_columns],
                "count": len(base_values),
                "missing": missing[:MAX_SET_MISSING],
                "missing_count": len(missing),
                "missing_truncated": len(missing) > MAX_SET_MISSING,
            },
            "target": {
                "columns": [
                    {**_column_brief(column), "matched": matched_by_column[column["uid"]]}
                    for column in target_columns
                ],
                "matched_rows": sum(matched_by_column.values()),
                "matched_values": len(found),
                "occurrences": occurrences,
            },
            "truncated": truncated,
            "limit": MAX_SET_ROWS,
        }
    )


@app.get("/api/tasks/<task_id>/report.xlsx")
def export_report(task_id: str):
    """导出比对报告：概览 + 差异明细（差异单元格标红）。"""
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    try:
        result = _compare_task(task)
    except CompareError as exc:
        return jsonify({"error": str(exc)}), 409

    labels = [col["label"] for col in result["columns"]]
    workbook = openpyxl.Workbook()

    sheet = workbook.active
    sheet.title = "概览"
    sheet.append(["比对报告"])
    sheet["A1"].font = Font(size=14, bold=True)
    sheet.append([])
    sheet.append(["任务名称", result["task"]["name"]])
    sheet.append(["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    sheet.append(["相似阈值", f'{round(result["task"]["threshold"] * 100)}%'])
    sheet.append(["数据起始行", f'第 {result["start_row"]} 行（第 1 行为表头）'])
    sheet.append(["参与行数", result["rows"]])
    sheet.append(["差异行数", len(result["diffs"])])
    sheet.append([])
    sheet.append(["文件", "页", "列", "数据行", "差异行"])
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)
    for col, stat in zip(result["columns"], result["per_column"]):
        sheet.append([col["file_name"], col["sheet_index"], col["column"], col["rows"], stat["diffs"]])
    for column, width in zip("ABCDE", (52, 8, 30, 10, 10)):
        sheet.column_dimensions[column].width = width

    detail = workbook.create_sheet("差异明细")
    detail.append(["行号", *labels, "不同列"])
    for cell in detail[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center")
        cell.fill = PatternFill("solid", fgColor="EDE7DA")

    mark = PatternFill("solid", fgColor="FBD9D2")
    for diff in result["diffs"]:
        values = [
            "<无数据>" if diff["values"][label] is None else diff["values"][label]
            for label in labels
        ]
        detail.append([diff["row"], *values, "、".join(diff["differ"])])
        for offset, label in enumerate(labels, start=2):
            cell = detail.cell(row=detail.max_row, column=offset)
            if label in diff["differ"]:
                cell.fill = mark
    detail.column_dimensions["A"].width = 8
    for index in range(len(labels)):
        detail.column_dimensions[openpyxl.utils.get_column_letter(index + 2)].width = 26
    detail.column_dimensions[openpyxl.utils.get_column_letter(len(labels) + 2)].width = 30
    detail.freeze_panes = "A2"

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'{result["task"]["name"]}-比对报告.xlsx',
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/ai")
def get_ai_settings():
    return jsonify(ai_settings_public())


@app.put("/api/ai")
def put_ai_settings():
    try:
        return jsonify(save_ai_settings(request.get_json(silent=True) or {}))
    except AiError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/ai/test")
def test_ai_settings():
    try:
        return jsonify(test_connection())
    except AiError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/tasks/<task_id>/ai-report")
def generate_ai_report(task_id: str):
    """让配置好的大模型解读比对结果。"""
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    try:
        result = _compare_task(task)
    except CompareError as exc:
        return jsonify({"error": str(exc)}), 409
    try:
        data = request_analysis(result)
    except AiError as exc:
        return jsonify({"error": str(exc)}), 400
    data["members"] = [col["uid"] for col in task["columns"]]
    task["ai_report"] = data
    return jsonify(data)


@app.post("/api/tasks/<task_id>/report.html")
def export_report_html(task_id: str):
    """导出单文件 HTML 报告（可直接打印成 PDF），可附带 AI 分析。"""
    task = TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    try:
        result = _compare_task(task)
    except CompareError as exc:
        return jsonify({"error": str(exc)}), 409

    body = request.get_json(silent=True) or {}
    ai = body.get("ai") if isinstance(body.get("ai"), dict) else task.get("ai_report")
    if ai and ai.get("markdown"):
        ai = {"markdown": ai["markdown"], "model": ai.get("model", ""), "time": ai.get("time", "")}
    else:
        ai = None

    page = render_report_html(result, ai)
    buffer = BytesIO(page.encode("utf-8"))
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'{result["task"]["name"]}-比对报告.html',
        mimetype="text/html",
    )


@app.patch("/api/settings")
def update_settings():
    payload = request.get_json(silent=True) or {}
    try:
        threshold = float(payload.get("threshold"))
    except (TypeError, ValueError):
        return jsonify({"error": "阈值必须是数字"}), 400
    if not 0 <= threshold <= 1:
        return jsonify({"error": "阈值需在 0~1 之间"}), 400

    SETTINGS["threshold"] = threshold
    _refresh_tasks()
    return jsonify(
        {
            "threshold": threshold,
            "tasks": [_task_public(task) for task in TASKS.values()],
        }
    )


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({"error": "文件超过 64MB 限制"}), 413


if __name__ == "__main__":
    import sys
    import threading
    import webbrowser

    url = "http://127.0.0.1:5000"
    print(f"列比对服务： {url}  (相似度设备: {device_name()})")
    if getattr(sys, "frozen", False):  # 打包后的 exe：启动后自动打开浏览器
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
