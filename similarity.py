"""列相似度计算：优先使用 GPU（torch），不可用时回退 numpy。

相似度定义：两列按行对齐，只统计「至少一方非空」的行，
相似度 = 取值相同的行数 / 参与比较的行数。
"""

from __future__ import annotations

import hashlib

import numpy as np

# 注意顺序：Windows 下先 import pandas 会让 torch 的 DLL 初始化失败
try:
    import torch

    CUDA_AVAILABLE = torch.cuda.is_available()
except Exception:  # 未安装 torch 或驱动不可用
    torch = None
    CUDA_AVAILABLE = False

try:
    import pandas as pd
except Exception:  # 未安装 pandas 时退回逐条哈希
    pd = None

# 单块候选矩阵的单元格数量，控制显存/内存占用（int64 约 8 字节/格）
CHUNK_CELLS = 8_000_000


def device_name() -> str:
    if CUDA_AVAILABLE:
        try:
            return torch.cuda.get_device_name(0)
        except Exception:
            return "CUDA"
    return "CPU (numpy)"


def canonical(value) -> str:
    """归一化单元格值：去空白、忽略大小写、整数值去掉小数点。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip().casefold()


_CODE_MEMO: dict[str, int] = {}
_CODE_MEMO_LIMIT = 500_000


def _code(text: str) -> int:
    """把文本稳定地映射成正的 int64，保证不同列之间可直接比较。

    台账里重复取值多，用字典记住文本→编码，省掉重复的哈希计算。
    """
    cached = _CODE_MEMO.get(text)
    if cached is not None:
        return cached
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    code = (int.from_bytes(digest, "little") >> 1) or 1
    if len(_CODE_MEMO) >= _CODE_MEMO_LIMIT:
        _CODE_MEMO.clear()
    _CODE_MEMO[text] = code
    return code


def _hash_texts(texts: list[str]) -> np.ndarray:
    """把一批文本批量哈希成正 int64；有 pandas 时走 C 实现，速度约快 10 倍。"""
    if pd is not None:
        hashed = pd.util.hash_pandas_object(
            pd.Series(texts, dtype="object"), index=False
        ).to_numpy(np.uint64)
        return (hashed >> 1).astype(np.int64)
    return np.array([_code(text) for text in texts], dtype=np.int64)


def encode(values: list) -> np.ndarray:
    """把一列值编码成 int64 数组，空值记为 0。

    编码在列之间保持一致（同一个值永远是同一个编码），
    这样才能跨列比较是否相同。
    """
    codes = np.zeros(len(values), dtype=np.int64)
    texts = [canonical(value) for value in values]

    # 只对去重后的文本做哈希，台账里重复值多时可省掉大量计算
    unique: dict[str, int] = {}
    for text in texts:
        if text and text not in unique:
            unique[text] = 0
    if not unique:
        return codes

    keys = list(unique)
    hashed = _hash_texts(keys)
    for index, text in enumerate(keys):
        unique[text] = int(hashed[index])
    for index, text in enumerate(texts):
        if text:
            codes[index] = unique[text]
    return codes


def similarities(seed: np.ndarray, candidates: list[np.ndarray]) -> np.ndarray:
    """计算 seed 列与每个候选列的相似度，返回 0~1 的 float32 数组。"""
    if not candidates:
        return np.zeros(0, dtype=np.float32)

    max_len = max([len(seed)] + [len(item) for item in candidates])
    if max_len == 0:
        return np.zeros(len(candidates), dtype=np.float32)

    seed_pad = np.zeros(max_len, dtype=np.int64)
    seed_pad[: len(seed)] = seed

    result = np.zeros(len(candidates), dtype=np.float32)
    rows_per_chunk = max(1, CHUNK_CELLS // max_len)
    seed_tensor = torch.from_numpy(seed_pad).to("cuda") if CUDA_AVAILABLE else None

    for start in range(0, len(candidates), rows_per_chunk):
        chunk = candidates[start : start + rows_per_chunk]
        block = np.zeros((len(chunk), max_len), dtype=np.int64)
        for row, array in enumerate(chunk):
            block[row, : len(array)] = array

        if CUDA_AVAILABLE:
            block_tensor = torch.from_numpy(block).to("cuda", non_blocking=True)
            compared_mask = (block_tensor != 0) | (seed_tensor != 0)
            match_mask = (block_tensor == seed_tensor) & (block_tensor != 0)
            compared = compared_mask.sum(dim=1)
            matched = match_mask.sum(dim=1).float()
            sim = torch.where(
                compared > 0,
                matched / compared.clamp(min=1).float(),
                torch.zeros_like(matched),
            )
            result[start : start + len(chunk)] = sim.cpu().numpy()
        else:
            compared_mask = (block != 0) | (seed_pad != 0)
            match_mask = (block == seed_pad) & (block != 0)
            compared = compared_mask.sum(axis=1)
            matched = match_mask.sum(axis=1)
            result[start : start + len(chunk)] = np.where(
                compared > 0,
                matched / np.maximum(compared, 1),
                0.0,
            )

    return result


def similarity_matrix(codes: list[np.ndarray]) -> np.ndarray:
    """一次算出所有列两两之间的相似度（n×n float32），对角线为 1。

    自动分组需要对每一列都和其他列比较；逐列调用 similarities 会把同样的
    编码反复搬到 GPU、反复建大矩阵，列一多就慢到像卡死。这里把所有列补齐成
    一个矩阵，分块一次性算完。
    """
    count = len(codes)
    matrix = np.eye(count, dtype=np.float32)
    if count < 2:
        return matrix

    max_len = max((len(item) for item in codes), default=0)
    if max_len == 0:
        return matrix

    padded = np.zeros((count, max_len), dtype=np.int64)
    for row, array in enumerate(codes):
        padded[row, : len(array)] = array

    # 每块参与比较的格子数不超过 CHUNK_CELLS，避免一次吃满内存/显存
    rows_per_chunk = max(1, CHUNK_CELLS // max(count * max_len, 1))
    base = torch.from_numpy(padded).to("cuda") if CUDA_AVAILABLE else None

    for start in range(0, count, rows_per_chunk):
        block = padded[start : start + rows_per_chunk]
        if CUDA_AVAILABLE:
            block_tensor = torch.from_numpy(block).to("cuda", non_blocking=True)
            compared = (
                (block_tensor[:, None, :] != 0) | (base[None, :, :] != 0)
            ).sum(dim=2)
            matched = (
                (block_tensor[:, None, :] == base[None, :, :])
                & (block_tensor[:, None, :] != 0)
            ).sum(dim=2)
            sim = torch.where(
                compared > 0,
                matched.float() / compared.clamp(min=1).float(),
                torch.zeros_like(matched, dtype=torch.float32),
            )
            matrix[start : start + len(block)] = sim.cpu().numpy()
        else:
            compared = ((block[:, None, :] != 0) | (padded[None, :, :] != 0)).sum(axis=2)
            matched = (
                (block[:, None, :] == padded[None, :, :]) & (block[:, None, :] != 0)
            ).sum(axis=2)
            matrix[start : start + len(block)] = np.where(
                compared > 0,
                matched / np.maximum(compared, 1),
                0.0,
            )

    return matrix
