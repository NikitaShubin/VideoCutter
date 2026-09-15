"""Тесты ядра: операции с фрагментами, история, сохранение/чтение в файл.

Покрывает «механику» на Python-ядре FragmentEditor + FileFragmentStore:
add/new_start/new_end/delete (все ветки), undo/redo, и round-trip всех
сегментов через текстовый .txt (формат legacy: по строке "start end").

Прогон:  python3 scripts/test_fragment_core.py
Возвращает код 0 при успехе, 1 при провале.
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from videocutter.adapters.file_store import FileFragmentStore  # noqa: E402
from videocutter.core.fragment_editor import FragmentEditor  # noqa: E402


_PASSED = 0


def check(name, fn):
    global _PASSED
    try:
        fn()
        _PASSED += 1
    except Exception:
        print(f"[FAIL] {name}")
        raise


def new_editor(tmpdir):
    store = FileFragmentStore(os.path.join(tmpdir, "fragments.txt"))
    return FragmentEditor(100, store)


def state(editor):
    return [list(f) for f in editor.fragments]


# --- операции ---

def test_add_inserts_sorted_and_rejects_overlap(tmpdir):
    e = new_editor(tmpdir)
    assert e.add((40, 60)) is True
    assert e.add((10, 20)) is True
    assert e.add((70, 90)) is True
    assert state(e) == [[10, 20], [40, 60], [70, 90]]

    assert e.add((15, 25)) is False
    assert e.add((5, 15)) is False
    assert e.add((21, 30)) is True


def test_new_start_end(tmpdir):
    e = new_editor(tmpdir)
    e.new_end(20)
    e.new_start(50)
    e.new_end(30)
    e.new_start(10)
    assert state(e) == [[10, 30], [50, 99]]


def test_new_end_inserts_front_not_back(tmpdir):
    # Было: new_end создавал [0, position] в конце списка → терялась сортировка,
    # и следующий new_start правил не тот фрагмент (перекрытие).
    e = new_editor(tmpdir)
    e.new_start(60)  # [60, 99]
    e.new_end(55)    # [0, 55] должен встать в начало
    e.new_start(45)  # первый конец >= 45 — [0, 55] → [45, 55]
    e.new_end(90)    # правый сегмент → [60, 90]
    assert state(e) == [[45, 55], [60, 90]]


def test_new_end_left_of_all_inserts_front(tmpdir):
    e = new_editor(tmpdir)
    e.new_start(60)  # [60, 99]
    e.new_end(55)    # [0, 55] в начало
    e.new_start(45)  # [45, 55]
    e.new_end(30)    # нет start <= 30 → [0, 30] в начало, не в конец
    assert state(e) == [[0, 30], [45, 55], [60, 99]]


def test_delete_inside(tmpdir):
    e = new_editor(tmpdir)
    e.add((10, 20))
    e.add((30, 40))
    assert e.delete(15) == "ok"
    assert state(e) == [[30, 40]]


def test_delete_edge(tmpdir):
    e = new_editor(tmpdir)
    e.add((10, 20))
    e.add((30, 40))
    assert e.delete(20) == "ok"
    assert state(e) == [[30, 40]]


def test_delete_gap_merges(tmpdir):
    e = new_editor(tmpdir)
    e.add((10, 20))
    e.add((30, 40))
    assert e.delete(25) == "ok"
    assert state(e) == [[10, 40]]


def test_delete_outside_extends(tmpdir):
    e = new_editor(tmpdir)
    e.add((10, 20))
    e.delete(5)
    assert state(e) == [[0, 20]]
    e.delete(50)
    assert state(e) == [[0, 99]]


def test_delete_empty_is_nothing(tmpdir):
    e = new_editor(tmpdir)
    assert e.delete(5) == "nothing"


# --- история ---

def test_undo_redo(tmpdir):
    e = new_editor(tmpdir)
    e.add((10, 20))
    e.add((30, 40))
    e.delete(15)
    assert state(e) == [[30, 40]]
    assert e.undo() is True
    assert state(e) == [[10, 20], [30, 40]]
    assert e.redo() is True
    assert state(e) == [[30, 40]]
    assert e.redo() is False
    e.undo()
    e.undo()
    assert state(e) == [[10, 20]]
    assert e.undo() is True
    assert state(e) == []
    assert e.undo() is False


def test_history_branch_cut_on_new_op(tmpdir):
    e = new_editor(tmpdir)
    e.add((10, 20))
    e.add((30, 40))
    e.undo()
    e.add((50, 60))
    assert e.redo() is False
    assert state(e) == [[10, 20], [50, 60]]


# --- сохранение/чтение в файл ---

def test_save_load_roundtrip_all_segments(tmpdir):
    e = new_editor(tmpdir)
    e.add((0, 5))
    e.add((12, 30))
    e.add((50, 99))
    e.delete(8)  # слияние (0,5)+(12,30) -> (0,30)
    e.new_start(3)
    e.new_end(55)
    before = state(e)

    persisted = os.path.join(tmpdir, "fragments.txt")
    assert os.path.isfile(persisted)

    e2 = FragmentEditor(100, FileFragmentStore(persisted))
    assert state(e2) == before


def test_file_format_is_legacy_style(tmpdir):
    e = new_editor(tmpdir)
    e.add((0, 5))
    e.add((12, 30))
    with open(os.path.join(tmpdir, "fragments.txt")) as f:
        lines = f.read().splitlines()
    assert lines == ["0 5", "12 30"]


def test_missing_file_loads_empty(tmpdir):
    e = new_editor(tmpdir)
    assert state(e) == []


def test_corrupt_lines_are_skipped(tmpdir):
    os.makedirs(tmpdir, exist_ok=True)
    path = os.path.join(tmpdir, "fragments.txt")
    with open(path, "w") as f:
        f.write("10 20\n\nnot a pair\n0 5\n")
    e = FragmentEditor(100, FileFragmentStore(path))
    assert state(e) == [[10, 20], [0, 5]]


def test_store_ok_honors_file(tmpdir):
    e = new_editor(tmpdir)
    e.add((10, 20))
    assert len(e.history) == 2
    assert [list(f) for f in e.history[0]] == []
    assert [list(f) for f in e.history[1]] == [[10, 20]]


def main():
    global _PASSED
    with tempfile.TemporaryDirectory(prefix="vc_core_test_") as tmpdir:
        tests = [
            test_add_inserts_sorted_and_rejects_overlap,
            test_new_start_end,
            test_new_end_inserts_front_not_back,
            test_new_end_left_of_all_inserts_front,
            test_delete_inside,
            test_delete_edge,
            test_delete_gap_merges,
            test_delete_outside_extends,
            test_delete_empty_is_nothing,
            test_undo_redo,
            test_history_branch_cut_on_new_op,
            test_save_load_roundtrip_all_segments,
            test_file_format_is_legacy_style,
            test_missing_file_loads_empty,
            test_corrupt_lines_are_skipped,
            test_store_ok_honors_file,
        ]
        for t in tests:
            check(t.__name__, lambda t=t: t(os.path.join(tmpdir, t.__name__)))
    print(f"ok: {_PASSED} проверок")
    return 0


if __name__ == "__main__":
    sys.exit(main())