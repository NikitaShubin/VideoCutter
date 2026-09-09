"""Базовые типы и модель данных ядра VideoCutter.

Здесь живут примитивы, которые не зависят ни от способа получения кадров
(локальный файл / облачные чанки), ни от способа хранения фрагментов
(файл .txt / БД / реестр).
"""

from __future__ import annotations

from typing import List, Tuple, Sequence


class CircleInd:
    """Целое число с замкнутым инкрементом/декрементом.

    Полезно для круговой адресации к элементам массива (буфер кадров).
    """

    def __init__(self, circle: int, ind: int = 0) -> None:
        assert 0 <= ind < circle
        self.circle = circle
        self.ind = ind

    def inc(self) -> int:
        self.ind += 1
        if self.ind == self.circle:
            self.ind = 0
        return self.ind

    def dec(self) -> int:
        self.ind -= 1
        if self.ind == -1:
            self.ind = self.circle - 1
        return self.ind

    def __int__(self) -> int:
        return self.ind

    __call__ = __int__

    def __eq__(self, other: object) -> bool:
        return self.ind == int(other)  # type: ignore[arg-type]

    def __ne__(self, other: object) -> bool:
        return self.ind != int(other)  # type: ignore[arg-type]


# Фрагмент — это пара индексов кадров [start, end] (оба включительно).
Fragment = Tuple[int, int]
FragmentList = List[Fragment]


def canonical_fragments(fragments: Sequence[Fragment]) -> List[Fragment]:
    """Возвращает копию списка фрагментов в каноничном виде (sorted, без дублей).

    Обработчик не должен полагаться на то, что ему передадут корректный список;
    эта функция приводит данные к единому виду перед работой.
    """
    unique: List[Fragment] = []
    seen = set()
    for start, end in fragments:
        item = (int(start), int(end))
        if item not in seen:
            seen.add(item)
            unique.append(item)
    unique.sort(key=lambda item: (item[0], item[1]))
    return unique
