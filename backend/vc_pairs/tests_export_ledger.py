# -*- coding: utf-8 -*-
"""Characterization tests for ExportLedger.

The ledger is the single module for export state: memory registry, queue,
cancel set, sidecar, freshness hash and file presence. These tests fix the
observable contract before the shallow globals are replaced.

Run: cd backend && DJANGO_DEBUG=0 python manage.py test vc_pairs.tests_export_ledger
"""

import json
import os
import stat
from unittest import mock

from export_ledger import LEDGER
from vc_fragments.tests import WorkspaceApiTestBase
from workspace import get_workspace


class ExportLedgerTestBase(WorkspaceApiTestBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(LEDGER._reset_for_tests)
        self.ws = get_workspace(self.ws_id)
        self.out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(self.out_dir, exist_ok=True)

    def _out(self, name="out.mp4", data=b"x"):
        path = os.path.join(self.out_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _current_hash(self):
        frags = self.ws.load_fragments()
        st = os.stat(self.video)
        return LEDGER.task_hash(frags, st.st_size, st.st_mtime_ns)

    def _write_sidecar(self, payload):
        with open(os.path.join(self.out_dir, ".export-state.json"),
                  "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _read_sidecar(self):
        with open(os.path.join(self.out_dir, ".export-state.json"),
                  encoding="utf-8") as f:
            return json.load(f)


class ExportLedgerStatusTests(ExportLedgerTestBase):
    """The list badge: status(ws) for every state."""

    def test_status_none_without_state(self):
        self.assertIsNone(LEDGER.status(self.ws))

    def test_status_queued(self):
        LEDGER.enqueue(self.ws_id, 3)
        self.assertEqual(
            LEDGER.status(self.ws),
            {"state": "queued", "index": 0, "total": 3, "error": ""},
        )

    def test_status_running(self):
        LEDGER.progress(self.ws_id, 2, 5)
        self.assertEqual(
            LEDGER.status(self.ws),
            {"state": "running", "index": 2, "total": 5, "error": ""},
        )

    def test_status_error(self):
        LEDGER.fail(self.ws_id, "boom")
        self.assertEqual(
            LEDGER.status(self.ws),
            {"state": "error", "index": 0, "total": 1, "error": "boom"},
        )

    def test_status_cancelled(self):
        LEDGER.cancel(self.ws_id, [])
        self.assertEqual(
            LEDGER.status(self.ws),
            {"state": "cancelled", "index": 0, "total": 1, "error": ""},
        )

    def test_status_done_with_live_files(self):
        path = self._out()
        LEDGER.finish(self.ws_id, [path], self._current_hash())
        self.assertEqual(LEDGER.status(self.ws)["state"], "done")

    def test_status_done_drops_when_file_missing(self):
        path = self._out()
        LEDGER.finish(self.ws_id, [path], self._current_hash())
        os.remove(path)
        self.assertIsNone(LEDGER.status(self.ws))
        self.assertNotIn(self.ws_id, LEDGER.states)

    def test_status_done_drops_when_hash_mismatch(self):
        path = self._out()
        LEDGER.finish(self.ws_id, [path], "wrong-hash")
        self.assertIsNone(LEDGER.status(self.ws))
        self.assertNotIn(self.ws_id, LEDGER.states)

    def test_status_done_drops_when_no_fragments(self):
        path = self._out()
        LEDGER.finish(self.ws_id, [path], self._current_hash())
        self.ws.save_fragments([])
        self.assertIsNone(LEDGER.status(self.ws))
        self.assertNotIn(self.ws_id, LEDGER.states)

    def test_status_done_drops_when_no_video(self):
        path = self._out()
        LEDGER.finish(self.ws_id, [path], self._current_hash())
        os.remove(self.video)
        self.assertIsNone(LEDGER.status(self.ws))
        self.assertNotIn(self.ws_id, LEDGER.states)


class ExportLedgerCurrentDoneTests(ExportLedgerTestBase):
    """current_done(ws): ready result under the CURRENT fragments/video."""

    def test_current_done_no_fragments(self):
        with open(os.path.join(self.ws_dir, "fragments.tsv"), "w") as f:
            f.write("start\tend\tcomment\n")
        self.assertIsNone(LEDGER.current_done(self.ws))

    def test_current_done_no_video(self):
        os.remove(self.video)
        self.assertIsNone(LEDGER.current_done(self.ws))

    def test_current_done_no_identity(self):
        self.assertIsNone(
            LEDGER.current_done(self.ws, video_identity=lambda _path: None)
        )

    def test_current_done_from_memory(self):
        path = self._out()
        LEDGER.finish(self.ws_id, [path], self._current_hash())
        done = LEDGER.current_done(self.ws)
        self.assertEqual(done["state"], "done")
        self.assertEqual(
            done["files"],
            [{
                "index": 1,
                "filename": "out.mp4",
                "url": f"/api/v1/pairs/{self.ws_id}/export/out.mp4",
            }],
        )

    def test_current_done_memory_missing_file_falls_back_to_sidecar(self):
        stale = self._out("stale.mp4")
        fresh = self._out("fresh.mp4")
        want = self._current_hash()
        LEDGER.finish(self.ws_id, [stale], want)
        os.remove(stale)
        self._write_sidecar({"hash": want, "files": ["fresh.mp4"]})
        done = LEDGER.current_done(self.ws)
        self.assertEqual(done["files"][0]["filename"], "fresh.mp4")
        self.assertNotIn(self.ws_id, LEDGER.states)

    def test_current_done_from_sidecar(self):
        self._out("sidecar.mp4")
        self._write_sidecar({"hash": self._current_hash(),
                             "files": ["sidecar.mp4"]})
        done = LEDGER.current_done(self.ws)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["files"][0]["filename"], "sidecar.mp4")

    def test_current_done_sidecar_hash_mismatch(self):
        self._out("sidecar.mp4")
        self._write_sidecar({"hash": "other", "files": ["sidecar.mp4"]})
        self.assertIsNone(LEDGER.current_done(self.ws))

    def test_current_done_sidecar_missing_file(self):
        self._write_sidecar({"hash": self._current_hash(),
                             "files": ["missing.mp4"]})
        self.assertIsNone(LEDGER.current_done(self.ws))

    def test_current_done_sidecar_empty_files(self):
        self._write_sidecar({"hash": self._current_hash(), "files": []})
        self.assertIsNone(LEDGER.current_done(self.ws))

    def test_current_done_sidecar_corrupt(self):
        with open(os.path.join(self.out_dir, ".export-state.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(LEDGER.current_done(self.ws))

    def test_current_done_sidecar_oserror(self):
        self._out("sidecar.mp4")
        self._write_sidecar({"hash": self._current_hash(),
                             "files": ["sidecar.mp4"]})
        with mock.patch("os.path.getsize", side_effect=OSError("gone")):
            self.assertIsNone(LEDGER.current_done(self.ws))


class ExportLedgerQueueTests(ExportLedgerTestBase):
    """Queue, slots, cancel, rename and forget."""

    def test_enqueue_positions(self):
        LEDGER.enqueue("a", 2)
        LEDGER.enqueue("b", 3)
        self.assertEqual(LEDGER.snapshot("a")["position"], 1)
        self.assertEqual(LEDGER.snapshot("b")["position"], 2)
        self.assertEqual(LEDGER.queue, ["a", "b"])

    def test_promote_starts_fifo_up_to_capacity(self):
        LEDGER.enqueue("a", 2)
        LEDGER.enqueue("b", 3)
        LEDGER.enqueue("c", 4)
        self.assertEqual(LEDGER.promote(2), ["a", "b"])
        self.assertEqual(LEDGER.snapshot("a")["state"], "running")
        self.assertEqual(LEDGER.snapshot("c")["position"], 1)

    def test_promote_does_not_exceed_capacity(self):
        LEDGER.enqueue("a", 2)
        LEDGER.promote(1)
        LEDGER.enqueue("b", 3)
        self.assertEqual(LEDGER.promote(1), [])
        self.assertEqual(LEDGER.snapshot("b")["state"], "queued")

    def test_promote_after_finish_starts_next(self):
        LEDGER.enqueue("a", 2)
        LEDGER.enqueue("b", 3)
        LEDGER.promote(1)
        LEDGER.fail("a", "boom")
        self.assertEqual(LEDGER.promote(1), ["b"])

    def test_is_running(self):
        LEDGER.progress("a", 0, 2)
        self.assertTrue(LEDGER.is_running("a"))
        LEDGER.fail("a", "boom")
        self.assertFalse(LEDGER.is_running("a"))

    def test_running_count(self):
        LEDGER.progress("a", 0, 2)
        LEDGER.progress("b", 0, 3)
        self.assertEqual(LEDGER.running_count(), 2)

    def test_request_cancel_marks_running(self):
        LEDGER.progress("a", 0, 2)
        LEDGER.request_cancel("a")
        self.assertTrue(LEDGER.cancelled("a"))

    def test_cancel_removes_partial_files_and_clears_flag(self):
        path = self._out("partial.mp4")
        LEDGER.progress("a", 0, 2)
        LEDGER.request_cancel("a")
        LEDGER.cancel("a", [path])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(LEDGER.snapshot("a")["state"], "cancelled")
        self.assertFalse(LEDGER.cancelled("a"))

    def test_cancel_queued_removes_from_queue(self):
        LEDGER.enqueue("a", 2)
        LEDGER.cancel("a", [])
        self.assertEqual(LEDGER.queue, [])
        self.assertEqual(LEDGER.snapshot("a")["state"], "cancelled")

    def test_rename_moves_state_and_queue(self):
        LEDGER.enqueue("old", 2)
        LEDGER.rename("old", "new")
        self.assertIsNone(LEDGER.snapshot("old"))
        self.assertEqual(LEDGER.snapshot("new")["state"], "queued")
        self.assertEqual(LEDGER.queue, ["new"])

    def test_forget_removes_state_and_queue(self):
        LEDGER.enqueue("a", 2)
        LEDGER.forget("a")
        self.assertIsNone(LEDGER.snapshot("a"))
        self.assertEqual(LEDGER.queue, [])

    def test_forget_clears_cancel_flag(self):
        LEDGER.progress("a", 0, 2)
        LEDGER.request_cancel("a")
        LEDGER.forget("a")
        self.assertFalse(LEDGER.cancelled("a"))

    def test_forget_keeps_cancel_flag_for_running_delete(self):
        LEDGER.progress("a", 0, 2)
        LEDGER.request_cancel("a")
        LEDGER.forget("a", keep_cancel=True)
        self.assertTrue(LEDGER.cancelled("a"))


class ExportLedgerPersistenceTests(ExportLedgerTestBase):
    """finish writes the same sidecar shape; empty result writes nothing."""

    def test_finish_writes_sidecar(self):
        path = self._out()
        LEDGER.finish(self.ws_id, [path], "hash123")
        self.assertEqual(
            self._read_sidecar(),
            {"hash": "hash123", "files": ["out.mp4"]},
        )

    def test_finish_without_files_writes_no_sidecar(self):
        LEDGER.finish(self.ws_id, [], "hash123")
        self.assertFalse(os.path.exists(
            os.path.join(self.out_dir, ".export-state.json")))

    def test_finish_sidecar_is_world_readable(self):
        path = self._out()
        LEDGER.finish(self.ws_id, [path], "hash123")
        sidecar = os.path.join(self.out_dir, ".export-state.json")
        self.assertEqual(stat.S_IMODE(os.stat(sidecar).st_mode), 0o644)

    def test_fail_sets_error(self):
        LEDGER.fail("a", "boom")
        self.assertEqual(LEDGER.snapshot("a"),
                         {"state": "error", "error": "boom"})


class ExportLedgerOutputsTests(ExportLedgerTestBase):
    """File-presence rule: all files exist and are non-zero."""

    def test_outputs_present(self):
        self._out()
        self.assertTrue(LEDGER.outputs_present(
            self.out_dir, [{"filename": "out.mp4"}]))

    def test_outputs_present_accepts_plain_names(self):
        self._out()
        self.assertTrue(LEDGER.outputs_present(self.out_dir, ["out.mp4"]))

    def test_outputs_present_missing(self):
        self.assertFalse(LEDGER.outputs_present(
            self.out_dir, [{"filename": "missing.mp4"}]))

    def test_outputs_present_zero_size(self):
        self._out(data=b"")
        self.assertFalse(LEDGER.outputs_present(
            self.out_dir, [{"filename": "out.mp4"}]))

    def test_outputs_present_empty_list_is_vacuously_true(self):
        self.assertTrue(LEDGER.outputs_present(self.out_dir, []))

    def test_outputs_present_oserror(self):
        self._out()
        with mock.patch("os.path.getsize", side_effect=OSError("gone")):
            self.assertFalse(LEDGER.outputs_present(
                self.out_dir, [{"filename": "out.mp4"}]))

    def test_purge_outputs_removes_ours_and_sidecar(self):
        self._out("video_fragment_1.mp4")
        self._out(".video_fragment_9.mp4")
        self._out("keep.txt")
        self._write_sidecar({"hash": "h", "files": ["video_fragment_1.mp4"]})

        LEDGER.purge_outputs(self.ws)

        self.assertEqual(sorted(os.listdir(self.out_dir)), ["keep.txt"])

    def test_purge_outputs_without_directory(self):
        os.rmdir(self.out_dir)

        LEDGER.purge_outputs(self.ws)

    def test_purge_outputs_remove_oserror(self):
        self._out("video_fragment_1.mp4")
        remove = mock.Mock(side_effect=OSError("ro"))

        LEDGER.purge_outputs(self.ws, remove=remove)

        self.assertTrue(os.path.isfile(
            os.path.join(self.out_dir, "video_fragment_1.mp4")))


class ExportLedgerIdentityTests(ExportLedgerTestBase):
    """Hash and file identity are the freshness stamp."""

    def test_export_state_missing_workspace(self):
        import workspace as ws_module
        self.assertIsNone(ws_module._export_state("ghost-ws-zzz"))

    def test_task_hash_ignores_fragment_order(self):
        st = os.stat(self.video)
        a = LEDGER.task_hash(
            [{"start": 5, "end": 9}, {"start": 0, "end": 3}],
            st.st_size, st.st_mtime_ns)
        b = LEDGER.task_hash(
            [{"start": 0, "end": 3}, {"start": 5, "end": 9}],
            st.st_size, st.st_mtime_ns)
        self.assertEqual(a, b)

    def test_video_identity_missing(self):
        self.assertIsNone(
            LEDGER.video_identity(os.path.join(self.ws_dir, "nope.mp4")))

    def test_video_identity_shape(self):
        st = os.stat(self.video)
        self.assertEqual(
            LEDGER.video_identity(self.video),
            {"size": st.st_size, "mtime_ns": st.st_mtime_ns},
        )

    def test_reset_clears_all_stores(self):
        self._out()
        LEDGER.enqueue("a", 2)
        LEDGER.request_cancel("a")
        LEDGER._reset_for_tests()
        self.assertEqual(LEDGER.states, {})
        self.assertEqual(LEDGER.queue, [])
        self.assertEqual(LEDGER.cancel_set, set())
