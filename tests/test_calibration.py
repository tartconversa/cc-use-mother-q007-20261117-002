from cycleflow.demo import END, START
from fastapi.testclient import TestClient

from cycleflow.api import create_app
from tests.common import ServiceCase


class CalibrationLifecycleTests(ServiceCase):
    def _complete_job(self, job_id="J1", number=20):
        self.complete_stages(job_id, base=number)

    def _invalidate(self, calibration_id="C-D1", number=40, minute=40, reason="out-of-tolerance"):
        return self.ingest(number, "calibration.invalidate", minute,
                           {"calibration_id": calibration_id, "reason": reason})

    def _extra_lots(self, motors=12, sensors=6, number=50):
        self.ingest(number, "lot.receive", 1, {"lot_id": "M2", "part": "motor",
                                               "supplier": "motors", "quantity": motors})
        self.ingest(number + 1, "lot.receive", 2, {"lot_id": "S2", "part": "sensor",
                                                   "supplier": "sensors", "quantity": sensors})

    def test_invalidated_calibration_parks_job_in_recheck_not_failed(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        tested = self.ingest(30, "test.record", 30, self.build_test_payload("T1", 2, 2))
        self.assertEqual(tested["state"], "accepted")
        self.assertTrue(self.service.get_test("T1")["verified"])
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        self._invalidate()
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        report = self.service.report("robot-arm", START, END)
        summary = report["summary"]
        # An unverified result is neither failure nor yield input.
        self.assertEqual(summary["tested_units"], 0)
        self.assertEqual(summary["passed_units"], 0)
        self.assertEqual(summary["failed_units"], 0)
        self.assertEqual(summary["unverified_units"], 4)
        self.assertEqual(summary["recheck_jobs"], 1)
        self.assertIsNone(summary["pass_rate"])
        job = report["jobs"][0]
        self.assertEqual(job["quality_state"], "recheck")
        self.assertFalse(job["tests"][0]["verified"])
        self.assertIn("calibration_invalidated", job["tests"][0]["verify_reason"])
        codes = {item["code"] for item in report["quality"]["issues"]}
        self.assertIn("unverified_test", codes)
        self.assertNotIn("unfinished_job", codes)
        self.assertFalse(report["quality"]["publishable"])
        queue = self.service.review_queue("robot-arm")
        self.assertEqual(queue["recheck_count"], 1)
        self.assertEqual(queue["recheck"][0]["test_id"], "T1")
        # Original test row is retained, not deleted.
        trace = self.service.get_test("T1")
        self.assertEqual(trace["passed"], 2)
        self.assertEqual(trace["device"]["device_id"], "D1")
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_rework_opened_on_invalidated_conclusion_is_flagged(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        self._invalidate()
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        self._extra_lots()
        rework = self.seed_job("J2", quantity=4, number=41, minute=41, parent="J1")
        self.assertEqual(rework["state"], "accepted")
        report = self.service.report("robot-arm", START, END)
        impacted = {job["job_id"]: job for job in report["jobs"] if job["parent_job"]}
        self.assertTrue(impacted["J2"]["rework_impacted"])
        codes = {item["code"] for item in report["quality"]["issues"]}
        self.assertIn("rework_premise_unverified", codes)
        queue = self.service.review_queue("robot-arm")
        self.assertEqual(queue["rework_impacted_count"], 1)
        self.assertEqual(queue["rework_impacted"][0]["job_id"], "J2")
        # Recheck state itself never qualifies as a normal failure path, but
        # rework remains open while the premise is unresolved.
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_reinstate_calibration_clears_recheck_and_restores_views(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 2, 2))
        self._invalidate()
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        reinstated = self.ingest(41, "calibration.reinstate", 41, {"calibration_id": "C-D1"})
        self.assertEqual(reinstated["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        report = self.service.report("robot-arm", START, END)
        self.assertEqual(report["summary"]["unverified_units"], 0)
        self.assertEqual(report["summary"]["failed_units"], 2)
        self.assertEqual(report["summary"]["pass_rate"], "0.5")
        self.assertEqual(report["jobs"][0]["quality_state"], "verified")
        self.assertTrue(report["quality"]["publishable"])
        self.assertEqual(self.service.review_queue("robot-arm")["recheck_count"], 0)
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_redo_test_supersedes_original_and_restores_consistent_view(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        self._invalidate()
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        # A second device with a valid calibration performs the redo.
        self.ingest(42, "equipment.register", 42, {"device_id": "D2", "name": "backup-tester"})
        self.ingest(43, "calibration.record", 43, {"calibration_id": "C-D2", "device_id": "D2",
                                                    "valid_from": "2026-01-01T00:00:00Z",
                                                    "valid_to": "2026-12-31T23:59:59Z"})
        redo = self.ingest(44, "test.record", 44,
                           self.build_test_payload("T2", 3, 1, device_id="D2"))
        self.assertEqual(redo["state"], "accepted")
        self.assertTrue(self.service.get_test("T2")["verified"])
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        report = self.service.report("robot-arm", START, END)
        self.assertEqual(report["summary"]["tested_units"], 4)
        self.assertEqual(report["summary"]["passed_units"], 3)
        self.assertEqual(report["summary"]["failed_units"], 1)
        self.assertEqual(report["summary"]["unverified_units"], 0)
        self.assertEqual(report["summary"]["superseded_units"], 4)
        self.assertEqual(report["summary"]["pass_rate"], "0.75")
        self.assertEqual(report["summary"]["recheck_jobs"], 0)
        tests = {test["test_id"]: test for test in report["jobs"][0]["tests"]}
        self.assertTrue(tests["T1"]["superseded"])
        self.assertFalse(tests["T2"]["superseded"])
        self.assertTrue(tests["T2"]["verified"])
        self.assertTrue(report["quality"]["publishable"])
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_redo_after_failed_premise_keeps_rework_legitimate(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 2, 2))
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        self._extra_lots()
        child = self.seed_job("J2", quantity=2, number=31, minute=31, parent="J1")
        self.assertEqual(child["state"], "accepted")
        self._invalidate()
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        self.ingest(42, "equipment.register", 42, {"device_id": "D2"})
        self.ingest(43, "calibration.record", 43, {"calibration_id": "C-D2", "device_id": "D2",
                                                    "valid_from": "2026-01-01T00:00:00Z",
                                                    "valid_to": "2026-12-31T23:59:59Z"})
        self.ingest(44, "test.record", 44,
                    self.build_test_payload("T2", 4, 0, device_id="D2"))
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        report = self.service.report("robot-arm", START, END)
        rework = next(job for job in report["jobs"] if job["job_id"] == "J2")
        self.assertFalse(rework["rework_impacted"])
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_late_calibration_registration_uses_actual_event_time(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.ingest(13, "equipment.register", 2, {"device_id": "D1"})
        self.seed_job()
        self._complete_job()
        # Test happens while no calibration covers the window: unverified.
        tested = self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        self.assertEqual(tested["state"], "accepted")
        self.assertFalse(self.service.get_test("T1")["verified"])
        self.assertEqual(self.service.get_test("T1")["verify_reason"], "no_calibration_coverage")
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        # Late arrival of a certificate that actually covered test time.
        late = self.ingest(40, "calibration.record", 40,
                           {"calibration_id": "C-D1", "device_id": "D1",
                            "valid_from": "2026-01-01T00:00:00Z",
                            "valid_to": "2026-12-31T23:59:59Z"})
        self.assertEqual(late["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        report = self.service.report("robot-arm", START, END)
        self.assertEqual(report["summary"]["tested_units"], 4)
        self.assertEqual(report["summary"]["unverified_units"], 0)
        self.assertTrue(report["quality"]["publishable"])

    def test_late_invalidation_is_effective_retroactively(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        # Certificate filed later, invalidated by an event whose actual time
        # precedes the test. The test's coverage must be re-evaluated.
        self.ingest(40, "calibration.invalidate", 40, {"calibration_id": "C-D1"})
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        self.assertEqual(self.service.review_queue("robot-arm")["recheck_count"], 1)

    def test_overlapping_validity_intervals_cover_independently(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.ingest(13, "equipment.register", 2, {"device_id": "D1"})
        self.ingest(14, "calibration.record", 3,
                    {"calibration_id": "C1", "device_id": "D1",
                     "valid_from": "2026-01-01T00:00:00Z",
                     "valid_to": "2026-01-12T12:00:00Z"})
        self.ingest(15, "calibration.record", 4,
                    {"calibration_id": "C2", "device_id": "D1",
                     "valid_from": "2026-01-12T00:00:00Z",
                     "valid_to": "2026-12-31T23:59:59Z"})
        self.seed_job()
        self._complete_job(number=16)
        tested = self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        self.assertEqual(tested["state"], "accepted")
        self.assertTrue(self.service.get_test("T1")["verified"])
        # Invalidate one overlapping certificate: the other still covers 09:30.
        self.ingest(40, "calibration.invalidate", 40, {"calibration_id": "C2"})
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        self.assertTrue(self.service.get_test("T1")["verified"])
        self.ingest(41, "calibration.invalidate", 41, {"calibration_id": "C1"})
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        self.ingest(42, "calibration.reinstate", 42, {"calibration_id": "C2"})
        self.assertEqual(self.service.get_job("J1")["status"], "completed")

    def test_separate_devices_have_independent_calibration_state(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device("D1")
        self.ingest(15, "equipment.register", 2, {"device_id": "D2"})
        self.ingest(16, "calibration.record", 3,
                    {"calibration_id": "C-D2", "device_id": "D2",
                     "valid_from": "2026-01-01T00:00:00Z",
                     "valid_to": "2026-12-31T23:59:59Z"})
        self.seed_job("J1", number=17)
        self._complete_job(number=18)
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0, device_id="D1"))
        self.seed_job("J2", quantity=2, number=31, minute=31)
        for number, stage, minute in ((50, "machining", 32), (52, "assembly", 34),
                                      (54, "integration", 36)):
            self.ingest(number, "job.stage_start", minute, {"job_id": "J2", "stage": stage})
            self.ingest(number + 1, "job.stage_finish", minute + 1, {"job_id": "J2", "stage": stage})
        self.ingest(60, "test.record", 38, self.build_test_payload("T2", 2, 0, job_id="J2",
                                                                    device_id="D2"))
        self.ingest(40, "calibration.invalidate", 40, {"calibration_id": "C-D1"})
        self.assertEqual(self.service.get_job("J1")["status"], "recheck")
        self.assertEqual(self.service.get_job("J2")["status"], "completed")
        report = self.service.report("robot-arm", START, END)
        self.assertEqual(report["summary"]["unverified_units"], 4)
        self.assertEqual(report["summary"]["tested_units"], 2)

    def test_frozen_release_stays_intact_but_freshness_explains_change(self):
        report = self.full_scenario()
        frozen = self.service.publish("jan", "robot-arm", START, END, report["fingerprint"])
        self.ingest(40, "calibration.invalidate", 40, {"calibration_id": "C1"})
        stored = self.service.get_release("jan")
        self.assertEqual(stored["fingerprint"], frozen["fingerprint"])
        self.assertEqual(stored["source_head"], frozen["source_head"])
        self.assertEqual(stored["report"]["summary"]["passed_units"], 3)
        freshness = self.service.shelf.freshness("jan")
        self.assertTrue(freshness["current_differs"])
        codes = {item["code"] for item in freshness["change_reasons"]}
        self.assertIn("job_status_changed", codes)
        self.assertIn("test_verification_changed", codes)
        self.assertIn("summary_changed", codes)
        subjects = {item["subject"] for item in freshness["change_reasons"]}
        self.assertIn("J1", subjects)
        self.assertIn("T1", subjects)
        self.assertIn("unverified_units", subjects)

    def test_uncovered_test_does_not_count_as_failure_in_weekly_and_cohorts(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        self._invalidate()
        weekly = self.service.weekly("robot-arm", START, END)
        self.assertEqual(weekly["weeks"][0]["unverified_units"], 4)
        self.assertEqual(weekly["weeks"][0]["tested_units"], 0)
        cohorts = self.service.cohorts("robot-arm", START, END)
        self.assertEqual(cohorts["cohorts"][0]["unverified"], 4)
        self.assertIsNone(cohorts["cohorts"][0]["pass_rate"])
        exposure = self.service.supplier_exposure("robot-arm", START, END)
        self.assertEqual(exposure["suppliers"][0]["associated_unverified_units"], 4)
        self.assertEqual(exposure["suppliers"][0]["associated_failed_units"], 0)

    def test_test_on_unknown_device_waits_then_accepts_on_registration(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self._complete_job()
        outcome = self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        self.assertEqual(outcome["state"], "waiting")
        self.assertEqual(outcome["error_code"], "missing_dependency")
        self.ingest(40, "equipment.register", 2, {"device_id": "D1"})
        self.ingest(41, "calibration.record", 3,
                    {"calibration_id": "C-D1", "device_id": "D1",
                     "valid_from": "2026-01-01T00:00:00Z",
                     "valid_to": "2026-12-31T23:59:59Z"})
        retried = self.service.retry(outcome["id"])
        self.assertEqual(retried["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "completed")

    def test_replacement_test_only_allowed_while_recheck(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        outcome = self.ingest(31, "test.record", 31, self.build_test_payload("T2", 4, 0))
        self.assertEqual(outcome["state"], "rejected")
        self.assertEqual(outcome["error_code"], "test_closed")
        self.assertEqual(len(self.service.get_job("J1")["stages"]), 3)
        self.assertEqual(self.service.get_job("J1")["status"], "completed")

    def test_cross_window_rework_premise_is_still_flagged(self):
        # Parent job opens in January and leaves the February report window, yet
        # a February rework job must still surface its unverified premise.
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self._extra_lots()
        january = self.service.ingest({
            "source": "test", "external_id": "70", "kind": "job.open",
            "occurred_at": "2026-01-20T09:04:00Z",
            "payload": {"job_id": "JAN1", "design_id": "R1", "quantity": 4}})
        self.assertEqual(january["state"], "accepted")
        for number, stage, minute in ((71, "machining", 5), (73, "assembly", 12),
                                      (75, "integration", 21)):
            self.service.ingest({
                "source": "test", "external_id": str(number), "kind": "job.stage_start",
                "occurred_at": f"2026-01-20T09:{minute:02d}:00Z",
                "payload": {"job_id": "JAN1", "stage": stage}})
            self.service.ingest({
                "source": "test", "external_id": str(number + 1), "kind": "job.stage_finish",
                "occurred_at": f"2026-01-20T09:{minute + 4:02d}:00Z",
                "payload": {"job_id": "JAN1", "stage": stage}})
        self.service.ingest({
            "source": "test", "external_id": "80", "kind": "test.record",
            "occurred_at": "2026-01-20T09:30:00Z",
            "payload": self.build_test_payload("TJAN", 2, 2, job_id="JAN1")})
        self.ingest(40, "calibration.invalidate", 40, {"calibration_id": "C-D1"})
        self.service.ingest({
            "source": "test", "external_id": "81", "kind": "job.open",
            "occurred_at": "2026-02-02T09:04:00Z",
            "payload": {"job_id": "FEB1", "design_id": "R1", "quantity": 2,
                        "parent_job": "JAN1"}})
        report = self.service.report("robot-arm", "2026-02-01T00:00:00Z",
                                     "2026-03-01T00:00:00Z")
        codes = {item["code"] for item in report["quality"]["issues"]}
        self.assertIn("rework_premise_unverified", codes)
        queue = self.service.review_queue("robot-arm")
        self.assertEqual({item["job_id"] for item in queue["rework_impacted"]}, {"FEB1"})

    def test_double_invalidation_and_reinstate_guards(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_device()
        self.seed_job()
        self._complete_job()
        self.ingest(30, "test.record", 30, self.build_test_payload("T1", 4, 0))
        self._invalidate()
        again = self.ingest(41, "calibration.invalidate", 41, {"calibration_id": "C-D1"})
        self.assertEqual(again["error_code"], "already_invalidated")
        self.ingest(42, "calibration.reinstate", 42, {"calibration_id": "C-D1"})
        reinstated = self.ingest(43, "calibration.reinstate", 43, {"calibration_id": "C-D1"})
        self.assertEqual(reinstated["error_code"], "not_invalidated")
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_http_endpoints_expose_review_queue_and_device(self):
        client = TestClient(create_app(str(self.path)))
        for item in self._scenario_events():
            response = client.post("/events", json=item)
            self.assertEqual(response.status_code, 200, response.text)
        device = client.get("/equipment/D1")
        self.assertEqual(device.status_code, 200)
        self.assertEqual({c["state"] for c in device.json()["calibrations"]}, {"invalidated"})
        reviews = client.get("/quality/reviews", params={"product": "robot-arm"})
        self.assertEqual(reviews.status_code, 200)
        body = reviews.json()
        self.assertEqual(body["recheck_count"], 1)
        self.assertEqual(body["rework_impacted_count"], 1)
        self.assertEqual(body["recheck"][0]["job_id"], "J1")

    def _scenario_events(self):
        events = []
        for number, kind, minute, payload in [
            (1, "design.release", 0, {"design_id": "R1", "product": "robot-arm",
                                      "revision": 1, "parts": {"motor": 2, "sensor": 1}}),
            (2, "lot.receive", 1, {"lot_id": "M1", "part": "motor", "supplier": "motors", "quantity": 12}),
            (3, "lot.receive", 2, {"lot_id": "S1", "part": "sensor", "supplier": "sensors", "quantity": 6}),
            (4, "lot.receive", 1, {"lot_id": "M2", "part": "motor", "supplier": "motors", "quantity": 12}),
            (5, "lot.receive", 2, {"lot_id": "S2", "part": "sensor", "supplier": "sensors", "quantity": 6}),
            (6, "equipment.register", 2, {"device_id": "D1"}),
            (7, "calibration.record", 3, {"calibration_id": "C-D1", "device_id": "D1",
                                          "valid_from": "2026-01-01T00:00:00Z",
                                          "valid_to": "2026-12-31T23:59:59Z"}),
            (8, "plan.create", 3, {"plan_id": "P1", "product": "robot-arm",
                                   "revision": 1, "minimum_pass_rate": "0.75"}),
            (9, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 4}),
        ]:
            events.append({"source": "test", "external_id": str(number), "kind": kind,
                           "occurred_at": f"2026-01-12T09:{minute:02d}:00Z", "payload": payload})
        base = 20
        for offset, stage, minute in ((0, "machining", 5), (2, "assembly", 12), (4, "integration", 21)):
            for kind, at in (("job.stage_start", minute), ("job.stage_finish", minute + 4)):
                base += 1
                events.append({"source": "test", "external_id": str(base), "kind": kind,
                               "occurred_at": f"2026-01-12T09:{at:02d}:00Z",
                               "payload": {"job_id": "J1", "stage": stage}})
        events.append({"source": "test", "external_id": "30", "kind": "test.record",
                       "occurred_at": "2026-01-12T09:30:00Z",
                       "payload": self.build_test_payload("T1", 4, 0)})
        events.append({"source": "test", "external_id": "40", "kind": "calibration.invalidate",
                       "occurred_at": "2026-01-12T09:40:00Z",
                       "payload": {"calibration_id": "C-D1", "reason": "out-of-tolerance"}})
        events.append({"source": "test", "external_id": "41", "kind": "job.open",
                       "occurred_at": "2026-01-12T09:41:00Z",
                       "payload": {"job_id": "J2", "design_id": "R1", "quantity": 4, "parent_job": "J1"}})
        return events
