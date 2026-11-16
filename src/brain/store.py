"""Compact storage for brain rows: the table in parquet, the vectors in a float32 .npy beside it.

<name>.parquet holds every column except 'vector'; <name>.vectors.npy holds the vectors in the
same row order. Millions of rows load in seconds and take ~100 bytes per row instead of ~1 KB
of Python lists. Old files that still have a 'vector' list column are read too."""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd

from .. import parquet_compat


def vectors_path(path: str | Path) -> Path:
    path = Path(path)
    return path.with_name(path.stem + '.vectors.npy')


def write_rows(frame: pd.DataFrame, vectors: np.ndarray, path: str | Path) -> None:
    path = Path(path); vectors = np.asarray(vectors, dtype=np.float32)
    if len(frame) != len(vectors): raise ValueError(f'{len(frame)} rows but {len(vectors)} vectors')
    table = frame.drop(columns=['vector'], errors='ignore').reset_index(drop=True)
    parquet_compat.write_parquet(table, path, index=False)
    np.save(vectors_path(path), vectors if vectors.ndim == 2 else vectors.reshape(len(table), -1))


def read_rows(path: str | Path, columns: list[str] | None = None) -> tuple[pd.DataFrame, np.ndarray]:
    """(table without 'vector', float32 matrix). Works for new (.npy) and old (list column) files."""
    path = Path(path); npy = vectors_path(path)
    if npy.is_file():
        table = parquet_compat.read_parquet(path, columns=columns).reset_index(drop=True)
        return table, np.load(npy)
    table = parquet_compat.read_parquet(path).reset_index(drop=True)
    if 'vector' not in table: raise ValueError(f'{path.name} has no vectors')
    lists = table.pop('vector').tolist()
    if columns: table = table[[column for column in columns if column in table]]
    if not lists: return table, np.empty((0, 0), dtype=np.float32)
    return table, np.asarray(lists, dtype=np.float32)
