import json

from .analytics import CycleAnalytics, window
from .contracts import DomainError, MissingDependency, fingerprint, required_text, stable_json, utc_now


class ReportShelf:
    def __init__(self, store):
        self.store = store
        self.analytics = CycleAnalytics(store)

    def preview(self, product, start, end):
        return self.analytics.report(product, start, end)

    def publish(self, release_id, product, start, end, expected_fingerprint):
        release_id = required_text(release_id, "release_id")
        product = required_text(product, "product")
        expected_fingerprint = required_text(expected_fingerprint, "expected_fingerprint")
        start, end = window(start, end)
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            if existing is not None:
                if (existing["product"], existing["window_start"], existing["window_end"],
                    existing["fingerprint"]) == (product, start, end, expected_fingerprint):
                    return self._serialize(existing, repeated=True)
                raise DomainError("release_conflict", "release ID already bound to different content", 409)
            report = self.analytics.report(product, start, end)
            if report["fingerprint"] != expected_fingerprint:
                raise DomainError("stale_preview", "preview fingerprint no longer matches current facts", 409)
            if not report["quality"]["publishable"]:
                raise DomainError("quality_block", "unresolved quality issues prevent publication", 409)
            db.execute(
                "INSERT INTO releases(release_id,product,window_start,window_end,created_at,report,fingerprint,source_head) VALUES(?,?,?,?,?,?,?,?)",
                (release_id, product, start, end, utc_now(), stable_json(report), expected_fingerprint, report["source_head"]))
            row = db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            return self._serialize(row)

    def get(self, release_id):
        with self.store.read() as db:
            row = db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            if row is None:
                raise MissingDependency("release", release_id)
            return self._serialize(row)

    def _serialize(self, row, repeated=False):
        report = json.loads(row["report"])
        stored = report.pop("fingerprint", None)
        if fingerprint(report) != stored or stored != row["fingerprint"]:
            raise DomainError("corrupt_release", "stored report fingerprint is invalid", 500)
        report["fingerprint"] = stored
        return {"release_id": row["release_id"], "created_at": row["created_at"],
                "fingerprint": row["fingerprint"], "source_head": row["source_head"],
                "repeated": repeated, "report": report}

    def list_product(self, product):
        with self.store.read() as db:
            rows = db.execute("SELECT * FROM releases WHERE product=? ORDER BY created_at,release_id", (product,))
            return [self._serialize(row) for row in rows]

    def freshness(self, release_id):
        frozen = self.get(release_id)
        report = frozen["report"]
        current = self.preview(report["product"], report["window_start"], report["window_end"])
        return {"release_id": release_id, "frozen_fingerprint": frozen["fingerprint"],
                "current_fingerprint": current["fingerprint"],
                "current_differs": current["fingerprint"] != frozen["fingerprint"],
                "frozen_source_head": frozen["source_head"], "current_source_head": current["source_head"],
                "change_reasons": self._change_reasons(report, current)}

    def _change_reasons(self, frozen, current):
        """Explain how the current view diverges from the frozen release.

        Historical releases are never rewritten; this makes the drift
        attributable to later calibration, invalidation or replacement tests.
        """
        reasons = []
        frozen_jobs = {job["job_id"]: job for job in frozen["jobs"]}
        current_jobs = {job["job_id"]: job for job in current["jobs"]}
        for job_id in sorted(set(frozen_jobs) | set(current_jobs)):
            before = frozen_jobs.get(job_id)
            after = current_jobs.get(job_id)
            if before is None:
                reasons.append({"code": "job_added", "subject": job_id})
                continue
            if after is None:
                reasons.append({"code": "job_removed", "subject": job_id})
                continue
            if before["status"] != after["status"]:
                reasons.append({"code": "job_status_changed", "subject": job_id,
                                "from_status": before["status"], "to_status": after["status"]})
            before_tests = {test["test_id"]: test for test in before["tests"]}
            after_tests = {test["test_id"]: test for test in after["tests"]}
            for test_id in sorted(set(after_tests) - set(before_tests)):
                reasons.append({"code": "test_added", "subject": test_id, "job_id": job_id})
            for test_id, test in after_tests.items():
                old = before_tests.get(test_id)
                if old is not None and old.get("verified", True) != test.get("verified", True):
                    reasons.append({"code": "test_verification_changed", "subject": test_id,
                                    "job_id": job_id, "verified": test["verified"],
                                    "reason": test.get("verify_reason")})
                if old is not None and old.get("superseded", False) != test.get("superseded", False):
                    reasons.append({"code": "test_superseded", "subject": test_id, "job_id": job_id})
        fields = ("tested_units", "passed_units", "failed_units", "unverified_units",
                  "superseded_units", "rework_impacted_jobs", "recheck_jobs")
        for field in fields:
            old_value = frozen["summary"].get(field, 0)
            new_value = current["summary"].get(field, 0)
            if old_value != new_value:
                reasons.append({"code": "summary_changed", "subject": field,
                                "from_value": old_value, "to_value": new_value})
        return reasons
