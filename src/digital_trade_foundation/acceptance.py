"""运行基础服务与医疗智能辅助监管域的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .governance import OversightService
from .service import DomainService
from .storage import Database

FIXED_TIME = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
VALIDATION = {"sample_size": 1200, "metrics": {"sensitivity": 0.93, "specificity": 0.91},
              "dataset": "2026Q2急诊回顾性数据"}


def _run_foundation(database: Database) -> dict[str, object]:
    service = DomainService(database, FixedClock(FIXED_TIME))
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-001", name="示范合作机构")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="系统管理员", role="admin", organization_id="org-001")
    service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                           display_name="合作负责人", role="operator", organization_id="org-001")
    service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                          organization_id="org-001", name="一号业务节点", timezone_name="Asia/Shanghai")
    first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                       category="partner_profile", external_key="record-001",
                                       data={"name": "基础资料", "enabled": True})
    replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                        category="partner_profile", external_key="record-001",
                                        data={"name": "基础资料", "enabled": True})
    return {"first_replayed": first.replayed, "second_replayed": replay.replayed}


def _run_governance(database: Database) -> dict[str, object]:
    service = OversightService(database, FixedClock(FIXED_TIME))
    service.register_organization(request_id="g-org", actor_id="bootstrap",
                                  organization_id="hospital-001", name="示范医院")
    service.register_actor(request_id="g-root", actor_id="bootstrap", new_actor_id="h-admin",
                           display_name="管理员", role="admin", organization_id="hospital-001")
    for actor_id, role, name in [
        ("h-op", "operator", "上线运维"),
        ("h-tech", "technical_reviewer", "技术验证员"),
        ("h-clin", "clinical_owner", "业务科主任"),
        ("h-sec", "security_officer", "安全审批员"),
        ("h-quality", "quality_officer", "质量管理员"),
        ("h-doc", "clinician", "值班医生"),
    ]:
        service.register_actor(request_id="g-actor-" + actor_id, actor_id="h-admin",
                               new_actor_id=actor_id, display_name=name, role=role,
                               organization_id="hospital-001")
    service.register_site(request_id="g-site-er", actor_id="h-op", site_id="emergency",
                          organization_id="hospital-001", name="急诊科", timezone_name="Asia/Shanghai")
    service.register_application(request_id="g-app", actor_id="h-op", application_id="radio-assist",
                                 name="影像智能辅助", vendor="供应商X")
    service.register_version(request_id="g-ver-1", actor_id="h-tech", application_id="radio-assist",
                             version_id="model-v1", version_label="1.0.0", risk_level="high",
                             validation_summary=VALIDATION)
    deployment = service.create_deployment(
        request_id="g-dep", actor_id="h-op", version_id="model-v1", site_id="emergency",
        department="急诊科", patient_population="成年急诊患者",
        thresholds={"risk_score": {"op": ">=", "value": 0.8}},
        review_policy={"manual_review_condition": "风险分不低于 0.8 或低置信",
                       "mandatory_reason_on_takeover": True},
        takeover_roles=["clinician", "quality_officer"],
        deactivation_plan={"steps": ["暂停自动建议下发", "通知值班与质量部门"],
                           "owner_role": "quality_officer"})
    deployment_id = deployment.resource_id

    # 三方会签：技术 -> 业务 -> 安全，分属不同人员
    service.submit_appraisal(request_id="g-apr-tech", actor_id="h-tech", deployment_id=deployment_id,
                             stage="technical", decision="approved", rationale="回顾性指标达标")
    service.submit_appraisal(request_id="g-apr-clin", actor_id="h-clin", deployment_id=deployment_id,
                             stage="clinical", decision="approved", rationale="临床流程可接受")
    service.submit_appraisal(request_id="g-apr-sec", actor_id="h-sec", deployment_id=deployment_id,
                             stage="security", decision="approved", rationale="安全与隐私控制达标")

    # 运行：建议 -> 人工接管（必须留原因）-> 任务与证据
    recommendation = service.log_recommendation(
        request_id="g-rec", actor_id="h-doc", deployment_id=deployment_id, task_ref="night-task-0401",
        patient_ref="patient-1001", payload={"risk_score": 0.91, "advice": "建议增强复查"})
    service.record_decision(request_id="g-dec", actor_id="h-doc",
                            recommendation_id=recommendation.resource_id,
                            disposition="overridden", reason="影像伪影，电话核实为误报")
    service.link_task(request_id="g-task", actor_id="h-doc",
                      recommendation_id=recommendation.resource_id, task_ref="followup-1001",
                      note="安排 24 小时随访")
    service.add_evidence(request_id="g-evidence", actor_id="h-doc",
                         recommendation_id=recommendation.resource_id, evidence_ref="cta-9932",
                         data={"series": "CTA-9932", "finding": "伪影"})

    # 事故：重复上报只形成一次处置
    first = service.report_incident(
        request_id="g-incident", actor_id="h-doc", incident_key="NIGHT-20261004-01",
        deployment_id=deployment_id, severity="critical", summary="夜班两科室阈值不一致",
        patient_ref="patient-1001", task_ref="night-task-0401")
    duplicate = service.report_incident(
        request_id="g-incident-repeat", actor_id="h-quality", incident_key="NIGHT-20261004-01",
        deployment_id=deployment_id, severity="critical", summary="夜班两科室阈值不一致",
        patient_ref="patient-1001", task_ref="night-task-0401")

    # 暂停特定患者范围并设定复核时限，随后追踪、复核、恢复
    service.pause_for_incident(
        request_id="g-pause", actor_id="h-quality", incident_id=first.incident_id,
        scope={"patient_ref": "patient-1001"}, review_due_at="2026-10-05T20:00:00Z")
    service.start_tracking(request_id="g-track", actor_id="h-quality",
                           incident_id=first.incident_id, note="排查版本与阈值配置")
    service.complete_review(
        request_id="g-review", actor_id="h-quality", incident_id=first.incident_id,
        findings="同一版本在两个科室阈值不一致，接管缺少完整原因",
        affected_patients=["patient-1001", "patient-1002"],
        affected_tasks=["night-task-0401"])
    service.recover_incident(request_id="g-recover", actor_id="h-quality",
                             incident_id=first.incident_id, resolution="统一阈值、补录接管原因并培训")

    timeline = service.incident_timeline(first.incident_id)
    profile = service.version_authorization_profile("model-v1")
    trace = service.recommendation_trace(recommendation.resource_id)
    return {
        "governance_authorized": profile["deployments"][0]["authorized_now"],
        "incident_duplicate_collapsed": bool(duplicate.duplicate),
        "incident_actions": [a["action_type"] for a in timeline["actions"]],
        "affected_patients": timeline["affected_patients"],
        "clinical_fact_preserved": trace["decisions"][0]["reason"] == "影像伪影，电话核实为误报",
        "takeover_roles": profile["deployments"][0]["takeover_roles"],
    }


def run() -> dict[str, object]:
    """执行基础登记链与监管全链路并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        foundation_path = Path(directory) / "foundation.sqlite3"
        foundation_db = Database(foundation_path)
        foundation = _run_foundation(foundation_db)
        foundation_valid, foundation_events = DomainService(foundation_db).verify_audit()
        foundation_db.close()

        governance_path = Path(directory) / "governance.sqlite3"
        governance_db = Database(governance_path)
        governance = _run_governance(governance_db)
        governance_valid, governance_events = OversightService(governance_db).verify_audit()
        governance_db.close()

        expected_actions = ["reported", "paused", "tracking_started", "review_completed", "recovered"]
        status_ok = (
            foundation_valid and governance_valid
            and not foundation["first_replayed"] and foundation["second_replayed"]
            and governance["governance_authorized"]
            and governance["incident_duplicate_collapsed"]
            and governance["incident_actions"] == expected_actions
            and governance["affected_patients"] == ["patient-1001", "patient-1002"]
            and governance["clinical_fact_preserved"]
        )
        return {
            "status": "ok" if status_ok else "failed",
            "audit_valid": foundation_valid and governance_valid,
            "foundation_audit_events": foundation_events,
            "governance_audit_events": governance_events,
            "first_replayed": foundation["first_replayed"],
            "second_replayed": foundation["second_replayed"],
            **governance,
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
