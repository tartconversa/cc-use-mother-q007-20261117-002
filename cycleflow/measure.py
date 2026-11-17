from decimal import Decimal

from .calibration import CalibrationRules
from .contracts import DomainError, MissingDependency, decimal_value, keys, nonnegative_int, required_text
from .execution import ExecutionRules


class MeasurementRules:
    def __init__(self, db):
        self.db = db
        self.jobs = ExecutionRules(db)
        self.calibrations = CalibrationRules(db)

    def create_plan(self, payload, occurred_at):
        keys(payload, {"plan_id", "product", "revision", "minimum_pass_rate"})
        plan_id = required_text(payload["plan_id"], "plan_id")
        product = required_text(payload["product"], "product")
        revision = nonnegative_int(payload["revision"], "revision", positive=True)
        minimum = decimal_value(payload["minimum_pass_rate"], "minimum_pass_rate")
        if Decimal(minimum) > 1:
            raise DomainError("invalid_threshold", "minimum_pass_rate cannot exceed one")
        design = self.db.execute("SELECT design_id,released_at FROM designs WHERE product=? AND revision=?",
                                 (product, revision)).fetchone()
        if design is None:
            raise MissingDependency("design revision", f"{product}:{revision}")
        if occurred_at < design["released_at"]:
            raise DomainError("time_order", "plan precedes design release")
        self.db.execute("INSERT INTO plans(plan_id,product,revision,minimum_pass_rate,effective_at) VALUES(?,?,?,?,?)",
                        (plan_id, product, revision, minimum, occurred_at))
        return {"plan_id": plan_id, "product": product, "revision": revision, "threshold": minimum}

    def record(self, payload, occurred_at, event_id):
        keys(payload, {"test_id", "job_id", "plan_id", "device_id", "passed", "failed"},
             {"test_id", "job_id", "plan_id", "device_id", "passed", "failed", "note"})
        test_id = required_text(payload["test_id"], "test_id")
        job_id = required_text(payload["job_id"], "job_id")
        plan_id = required_text(payload["plan_id"], "plan_id")
        device_id = required_text(payload["device_id"], "device_id")
        passed = nonnegative_int(payload["passed"], "passed")
        failed = nonnegative_int(payload["failed"], "failed")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise DomainError("invalid_note", "note must be short text")
        job = self.jobs.get(job_id)
        prior_effective = self.db.execute(
            "SELECT * FROM tests WHERE job_id=? AND superseded=0 ORDER BY tested_at,test_id",
            (job_id,)).fetchall()
        if not prior_effective:
            if job["status"] != "awaiting_test":
                raise DomainError("job_not_testable", "job must complete integration before testing", 409)
        else:
            # A replacement (redo) test is only admissible while the prior
            # conclusion is under review. The original row stays on record and
            # is marked superseded rather than deleted.
            if job["status"] != "recheck":
                raise DomainError("test_closed", "only recheck jobs accept a replacement test", 409)
            previous = prior_effective[-1]
            if occurred_at <= previous["tested_at"]:
                raise DomainError("time_order", "replacement test must follow the original test")
        if passed + failed != job["quantity"]:
            raise DomainError("sample_mismatch", "tested units must equal job quantity")
        plan = self.get_plan(plan_id)
        if plan["product"] != job["design"]["product"] or plan["revision"] != job["design"]["revision"]:
            raise DomainError("plan_mismatch", "plan must match exact design revision")
        if occurred_at < max(plan["effective_at"], job["completed_at"]):
            raise DomainError("time_order", "test precedes plan or integration finish")
        device = self.db.execute("SELECT * FROM equipment WHERE device_id=?", (device_id,)).fetchone()
        if device is None:
            raise MissingDependency("device", device_id)
        if occurred_at < device["registered_at"]:
            raise DomainError("time_order", "test precedes device registration")
        if self.db.execute("SELECT 1 FROM tests WHERE test_id=?", (test_id,)).fetchone():
            raise DomainError("duplicate_test", "test already recorded", 409)
        if prior_effective:
            # Supersede before inserting the replacement so the new row never
            # matches the open-range update.
            self.db.execute("UPDATE tests SET superseded=1 WHERE job_id=? AND superseded=0", (job_id,))
        self.db.execute(
            "INSERT INTO tests(test_id,job_id,plan_id,tested_at,passed,failed,note,event_id,device_id)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (test_id, job_id, plan_id, occurred_at, passed, failed, note, event_id, device_id))
        # Verification and job status derive from calibration facts at event
        # actual time; an uncovered test parks the job in recheck instead of
        # treating the result as a failure.
        self.calibrations.reconcile()
        row = self.db.execute("SELECT * FROM tests WHERE test_id=?", (test_id,)).fetchone()
        rate = Decimal(passed) / Decimal(job["quantity"])
        return {"test_id": test_id, "job_id": job_id, "device_id": device_id,
                "verified": bool(row["verified"]), "verify_reason": row["verify_reason"],
                "superseded_tests": [r["test_id"] for r in prior_effective],
                "pass_rate": str(rate), "threshold": plan["minimum_pass_rate"]}

    def get_plan(self, plan_id):
        row = self.db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise MissingDependency("plan", plan_id)
        return dict(row)

    def tests_for_job(self, job_id):
        self.jobs.get(job_id)
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM tests WHERE job_id=? ORDER BY tested_at,test_id", (job_id,))]

    def trace(self, test_id):
        row = self.db.execute("SELECT * FROM tests WHERE test_id=?", (test_id,)).fetchone()
        if row is None:
            raise MissingDependency("test", test_id)
        test = dict(row)
        test["plan"] = self.get_plan(test["plan_id"])
        test["device"] = self.calibrations.get_device(test["device_id"])
        test["job_lineage"] = self.jobs.lineage(test["job_id"])
        test["reservations"] = self.jobs.inventory.reservations(test["job_id"])
        return test

    def dispatch(self, kind, payload, occurred_at, event_id):
        if kind == "plan.create":
            return self.create_plan(payload, occurred_at)
        if kind == "test.record":
            return self.record(payload, occurred_at, event_id)
        raise DomainError("unknown_kind", f"unsupported measurement event {kind}")
