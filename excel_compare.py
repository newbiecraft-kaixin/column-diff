"""Excel / CSV 列比对工具。

核心能力：
1. 加载 .xlsx / .xlsm / .csv 文件中的列数据
2. 比较两列或多列，只返回不一致的记录（完全相同的行不输出）

作为模块使用（两个函数：加载 + 比对）：
    from excel_compare import load_columns, compare_columns

    cols = load_columns("数据.xlsx", ["手机号", "联系电话"])
    diffs = compare_columns(cols, start_row=2)

命令行使用：
    python excel_compare.py 数据.xlsx -c 手机号 联系电话
    python excel_compare.py 数据.xlsx -c 手机号 联系电话 备用电话 --sheet Sheet1
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any, Sequence

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

# 某个列在该行缺少数据（行长度不足）时的占位标记
MISSING = object()

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}


@dataclass(frozen=True)
class Diff:
    """一行比对不一致的记录。"""

    row: int
    values: dict[str, Any]
    differ_columns: tuple[str, ...]

    @property
    def base_name(self) -> str:
        return next(iter(self.values))

    def __str__(self) -> str:
        base_name = self.base_name
        base_value = _show(self.values[base_name])
        detail = "；".join(
            f"{base_name}={base_value}  ≠  {name}={_show(self.values[name])}"
            for name in self.differ_columns
        )
        return f"第{self.row}行  {detail}"


def _show(value: Any) -> str:
    """把单元格值转成便于阅读的字符串。"""
    if value is MISSING:
        return "<无数据>"
    if value is None:
        return "<空>"
    if isinstance(value, str):
        return repr(value)
    return str(value)


def _key(value: Any, strip: bool, ignore_case: bool) -> Any:
    """把值归一化成可比较的形式。"""
    if isinstance(value, str):
        text = value.strip() if strip else value
        return text.casefold() if ignore_case else text
    return value


def _equal(
    left: Any,
    right: Any,
    strip: bool = True,
    ignore_case: bool = False,
) -> bool:
    return _key(left, strip, ignore_case) == _key(right, strip, ignore_case)


def compare_columns(
    columns: dict[str, Sequence[Any]],
    start_row: int = 1,
    *,
    strip: bool = True,
    ignore_case: bool = False,
) -> list[Diff]:
    """按行比较两列或多列，返回所有取值不一致的记录。

    以第一列为基准，其余列与之逐行比较；某行只要有一列不同就输出该行，
    并标明是哪些列不同。所有列都相同的行不会出现在结果里。

    :param columns: 列名 -> 列数据（不含表头），顺序即比较顺序
    :param start_row: 第一行数据对应的 Excel 行号
    :param strip: 是否忽略字符串首尾空白
    :param ignore_case: 是否忽略大小写
    :return: 差异列表
    """
    if len(columns) < 2:
        raise ValueError("至少需要两列才能比对")

    names = list(columns)
    total = max(len(values) for values in columns.values())
    diffs: list[Diff] = []

    for i in range(total):
        row_values: dict[str, Any] = {}
        for name in names:
            values = columns[name]
            row_values[name] = values[i] if i < len(values) else MISSING

        base = row_values[names[0]]
        differ = tuple(
            name
            for name in names[1:]
            if not _equal(base, row_values[name], strip, ignore_case)
        )
        if differ:
            diffs.append(Diff(row=i + start_row, values=row_values, differ_columns=differ))

    return diffs


try:  # 可选：Rust 实现的读取器，大文件解析比 openpyxl 快 5~10 倍
    from python_calamine import CalamineWorkbook
except Exception:  # 未安装则退回 openpyxl
    CalamineWorkbook = None


def _read_excel_rows(path: Path, sheet: str | None) -> list[list[Any]]:
    """读取 Excel 为二维列表：装了 python-calamine 走快路径，失败再退回 openpyxl。"""
    if CalamineWorkbook is not None and path.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            workbook = CalamineWorkbook.from_path(str(path))
            name = sheet or workbook.sheet_names[0]
            return [list(row) for row in workbook.get_sheet_by_name(name).to_python()]
        except Exception:
            pass

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        worksheet = workbook[sheet] if sheet else workbook.active
        return [list(row) for row in worksheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def sheet_names(path: str | Path) -> list[str]:
    """列出工作表名（calamine 可用时秒开，否则回退 openpyxl）。"""
    file_path = Path(path)
    if file_path.suffix.lower() not in EXCEL_SUFFIXES:
        return []
    if CalamineWorkbook is not None and file_path.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            return list(CalamineWorkbook.from_path(str(file_path)).sheet_names)
        except Exception:
            pass
    workbook = openpyxl.load_workbook(file_path, read_only=True)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def preview_rows(path: str | Path, sheet: str | None = None, limit: int = 8) -> list[list[Any]]:
    """读取文件开头的若干原始行，用于自动识别表头在第几行。"""
    try:
        return _read_rows(Path(path), sheet, 1)[:limit]
    except Exception:
        return []


def _read_rows(path: Path, sheet: str | None, header_row: int) -> list[list[Any]]:
    """读取文件为二维列表，返回从表头行开始的数据。"""
    suffix = path.suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        rows = _read_excel_rows(path, sheet)
    elif suffix == ".csv":
        rows = []
        for encoding in ("utf-8-sig", "gbk"):
            try:
                with path.open("r", encoding=encoding, newline="") as file:
                    rows = [row for row in csv.reader(file)]
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(f"无法识别 CSV 编码: {path}")
    elif suffix == ".xls":
        raise ValueError("不支持 .xls 格式，请另存为 .xlsx 后再比对")
    else:
        raise ValueError(f"不支持的文件类型: {suffix}")

    if header_row < 1:
        raise ValueError("表头行号必须从 1 开始")
    if len(rows) < header_row:
        raise ValueError(f"文件内容不足 {header_row} 行，无法读取表头")
    return rows[header_row - 1 :]


def _dedupe_names(names: list[str]) -> list[str]:
    """表头去重，重名的列追加序号。"""
    seen: dict[str, int] = {}
    result: list[str] = []
    for index, name in enumerate(names, start=1):
        if name not in seen:
            seen[name] = 1
            result.append(name)
            continue
        seen[name] += 1
        result.append(f"{name}({seen[name]})")
    return result


def _resolve_index(spec: str | int, names: list[str]) -> int:
    """把列名、列字母或 1 起始的序号解析成 0 起始下标。"""
    if isinstance(spec, int):
        index = spec - 1
    else:
        text = str(spec).strip()
        if text in names:
            index = names.index(text)
        elif re.fullmatch(r"[A-Za-z]{1,3}", text):
            index = column_index_from_string(text.upper()) - 1
        else:
            raise KeyError(f"找不到列: {spec}")
    if not 0 <= index < len(names):
        raise IndexError(f"列 {spec} 超出范围，当前共 {len(names)} 列")
    return index


def load_columns(
    path: str | Path,
    columns: Sequence[str | int] | None = None,
    sheet: str | None = None,
    header_row: int = 1,
) -> dict[str, list[Any]]:
    """加载指定列的数据。

    :param path: 文件路径，支持 .xlsx / .xlsm / .csv
    :param columns: 要读取的列，可写列名、列字母（A/B/C）或 1 起始序号；None 表示全部列
    :param sheet: 工作表名，None 表示活动工作表
    :param header_row: 表头所在行号，默认 1
    :return: 列名 -> 列数据（不含表头），行号从 header_row + 1 开始
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    rows = _read_rows(file_path, sheet, header_row)
    header = ["" if cell is None else str(cell).strip() for cell in rows[0]]
    data = rows[1:]

    width = max([len(header)] + [len(row) for row in data])
    header += ["" for _ in range(width - len(header))]

    # 空表头用列字母补位，保证每列都有名字
    header = [
        name if name else get_column_letter(index)
        for index, name in enumerate(header, start=1)
    ]
    names = _dedupe_names(header)

    if columns is None:
        selected = list(range(width))
    else:
        selected = [_resolve_index(spec, names) for spec in columns]

    result: dict[str, list[Any]] = {}
    # 用 zip_longest 在 C 层完成转置，比逐行逐列取值的 Python 循环快得多
    transposed = list(zip_longest(*data, fillvalue=None)) if data else []
    for index in selected:
        result[names[index]] = list(transposed[index]) if index < len(transposed) else []
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="比较 Excel / CSV 中两列或多列是否一致")
    parser.add_argument("path", help="文件路径（.xlsx/.xlsm/.csv）")
    parser.add_argument(
        "-c",
        "--columns",
        nargs="+",
        required=True,
        help="要比较的列（列名或列字母），至少两个",
    )
    parser.add_argument("--sheet", help="工作表名，默认活动工作表")
    parser.add_argument("--header-row", type=int, default=1, help="表头行号，默认 1")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="忽略大小写")
    parser.add_argument("--keep-spaces", action="store_true", help="不忽略首尾空格")
    args = parser.parse_args(argv)

    if len(args.columns) < 2:
        parser.error("至少需要指定两列")

    loaded = load_columns(args.path, args.columns, args.sheet, args.header_row)
    start_row = args.header_row + 1
    strip = not args.keep_spaces

    diffs = compare_columns(
        loaded,
        start_row,
        strip=strip,
        ignore_case=args.ignore_case,
    )

    for diff in diffs:
        print(diff)

    total = max(len(values) for values in loaded.values())
    print(f"\n比对列: {' vs '.join(loaded)}")
    print(f"数据行数: {total}，差异 {len(diffs)} 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
