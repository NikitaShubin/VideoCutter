"""Редактор списка фрагментов: алгоритм отбора и история изменений.

Перенос логики десктопного ``Backend`` в чистое ядро: операции add/delete/
new_start/new_end, undo/redo, перечень ключевых кадров, суммарное покрытие.
Не зависит от способа хранения — использует порт ``IFragmentStore``.
"""

from __future__ import annotations

import copy
from typing import List, Optional, Sequence, Tuple

from .model import Fragment, FragmentList
from .ports import IFragmentStore


class FragmentEditor:
    def __init__(self, total_frames: int, store: IFragmentStore) -> None:
        self.total_frames = total_frames
        self.store = store
        self.fragments: FragmentList = []
        self.history: List[FragmentList] = []
        self.hist_pos = 0
        self._load_initial()

    # --- загрузка/сохранение (через порт) ---

    def _load_initial(self) -> None:
        loaded = self.store.load_fragments()
        self.fragments = copy.deepcopy(loaded)
        self.history = [copy.deepcopy(self.fragments)]
        self.hist_pos = 0

    def save(self) -> None:
        self.store.save_fragments(self.fragments)

    # --- история операций ---

    def _update_hist(self, sync: bool = True) -> None:
        self.history = self.history[: self.hist_pos + 1] + [
            copy.deepcopy(self.fragments)
        ]
        self.hist_pos = len(self.history) - 1
        if sync:
            self.save()

    def undo(self, sync: bool = True) -> bool:
        if self.hist_pos:
            self.hist_pos -= 1
            self.fragments = copy.deepcopy(self.history[self.hist_pos])
            if sync:
                self.save()
            return True
        return False

    def redo(self, sync: bool = True) -> bool:
        next_pos = self.hist_pos + 1
        if next_pos == len(self.history):
            return False
        self.hist_pos = next_pos
        self.fragments = copy.deepcopy(self.history[self.hist_pos])
        if sync:
            self.save()
        return True

    # --- проверки ---

    def check(self, position: int) -> bool:
        return 0 <= position < self.total_frames

    # --- операции с фрагментами ---

    def add(self, fragment: Fragment) -> bool:
        """Вносит новый фрагмент, если нет наложения с уже имеющимися."""
        new_start, new_end = fragment
        new_ind = 0
        for ind, (start, end) in enumerate(self.fragments):
            if new_start > end:
                new_ind = ind + 1
            elif new_end < start:
                new_ind = ind
                break
            else:
                return False

        self.fragments = (
            self.fragments[:new_ind] + [tuple(fragment)] + self.fragments[new_ind:]
        )
        self._update_hist()
        return True

    def contains(self, position: int) -> bool:
        """Входит ли позиция в один из существующих фрагментов."""
        for start, end in self.fragments:
            if start <= position <= end:
                return True
        return False

    def new_start(self, position: int) -> bool:
        """Изменяет начало следующего фрагмента (или создаёт, доходящий до конца)."""
        if not self.check(position):
            return False
        for ind, (start, end) in enumerate(self.fragments):
            if end >= position:
                self.fragments[ind] = (position, end)
                break
        else:
            self.fragments.append((position, self.total_frames - 1))
        self._update_hist()
        return True

    def new_end(self, position: int) -> bool:
        """Изменяет конец предыдущего фрагмента (или создаёт, начиная с 0)."""
        if not self.check(position):
            return False
        for ind, (start, end) in reversed(list(enumerate(self.fragments))):
            if start <= position:
                self.fragments[ind] = (start, position)
                break
        else:
            # Все начала правее position: новый фрагмент встаёт в начало,
            # иначе список теряет сортировку и правки границ бьют в неверный.
            self.fragments.insert(0, (0, position))
        self._update_hist()
        return True

    # Коды результата удаления, чтобы UI сам решал, как сообщить пользователю.
    DELETE_OK = "ok"
    DELETE_NOTHING = "nothing"
    DELETE_AMBIGUOUS = "ambiguous"

    def delete(self, position: int) -> str:
        """Удаляет/корректирует фрагмент на текущей позиции.

        Возвращает код результата. ``ambiguous`` означает, что позиция находится
        на стыке двух фрагментов и требуется уточнение (UI должен показать это
        пользователю).
        """
        if len(self.fragments) == 0:
            return self.DELETE_NOTHING

        point = (position, position)
        if point in self.fragments:
            self.fragments.remove(point)
            self._update_hist()
            return self.DELETE_OK

        # Ищем фрагменты, один из краёв которых приходится на текущий кадр.
        fragments2del = [f for f in self.fragments if position in f]

        if len(fragments2del) == 1:
            self.fragments.remove(fragments2del[0])
            self._update_hist()
            return self.DELETE_OK

        if len(fragments2del) == 2:
            return self.DELETE_AMBIGUOUS

        if len(fragments2del) == 0:
            if position < self.fragments[0][0]:
                self.fragments[0] = (0, self.fragments[0][1])
                self._update_hist()
                return self.DELETE_OK
            if position > self.fragments[-1][1]:
                self.fragments[-1] = (self.fragments[-1][0], self.total_frames - 1)
                self._update_hist()
                return self.DELETE_OK

            # Позиция в промежутке между фрагментами — удаляем границы этого промежутка.
            flat: List[int] = []
            for s, e in self.fragments:
                flat.extend([s, e])
            for ind in range(len(flat) - 1):
                if flat[ind] < position < flat[ind + 1]:
                    del flat[ind : ind + 2]
                    self.fragments = [
                        (int(flat[i]), int(flat[i + 1]))
                        for i in range(0, len(flat), 2)
                    ]
                    self._update_hist()
                    return self.DELETE_OK
            raise ValueError("Ошибка логики программы!")

        raise ValueError("Ошибка в списках фрагментов!")

    # --- вспомогательные расчёты ---

    def selected_frames(self) -> int:
        return sum(end - start for start, end in self.fragments)

    def get_all_key_frames(self) -> List[int]:
        """Возвращает все ключевые кадры (начала и концы фрагментов), отсортированные."""
        key_frames = set()
        for start, end in self.fragments:
            key_frames.add(start)
            key_frames.add(end)
        return sorted(key_frames)

    # --- таймлайн (статус-бар) ---

    def start_frame2sb_axis(self, width: int, frame: int) -> int:
        return int(round((width - 1) * frame / self.total_frames)) if self.total_frames else 0

    def end_frame2sb_axis(self, width: int, frame: int) -> int:
        return self.start_frame2sb_axis(width, frame) + 1
