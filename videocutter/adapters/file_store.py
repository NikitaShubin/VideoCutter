"""Хранение фрагментов в текстовом файле (.txt).

Реализация порта ``IFragmentStore``: каждая строка — "start end".
"""

from __future__ import annotations

import os
from typing import Sequence

from ..core.model import Fragment, FragmentList
from ..core.ports import IFragmentStore


class FileFragmentStore(IFragmentStore):
    def __init__(self, path: str) -> None:
        self.path = path

    def load_fragments(self) -> FragmentList:
        if not os.path.isfile(self.path):
            return []
        fragments: FragmentList = []
        with open(self.path, "r") as f:
            for line in f.readlines():
                parts = line.split()
                if len(parts) == 2:
                    fragments.append((int(parts[0]), int(parts[1])))
        return fragments

    def save_fragments(self, fragments: Sequence[Fragment]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            for start, end in fragments:
                f.write(f"{start} {end}\n")
