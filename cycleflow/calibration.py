from decimal import Decimal

from .contracts import DomainError, MissingDependency, duration_seconds, keys, required_text, timestamp


def coverage(db, device_id, at):
    """Return active (non-invalidated) calibrations of a device covering time at.

    Validity intervals are half-open [valid_from, valid_to). Overlapping
    certificates of the same device are handled independently: coverage exists
    when at least one active interval contains the event time.
    """
    return [dict(row) for row in db.execute(
        "SELECT * FROM calibrations WHERE device_id=? AND state='active'"
        " AND valid_from<=? AND valid_to>? ORDER BY valid_from,calibration_id",
        (device_id, at, at))]


def invalidated_ids(db, device_id, at):
    return [row["calibration_id"] for row in db.execute(
        "SELECT calibration_id FROM calibrations WHERE device_id=? AND state='invalidated'"
        " AND valid_from<=? AND valid_to>? ORDER BY calibration_id",
        (device_id, at, at))]


class CalibrationRules:
    def __init__(self, db):
        self.db = db

    def register_device(self, payload, occurred_at, event_id):
        keys(payload, {"device_id"}, {"device_id", "name"})
        device_id = required_text(payload["device_id"], "device_id")
        name = required_text(payload.get("name", device_id), "name")
        if self.db.execute("SELECT 1 FROM equipment WHERE device_id=?", (device_id,)).fetchone():
            raise DomainError("duplicate_device", "device already registered", 409)
        self.db.execute("INSERT INTO equipment(device_id,name,registered_at,event_id) VALUES(?,?,?,?)",
                        (device_id, name, occurred_at, event_id))
        return {"device_id": device_id, "name": name, "registered_at": occurred_at}

    def record_calibration(self, payload, occurred_at, event_id):
        keys(payload, {"calibration_id", "device_id", "valid_from", "valid_to"})
        calibration_id = required_text(payload["calibration_id"], "calibration_id")
        device_id = required_text(payload["device_id"], "device_id")
        valid_from = timestamp(payload["valid_from"], "valid_from")
        valid_to = timestamp(payload["valid_to"], "valid_to")
        duration_seconds(valid_from, valid_to)
        device = self.db.execute("SELECT * FROM equipment WHERE device_id=?", (device_id,)).fetchone()
        if device is None:
            raise MissingDependency("device", device_id)
        if occurred_at < device["registered_at"]:
            raise DomainError("time_order", "calibration precedes device registration")
        if self.db.execute("SELECT 1 FROM calibrations WHERE calibration_id=?",
                           (calibration_id,)).fetchone():
            raise DomainError("duplicate_calibration", "calibration already recorded", 409)
        self.db.execute(
            "INSERT INTO calibrations(calibration_id,device_id,valid_from,valid_to,recorded_at,state,event_id)"
            " VALUES(?,?,?,?,?, 'active', ?)",
            (calibration_id, device_id, valid_from, valid_to, occurred_at, event_id))
        self.reconcile()
        return {"calibration_id": calibration_id, "device_id": device_id,
                "valid_from": valid_from, "valid_to": valid_to, "state": "active"}

    def invalidate_calibration(self, payload, occurred_at):
        keys(payload, {"calibration_id"}, {"calibration_id", "reason"})
        calibration_id = required_text(payload["calibration_id"], "calibration_id")
        reason = payload.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 500:
            raise DomainError("invalid_field", "reason must be short text")
        calibration = self._get(calibration_id)
        if occurred_at < calibration["recorded_at"]:
            raise DomainError("time_order", "invalidation precedes calibration record")
        if calibration["state"] == "invalidated":
            raise DomainError("already_invalidated", "calibration already invalidated", 409)
        self.db.execute("UPDATE calibrations SET state='invalidated' WHERE calibration_id=?",
                        (calibration_id,))
        self.reconcile()
        return {"calibration_id": calibration_id, "state": "invalidated", "reason": reason}

    def reinstate_calibration(self, payload, occurred_at):
        """Revoke a mistaken invalidation alarm; coverage is restored for the
        whole original validity interval, effective at the actual event time."""
        keys(payload, {"calibration_id"})
        calibration_id = required_text(payload["calibration_id"], "calibration_id")
        calibration = self._get(calibration_id)
        if occurred_at < calibration["recorded_at"]:
            raise DomainError("time_order", "reinstatement precedes calibration record")
        if calibration["state"] != "invalidated":
            raise DomainError("not_invalidated", "calibration is not invalidated", 409)
        self.db.execute("UPDATE calibrations SET state='active' WHERE calibration_id=?",
                        (calibration_id,))
        self.reconcile()
        return {"calibration_id": calibration_id, "state": "active"}

    def _get(self, calibration_id):
        row = self.db.execute("SELECT * FROM calibrations WHERE calibration_id=?",
                              (calibration_id,)).fetchone()
        if row is None:
            raise MissingDependency("calibration", calibration_id)
        return dict(row)

    def get_device(self, device_id):
        row = self.db.execute("SELECT * FROM equipment WHERE device_id=?", (device_id,)).fetchone()
        if row is None:
            raise MissingDependency("device", device_id)
        result = dict(row)
        result["calibrations"] = [dict(r) for r in self.db.execute(
            "SELECT calibration_id,valid_from,valid_to,recorded_at,state,event_id"
            " FROM calibrations WHERE device_id=? ORDER BY valid_from,calibration_id",
            (device_id,))]
        return result

    # ----- projection reconciliation ------------------------------------

    def verify_test(self, test):
        """Decide verification state of one test row from calibration facts as
        of the test's actual event time. Never treats an unverified result as
        a pass/fail: it only flags coverage."""
        covered = coverage(self.db, test["device_id"], test["tested_at"])
        if covered:
            return 1, None
        invalid = invalidated_ids(self.db, test["device_id"], test["tested_at"])
        if invalid:
            return 0, "calibration_invalidated:" + ",".join(invalid)
        return 0, "no_calibration_coverage"

    def reconcile(self):
        """Recompute verification flags and derived job states from facts.

        Called after any calibration or test event, including out-of-order
        (late) arrivals, so the current view always reflects event actual time.
        Original test rows are never deleted; replacement tests supersede them.
        """
        tests = [dict(row) for row in self.db.execute("SELECT * FROM tests ORDER BY tested_at,test_id")]
        for test in tests:
            verified, reason = self.verify_test(test)
            self.db.execute("UPDATE tests SET verified=?,verify_reason=? WHERE test_id=?",
                            (verified, reason, test["test_id"]))
        job_ids = [row[0] for row in self.db.execute(
            "SELECT DISTINCT job_id FROM tests ORDER BY job_id")]
        for job_id in job_ids:
            self._reconcile_job(job_id)

    def _reconcile_job(self, job_id):
        effective = [dict(row) for row in self.db.execute(
            "SELECT * FROM tests WHERE job_id=? AND superseded=0 ORDER BY tested_at,test_id",
            (job_id,))]
        if not effective:
            return
        job = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            return
        latest = effective[-1]
        plan = self.db.execute("SELECT minimum_pass_rate FROM plans WHERE plan_id=?",
                               (latest["plan_id"],)).fetchone()
        quantity = job["quantity"]
        rate = Decimal(latest["passed"]) / Decimal(quantity)
        accepted = rate >= Decimal(plan["minimum_pass_rate"])
        if latest["verified"]:
            status = "completed" if accepted else "failed"
            reason = None if accepted else "quality_threshold"
        else:
            status = "recheck"
            reason = latest["verify_reason"]
        self.db.execute("UPDATE jobs SET status=?,failure_reason=?,completed_at=COALESCE(completed_at,?) WHERE job_id=?",
                        (status, reason, latest["tested_at"], job_id))

    def dispatch(self, kind, payload, occurred_at, event_id):
        if kind == "equipment.register":
            return self.register_device(payload, occurred_at, event_id)
        if kind == "calibration.record":
            return self.record_calibration(payload, occurred_at, event_id)
        if kind == "calibration.invalidate":
            return self.invalidate_calibration(payload, occurred_at)
        if kind == "calibration.reinstate":
            return self.reinstate_calibration(payload, occurred_at)
        raise DomainError("unknown_kind", f"unsupported calibration event {kind}")
