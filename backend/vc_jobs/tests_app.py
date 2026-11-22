# -*- coding: utf-8 -*-
"""Автозапуск планировщика: тесты, runserver-релоадер, gunicorn.

Прогон: cd backend && python manage.py test vc_jobs.tests_app
"""

import os
from unittest import mock

from django.test import SimpleTestCase

import vc_jobs
from vc_jobs.apps import VcJobsConfig, should_autostart


class ShouldAutostartTests(SimpleTestCase):
    def test_gunicorn_starts_by_default(self):
        self.assertTrue(should_autostart(argv=["gunicorn"], env={}))

    def test_disabled_by_env(self):
        self.assertFalse(should_autostart(
            argv=["gunicorn"], env={"VC_JOBS_AUTOSTART": "0"}))

    def test_test_runner_does_not_start(self):
        self.assertFalse(should_autostart(argv=["manage.py", "test"], env={}))
        self.assertFalse(should_autostart(argv=["pytest"], env={}))

    def test_runserver_starts_only_in_reload_child(self):
        self.assertFalse(should_autostart(
            argv=["manage.py", "runserver"], env={}))
        self.assertTrue(should_autostart(
            argv=["manage.py", "runserver"], env={"RUN_MAIN": "true"}))

    def test_runserver_noreload_starts(self):
        self.assertTrue(should_autostart(
            argv=["manage.py", "runserver", "--noreload"], env={}))


class BootRepairTests(SimpleTestCase):
    def test_repair_uses_workspace_root_and_anchor(self):
        import workspace

        with mock.patch.object(workspace, "WORKSPACE_ROOT", "/data/ws"), \
                mock.patch.dict("os.environ",
                                {"VC_ACCESS_ANCHOR": "/media/user"}), \
                mock.patch("export_access.repair_exports") as repair:
            from vc_jobs.apps import repair_access_on_boot

            repair_access_on_boot()

        repair.assert_called_once_with("/data/ws", anchor="/media/user")

    def test_repair_failure_is_logged_not_raised(self):
        with mock.patch("export_access.repair_exports",
                        side_effect=OSError("диск")):
            from vc_jobs.apps import repair_access_on_boot

            with self.assertLogs("vc_jobs.apps", level="ERROR"):
                repair_access_on_boot()


class ReadyTests(SimpleTestCase):
    def setUp(self):
        self.repair = mock.patch("vc_jobs.apps.repair_access_on_boot")
        self.repair.start()
        self.addCleanup(self.repair.stop)

    def test_ready_starts_scheduler(self):
        scheduler = mock.Mock()
        with mock.patch("vc_jobs.get_scheduler", return_value=scheduler), \
                mock.patch("vc_jobs.apps.should_autostart",
                           return_value=True):
            VcJobsConfig("vc_jobs", __import__("vc_jobs")).ready()

        scheduler.start.assert_called_once_with()

    def test_ready_skips_when_disabled(self):
        with mock.patch("vc_jobs.get_scheduler") as get_scheduler, \
                mock.patch("vc_jobs.apps.should_autostart",
                           return_value=False):
            VcJobsConfig("vc_jobs", __import__("vc_jobs")).ready()

        get_scheduler.assert_not_called()

    def test_start_failure_does_not_break_boot(self):
        scheduler = mock.Mock()
        scheduler.start.side_effect = RuntimeError("диск")
        with mock.patch("vc_jobs.get_scheduler", return_value=scheduler), \
                mock.patch("vc_jobs.apps.should_autostart",
                           return_value=True):
            with self.assertLogs("vc_jobs.apps", level="ERROR"):
                VcJobsConfig("vc_jobs", __import__("vc_jobs")).ready()


class SchedulerSingletonTests(SimpleTestCase):
    def setUp(self):
        self.old = vc_jobs.peek_scheduler()
        vc_jobs.set_scheduler(None)
        self.addCleanup(vc_jobs.set_scheduler, self.old)

    def test_get_scheduler_builds_lazily_from_state_root(self):
        sentinel = object()
        with mock.patch("vc_jobs.scheduler.build_scheduler",
                        return_value=sentinel) as build, \
                mock.patch.dict(os.environ,
                                {"VC_STATE_ROOT": "/tmp/vc-state"}):
            scheduler = vc_jobs.get_scheduler()

        self.assertIs(scheduler, sentinel)
        build.assert_called_once_with("/tmp/vc-state")
        self.assertIs(vc_jobs.peek_scheduler(), sentinel)
