"""医疗智能辅助监管域的规则测试。"""

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from digital_trade_foundation.governance import OversightService
from digital_trade_foundation.storage import Database

VALIDATION = {"sample_size": 1200, "metrics": {"sensitivity": 0.93, "specificity": 0.91}}
THRESHOLDS = {"risk_score": {"op": ">=", "value": 0.8}}
POLICY = {"manual_review_condition": "风险分不低于 0.8"}
PLAN = {"steps": ["停止自动下发", "通知值班负责人"], "owner_role": "quality_officer"}


def build_service(database, clock):
    """在给定库上登记组织、岗位、科室、应用与首个版本。"""

    service = OversightService(database, clock)
    service.register_organization(request_id="org1", actor_id="bootstrap", organization_id="h1", name="第一医院")
    service.register_actor(request_id="root", actor_id="bootstrap", new_actor_id="admin1",
                           display_name="管理员", role="admin", organization_id="h1")
    for actor_id, role, name in [
        ("op1", "operator", "运维员"),
        ("tech1", "technical_reviewer", "技术验证员"),
        ("tech2", "technical_reviewer", "技术验证员乙"),
        ("clin1", "clinical_owner", "业务主任"),
        ("sec1", "security_officer", "安全审批员"),
        ("q1", "quality_officer", "质量员"),
        ("doc1", "clinician", "医生甲"),
    ]:
        service.register_actor(request_id="actor-" + actor_id, actor_id="admin1", new_actor_id=actor_id,
                               display_name=name, role=role, organization_id="h1")
    service.register_site(request_id="site-er", actor_id="op1", site_id="dept-er",
                          organization_id="h1", name="急诊", timezone_name="Asia/Shanghai")
    service.register_site(request_id="site-icu", actor_id="op1", site_id="dept-icu",
                          organization_id="h1", name="重症", timezone_name="Asia/Shanghai")
    service.register_application(request_id="app1", actor_id="op1", application_id="app-radio",
                                 name="影像辅助", vendor="供应商X")
    service.register_version(request_id="ver1", actor_id="tech1", application_id="app-radio",
                             version_id="v1", version_label="1.0.0", risk_level="high",
                             validation_summary=VALIDATION)
    return service


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(datetime(2026, 10, 5, 8, tzinfo=timezone.utc))
        self.database = Database()
        self.service = build_service(self.database, self.clock)

    def tearDown(self):
        self.database.close()

    def _deployment(self, request_id="dep1", *, site_id="dept-er", department="急诊",
                    population="成年急诊", thresholds=None, version_id="v1",
                    takeover_roles=("clinician",)):
        receipt = self.service.create_deployment(
            request_id=request_id, actor_id="op1", version_id=version_id, site_id=site_id,
            department=department, patient_population=population,
            thresholds=thresholds or THRESHOLDS, review_policy=POLICY,
            takeover_roles=list(takeover_roles), deactivation_plan=PLAN)
        return receipt.resource_id

    def _authorize(self, deployment_id, prefix="apr"):
        self.service.submit_appraisal(request_id=prefix + "-tech", actor_id="tech1",
                                      deployment_id=deployment_id, stage="technical",
                                      decision="approved", rationale="技术指标达标")
        self.service.submit_appraisal(request_id=prefix + "-clin", actor_id="clin1",
                                      deployment_id=deployment_id, stage="clinical",
                                      decision="approved", rationale="临床可接受")
        self.service.submit_appraisal(request_id=prefix + "-sec", actor_id="sec1",
                                      deployment_id=deployment_id, stage="security",
                                      decision="approved", rationale="安全达标")

    # ------------------------------------------------------------ 上线与会签

    def test_approvals_must_follow_fixed_order(self):
        deployment_id = self._deployment()
        with self.assertRaises(ConflictError):
            self.service.submit_appraisal(request_id="early-sec", actor_id="sec1",
                                          deployment_id=deployment_id, stage="security",
                                          decision="approved", rationale="过早")
        with self.assertRaises(ConflictError):
            self.service.submit_appraisal(request_id="early-clin", actor_id="clin1",
                                          deployment_id=deployment_id, stage="clinical",
                                          decision="approved", rationale="过早")

    def test_appraisal_requires_matching_responsibility(self):
        deployment_id = self._deployment()
        with self.assertRaises(PermissionDenied):
            self.service.submit_appraisal(request_id="wrong-role", actor_id="clin1",
                                          deployment_id=deployment_id, stage="technical",
                                          decision="approved", rationale="越权")
        self._authorize(deployment_id)
        profile = self.service.version_authorization_profile("v1")["deployments"][0]
        self.assertTrue(profile["authorized_now"])
        self.assertEqual(["clinical", "security", "technical"], profile["current_config_approvals"])

    def test_one_person_cannot_cover_two_stages(self):
        # 角色单一：技术验证员即使想在同配置上再签业务也被拒绝
        deployment_id = self._deployment()
        self.service.submit_appraisal(request_id="t1", actor_id="tech1", deployment_id=deployment_id,
                                      stage="technical", decision="approved", rationale="ok")
        with self.assertRaises(PermissionDenied):
            self.service.submit_appraisal(request_id="t2", actor_id="tech1", deployment_id=deployment_id,
                                          stage="clinical", decision="approved", rationale="越权")

    def test_threshold_change_resets_approvals_to_pending(self):
        deployment_id = self._deployment()
        self._authorize(deployment_id)
        self.service.update_deployment_config(
            request_id="cfg1", actor_id="op1", deployment_id=deployment_id,
            thresholds={"risk_score": {"op": ">=", "value": 0.85}})
        profile = self.service.version_authorization_profile("v1")["deployments"][0]
        self.assertEqual("pending", profile["state"]["status"])
        self.assertFalse(profile["authorized_now"])
        self.assertEqual([], profile["current_config_approvals"])
        with self.assertRaises(ConflictError):
            self.service.log_recommendation(request_id="rec-blocked", actor_id="doc1",
                                            deployment_id=deployment_id, task_ref="t1",
                                            patient_ref="p1", payload={"x": 1})

    def test_same_version_different_department_uses_own_threshold(self):
        er = self._deployment("dep-er", site_id="dept-er", department="急诊")
        icu = self._deployment("dep-icu", site_id="dept-icu", department="重症",
                               thresholds={"risk_score": {"op": ">=", "value": 0.6}})
        self._authorize(er, "er")
        # 急诊已授权不代表重症已授权
        profile = self.service.version_authorization_profile("v1")
        by_department = {d["department"]: d for d in profile["deployments"]}
        self.assertTrue(by_department["急诊"]["authorized_now"])
        self.assertFalse(by_department["重症"]["authorized_now"])
        self.assertEqual(0.6, by_department["重症"]["thresholds"]["risk_score"]["value"])
        self.assertNotEqual(er, icu)

    def test_new_version_does_not_inherit_old_validation(self):
        self.service.register_version(
            request_id="ver2", actor_id="tech1", application_id="app-radio", version_id="v2",
            version_label="2.0.0", risk_level="high",
            validation_summary={"sample_size": 300, "metrics": {"sensitivity": 0.88}})
        self._deployment("dep-old", version_id="v1")
        new_dep = self.service.create_deployment(
            request_id="dep-new", actor_id="op1", version_id="v2", site_id="dept-er",
            department="急诊介入", patient_population="成年急诊", thresholds=THRESHOLDS,
            review_policy=POLICY, takeover_roles=["clinician"], deactivation_plan=PLAN).resource_id
        state = self.service.version_authorization_profile("v2")["deployments"][0]["state"]
        self.assertEqual("pending", state["status"])
        with self.assertRaises(ConflictError):
            self.service.log_recommendation(request_id="new-blocked", actor_id="doc1",
                                            deployment_id=new_dep, task_ref="t", patient_ref="p",
                                            payload={"x": 1})

    # ------------------------------------------------------------ 运行连线

    def test_decision_requires_reason_and_authorized_takeover_role(self):
        deployment_id = self._deployment()
        self._authorize(deployment_id)
        rec = self.service.log_recommendation(
            request_id="rec1", actor_id="doc1", deployment_id=deployment_id, task_ref="task-1",
            patient_ref="p-1001", payload={"risk_score": 0.91}).resource_id
        with self.assertRaises(ValidationError):
            self.service.record_decision(request_id="dec-empty", actor_id="doc1",
                                         recommendation_id=rec, disposition="overridden", reason="   ")
        # 质量员不在接管岗位内
        with self.assertRaises(PermissionDenied):
            self.service.record_decision(request_id="dec-q", actor_id="q1", recommendation_id=rec,
                                         disposition="overridden", reason="误报")
        self.service.record_decision(request_id="dec1", actor_id="doc1", recommendation_id=rec,
                                     disposition="overridden", reason="影像伪影，已电话核实")
        trace = self.service.recommendation_trace(rec)
        self.assertEqual("overridden", trace["decisions"][0]["disposition"])
        self.assertEqual("clinician", trace["decisions"][0]["decided_role"])

    def test_recommendation_links_decisions_tasks_and_evidence(self):
        deployment_id = self._deployment()
        self._authorize(deployment_id)
        rec = self.service.log_recommendation(
            request_id="rec2", actor_id="doc1", deployment_id=deployment_id, task_ref="task-1",
            patient_ref="p-1001", payload={"risk_score": 0.91}).resource_id
        self.service.record_decision(request_id="dec2", actor_id="doc1", recommendation_id=rec,
                                     disposition="accepted", reason="与影像一致")
        self.service.link_task(request_id="task-link", actor_id="doc1", recommendation_id=rec,
                               task_ref="task-followup", note="安排随访")
        self.service.add_evidence(request_id="ev1", actor_id="doc1", recommendation_id=rec,
                                  evidence_ref="image-99", data={"series": "CTA-9932"})
        trace = self.service.recommendation_trace(rec)
        self.assertEqual(1, len(trace["decisions"]))
        self.assertEqual(["task-followup"], [t["task_ref"] for t in trace["related_tasks"]])
        self.assertEqual(["image-99"], [e["evidence_ref"] for e in trace["evidences"]])

    # ------------------------------------------------------------ 事故与恢复

    def _raised_incident(self, deployment_id):
        return self.service.report_incident(
            request_id="inc1", actor_id="doc1", incident_key="INC-NIGHT-0401",
            deployment_id=deployment_id, severity="critical", summary="夜班阈值异常",
            patient_ref="p-1001", task_ref="task-1")

    def test_duplicate_reports_form_single_disposition(self):
        deployment_id = self._deployment()
        self._authorize(deployment_id)
        first = self._raised_incident(deployment_id)
        second = self.service.report_incident(
            request_id="inc2", actor_id="q1", incident_key="INC-NIGHT-0401",
            deployment_id=deployment_id, severity="critical", summary="夜班阈值异常",
            patient_ref="p-1001", task_ref="task-1")
        replay = self.service.report_incident(
            request_id="inc1", actor_id="doc1", incident_key="INC-NIGHT-0401",
            deployment_id=deployment_id, severity="critical", summary="夜班阈值异常",
            patient_ref="p-1001", task_ref="task-1")
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        # 原请求重放：保持首次结果 duplicate=False，且不产生第二次处置
        self.assertFalse(replay.duplicate)
        self.assertTrue(replay.replayed)
        timeline = self.service.incident_timeline(first.incident_id)
        self.assertEqual(["reported"], [a["action_type"] for a in timeline["actions"]])

    def test_same_key_with_different_content_conflicts(self):
        deployment_id = self._deployment()
        self._authorize(deployment_id)
        self._raised_incident(deployment_id)
        with self.assertRaises(ConflictError):
            self.service.report_incident(
                request_id="inc-other", actor_id="doc1", incident_key="INC-NIGHT-0401",
                deployment_id=deployment_id, severity="high", summary="内容变了")

    def test_pause_scope_blocks_and_review_due_is_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "governance.sqlite3"
            database = Database(path)
            service = build_service(database, self.clock)
            deployment_id = service.create_deployment(
                request_id="dep1", actor_id="op1", version_id="v1", site_id="dept-er",
                department="急诊", patient_population="成年急诊", thresholds=THRESHOLDS,
                review_policy=POLICY, takeover_roles=["clinician"],
                deactivation_plan=PLAN).resource_id
            for request_id, actor, stage in [("apr-t", "tech1", "technical"),
                                             ("apr-c", "clin1", "clinical"),
                                             ("apr-s", "sec1", "security")]:
                service.submit_appraisal(request_id=request_id, actor_id=actor,
                                         deployment_id=deployment_id, stage=stage,
                                         decision="approved", rationale="ok")
            incident = service.report_incident(
                request_id="inc1", actor_id="doc1", incident_key="INC-NIGHT-0401",
                deployment_id=deployment_id, severity="critical", summary="夜班阈值异常",
                patient_ref="p-1001", task_ref="task-1").incident_id
            service.pause_for_incident(
                request_id="pause1", actor_id="q1", incident_id=incident,
                scope={"patient_ref": "p-1001"}, review_due_at="2026-10-05T10:00:00Z")
            with self.assertRaises(ConflictError):
                service.log_recommendation(
                    request_id="rec-in-scope", actor_id="doc1", deployment_id=deployment_id,
                    task_ref="task-9", patient_ref="p-1001", payload={"x": 1})
            service.log_recommendation(
                request_id="rec-out-scope", actor_id="doc1", deployment_id=deployment_id,
                task_ref="task-3", patient_ref="p-2002", payload={"risk_score": 0.2})
            database.close()

            # 服务重启：暂停状态、范围与复核时限仍然有效
            restarted_db = Database(path)
            restarted = OversightService(restarted_db,
                                         FixedClock(datetime(2026, 10, 5, 12, tzinfo=timezone.utc)))
            overdue = restarted.overdue_reviews()
            self.assertEqual(1, len(overdue))
            self.assertEqual(deployment_id, overdue[0]["deployment_id"])
            state = restarted.version_authorization_profile("v1")["deployments"][0]["state"]
            self.assertEqual("paused", state["status"])
            self.assertEqual("p-1001", state["paused_scope"]["patient_ref"])
            with self.assertRaises(ConflictError):
                restarted.log_recommendation(
                    request_id="rec-still-blocked", actor_id="doc1", deployment_id=deployment_id,
                    task_ref="task-10", patient_ref="p-1001", payload={"x": 1})
            valid, _ = restarted.verify_audit()
            self.assertTrue(valid)
            restarted_db.close()

    def test_recovery_does_not_rewrite_clinical_facts(self):
        deployment_id = self._deployment()
        self._authorize(deployment_id)
        rec = self.service.log_recommendation(
            request_id="rec3", actor_id="doc1", deployment_id=deployment_id, task_ref="task-1",
            patient_ref="p-1001", payload={"risk_score": 0.91}).resource_id
        self.service.record_decision(request_id="dec3", actor_id="doc1", recommendation_id=rec,
                                     disposition="overridden", reason="夜班确认误报")
        incident = self._raised_incident(deployment_id).incident_id
        self.service.pause_for_incident(request_id="pause2", actor_id="q1", incident_id=incident,
                                        review_due_at="2026-10-05T10:00:00Z")
        self.service.start_tracking(request_id="track1", actor_id="q1", incident_id=incident,
                                    note="排查两科室阈值")
        self.service.complete_review(
            request_id="review1", actor_id="q1", incident_id=incident,
            findings="两科室阈值不一致且接管原因缺失", affected_patients=["p-1001", "p-1002"],
            affected_tasks=["task-1"])
        recovered = self.service.recover_incident(request_id="recover1", actor_id="q1",
                                                  incident_id=incident, resolution="统一阈值并培训")
        self.assertFalse(recovered.replayed)
        timeline = self.service.incident_timeline(incident)
        self.assertEqual(
            ["reported", "paused", "tracking_started", "review_completed", "recovered"],
            [a["action_type"] for a in timeline["actions"]])
        self.assertEqual(["p-1001", "p-1002"], timeline["affected_patients"])
        # 临床事实原样保留
        trace = self.service.recommendation_trace(rec)
        self.assertEqual("overridden", trace["decisions"][0]["disposition"])
        self.assertEqual("authorized", timeline["deployment_state"]["status"])

    def test_review_reports_affected_patients_and_tasks(self):
        deployment_id = self._deployment()
        self._authorize(deployment_id)
        incident = self._raised_incident(deployment_id).incident_id
        self.service.pause_for_incident(request_id="pause3", actor_id="q1", incident_id=incident)
        with self.assertRaises(ConflictError):
            self.service.complete_review(
                request_id="early-review", actor_id="q1", incident_id=incident, findings="x",
                affected_patients=[], affected_tasks=[])
        self.service.start_tracking(request_id="track2", actor_id="q1", incident_id=incident)
        self.service.complete_review(
            request_id="review2", actor_id="q1", incident_id=incident,
            findings="圈定影响面", affected_patients=["p-1001"], affected_tasks=["task-1", "task-7"])
        timeline = self.service.incident_timeline(incident)
        self.assertEqual(["task-1", "task-7"], timeline["affected_tasks"])

    def test_version_profile_lists_open_risks_and_takeover_roles(self):
        deployment_id = self._deployment(takeover_roles=("clinician", "quality_officer"))
        self._authorize(deployment_id)
        incident = self._raised_incident(deployment_id).incident_id
        self.service.pause_for_incident(request_id="pause4", actor_id="q1", incident_id=incident,
                                        scope={"whole_deployment": True},
                                        review_due_at="2026-10-05T10:00:00Z")
        profile = self.service.version_authorization_profile("v1")
        types = {risk["type"] for risk in profile["unclosed_risks"]}
        self.assertIn("paused_scope", types)
        self.assertIn("open_incident", types)
        self.assertEqual(["clinician", "quality_officer"],
                         profile["deployments"][0]["takeover_roles"])

    # ------------------------------------------------------------ 并发与幂等

    def test_concurrent_approvals_never_skip_order(self):
        deployment_id = self._deployment()
        errors = []

        def attempt(stage, actor, request_id):
            try:
                self.service.submit_appraisal(
                    request_id=request_id, actor_id=actor, deployment_id=deployment_id,
                    stage=stage, decision="approved", rationale="并发提交")
            except Exception as exc:  # noqa: BLE001 - 记录所有越序结果
                errors.append((stage, type(exc).__name__))

        threads = [
            threading.Thread(target=attempt, args=("clinical", "clin1", "cc1")),
            threading.Thread(target=attempt, args=("security", "sec1", "ss1")),
            threading.Thread(target=attempt, args=("technical", "tech1", "tt1")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 至少业务与安全中的越序尝试必须失败；技术成功后其余也不得自动越序授权
        self.assertTrue(errors)
        stages_present = self.service.version_authorization_profile("v1")["deployments"][0]
        self.assertNotIn("security", stages_present["current_config_approvals"])

    def test_idempotent_writes_replay_receipts(self):
        deployment_id = self._deployment()
        r1 = self.service.submit_appraisal(request_id="same-tech", actor_id="tech1",
                                           deployment_id=deployment_id, stage="technical",
                                           decision="approved", rationale="r")
        r2 = self.service.submit_appraisal(request_id="same-tech", actor_id="tech1",
                                           deployment_id=deployment_id, stage="technical",
                                           decision="approved", rationale="r")
        self.assertFalse(r1.replayed)
        self.assertTrue(r2.replayed)
        self.assertEqual(r1.resource_id, r2.resource_id)


if __name__ == "__main__":
    unittest.main()
