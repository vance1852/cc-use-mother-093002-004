"""医疗智能辅助应用的上线、运行与事故恢复监管服务。

在基础服务的组织/操作者/场所边界上，提供：

- 上线登记：应用、不可变版本（验证摘要、风险分级）、部署范围（科室、人群、
  阈值、人工复核条件、接管岗位、停用预案）；
- 三方会签：技术验证、业务签署、安全审批分属不同岗位并按固定顺序进行，
  版本、阈值或适用范围任一变化都会生成新的配置指纹，批准必须重新评估；
- 运行连线：自动建议、人工决定、关联任务、支撑证据只追加地连接在一起；
- 事故恢复：事件语义去重、按范围暂停并持久化复核时限、追踪/复核/恢复
  只追加，恢复只解除暂停，不改写任何临床事实。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService

# 会签阶段与顺序：技术验证 -> 业务签署 -> 安全审批
STAGE_ORDER = ("technical", "clinical", "security")
STAGE_ROLES = {
    "technical": "technical_reviewer",
    "clinical": "clinical_owner",
    "security": "security_officer",
}
RISK_LEVELS = frozenset({"low", "medium", "high"})
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
DECISION_DISPOSITIONS = frozenset({"accepted", "overridden", "deferred"})
# 事故未闭环的状态
INCIDENT_OPEN_STATUSES = frozenset({"open", "tracking", "reviewing"})


@dataclass(frozen=True)
class IncidentReceipt:
    """描述一次事故上报的稳定结果。"""

    request_id: str
    incident_id: str
    replayed: bool
    duplicate: bool


class OversightService(DomainService):
    """协调智能辅助监管域的权限、顺序、幂等、事务与审计规则。"""

    # ------------------------------------------------------------------ 上线登记

    def register_application(self, *, request_id: str, actor_id: str, application_id: str,
                             name: str, vendor: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "application_id": application_id, "name": name, "vendor": vendor}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            application_id = self._identifier(application_id, "application_id")
            name = self._text(name, "name")
            vendor = self._text(vendor, "vendor")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ai_applications(application_id,name,vendor,current_version_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (application_id, name, vendor, None, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("应用编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="ai.application.registered",
                             resource_type="ai_application", resource_id=application_id,
                             detail={"name": name, "vendor": vendor}, occurred_at=self._now())
                return "ai_application", application_id, {"application_id": application_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_ai_application", payload=payload, create=create)

    def register_version(self, *, request_id: str, actor_id: str, application_id: str,
                         version_id: str, version_label: str, risk_level: str,
                         validation_summary: dict[str, Any]) -> WriteReceipt:
        if not isinstance(validation_summary, dict) or not validation_summary:
            raise ValidationError("validation_summary 必须是非空对象")
        payload = {"actor_id": actor_id, "application_id": application_id, "version_id": version_id,
                   "version_label": version_label, "risk_level": risk_level,
                   "validation_summary": validation_summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "technical_reviewer")
            if connection.execute("SELECT 1 FROM ai_applications WHERE application_id=?",
                                  (application_id,)).fetchone() is None:
                raise NotFoundError("应用不存在")
            version_id = self._identifier(version_id, "version_id")
            version_label = self._text(version_label, "version_label", 80)
            if risk_level not in RISK_LEVELS:
                raise ValidationError("risk_level 必须是 low/medium/high")
            sample_size = validation_summary.get("sample_size")
            if not isinstance(sample_size, int) or sample_size <= 0:
                raise ValidationError("validation_summary.sample_size 必须是正整数")
            metrics = validation_summary.get("metrics")
            if not isinstance(metrics, dict) or not metrics:
                raise ValidationError("validation_summary.metrics 必须是非空对象")
            validation_digest = digest(validation_summary)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ai_versions(version_id,application_id,version_label,validation_summary,"
                        "validation_digest,risk_level,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (version_id, application_id, version_label, canonical_json(validation_summary),
                         validation_digest, risk_level, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("版本编号已经存在或版本标签在该应用下重复") from exc
                # 新版本不会继承旧版本的任何结论：应用当前版本指针更新，旧验证结论只对旧版本有效
                connection.execute(
                    "UPDATE ai_applications SET current_version_id=? WHERE application_id=?",
                    (version_id, application_id),
                )
                append_event(connection, actor_id=actor_id, action="ai.version.registered",
                             resource_type="ai_version", resource_id=version_id,
                             detail={"application_id": application_id, "version_label": version_label,
                                     "risk_level": risk_level, "validation_digest": validation_digest},
                             occurred_at=self._now())
                return "ai_version", version_id, {"version_id": version_id, "validation_digest": validation_digest}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_ai_version", payload=payload, create=create)

    def create_deployment(self, *, request_id: str, actor_id: str, version_id: str, site_id: str,
                          department: str, patient_population: str, thresholds: dict[str, Any],
                          review_policy: dict[str, Any], takeover_roles: list[str],
                          deactivation_plan: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id, "site_id": site_id,
                   "department": department, "patient_population": patient_population,
                   "thresholds": thresholds, "review_policy": review_policy,
                   "takeover_roles": takeover_roles, "deactivation_plan": deactivation_plan}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version = connection.execute("SELECT * FROM ai_versions WHERE version_id=?", (version_id,)).fetchone()
            if version is None:
                raise NotFoundError("版本不存在")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            department = self._text(department, "department", 120)
            patient_population = self._text(patient_population, "patient_population", 200)
            thresholds = self._validate_thresholds(thresholds)
            review_policy = self._validate_review_policy(review_policy)
            takeover_roles = self._validate_takeover_roles(takeover_roles)
            deactivation_plan = self._validate_deactivation_plan(deactivation_plan)
            config = self._config_material(version_id, site_id, department, patient_population, thresholds,
                                           review_policy, takeover_roles, deactivation_plan)
            config_digest = digest(config)
            deployment_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ai_deployments(deployment_id,version_id,site_id,department,patient_population,"
                        "thresholds_json,review_policy_json,takeover_roles_json,deactivation_plan_json,"
                        "config_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (deployment_id, version_id, site_id, department, patient_population,
                         canonical_json(thresholds), canonical_json(review_policy),
                         canonical_json(takeover_roles), canonical_json(deactivation_plan),
                         config_digest, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一版本在该科室与人群上的部署已经存在") from exc
                connection.execute(
                    "INSERT INTO deployment_states(deployment_id,status,paused_scope_json,review_due_at,"
                    "pause_incident_id,updated_at) VALUES(?,?,NULL,NULL,NULL,?)",
                    (deployment_id, "pending", self._now()),
                )
                append_event(connection, actor_id=actor_id, action="ai.deployment.created",
                             resource_type="ai_deployment", resource_id=deployment_id,
                             detail={"version_id": version_id, "site_id": site_id, "department": department,
                                     "patient_population": patient_population, "config_digest": config_digest,
                                     "risk_level": version["risk_level"]},
                             occurred_at=self._now())
                return "ai_deployment", deployment_id, {"deployment_id": deployment_id,
                                                         "config_digest": config_digest}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_ai_deployment", payload=payload, create=create)

    def update_deployment_config(self, *, request_id: str, actor_id: str, deployment_id: str,
                                 thresholds: dict[str, Any] | None = None,
                                 review_policy: dict[str, Any] | None = None,
                                 takeover_roles: list[str] | None = None,
                                 deactivation_plan: dict[str, Any] | None = None,
                                 patient_population: str | None = None) -> WriteReceipt:
        """修改阈值、人工复核条件、接管岗位、停用预案或适用人群。

        任一变化都会改变配置指纹，使三方会签全部失效并重新进入评估；
        若部署正处于事故暂停，暂停状态不会被本操作解除。
        """

        payload = {"actor_id": actor_id, "deployment_id": deployment_id, "thresholds": thresholds,
                   "review_policy": review_policy, "takeover_roles": takeover_roles,
                   "deactivation_plan": deactivation_plan, "patient_population": patient_population}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            current = self._load_deployment(connection, deployment_id)
            thresholds = self._validate_thresholds(thresholds if thresholds is not None
                                                   else json.loads(current["thresholds_json"]))
            review_policy = self._validate_review_policy(review_policy if review_policy is not None
                                                         else json.loads(current["review_policy_json"]))
            takeover_roles = self._validate_takeover_roles(takeover_roles if takeover_roles is not None
                                                           else json.loads(current["takeover_roles_json"]))
            deactivation_plan = self._validate_deactivation_plan(
                deactivation_plan if deactivation_plan is not None
                else json.loads(current["deactivation_plan_json"]))
            patient_population = self._text(patient_population if patient_population is not None
                                            else current["patient_population"], "patient_population", 200)
            config = self._config_material(current["version_id"], current["site_id"], current["department"],
                                           patient_population, thresholds, review_policy, takeover_roles,
                                           deactivation_plan)
            config_digest = digest(config)
            state = connection.execute("SELECT * FROM deployment_states WHERE deployment_id=?",
                                       (deployment_id,)).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                if config_digest == current["config_digest"]:
                    raise ValidationError("配置没有任何变化")
                connection.execute(
                    "UPDATE ai_deployments SET thresholds_json=?,review_policy_json=?,takeover_roles_json=?,"
                    "deactivation_plan_json=?,patient_population=?,config_digest=? WHERE deployment_id=?",
                    (canonical_json(thresholds), canonical_json(review_policy), canonical_json(takeover_roles),
                     canonical_json(deactivation_plan), patient_population, config_digest, deployment_id),
                )
                if state["status"] != "paused":
                    # 配置变化使批准重新进入评估；暂停状态由事故恢复流程专属解除
                    connection.execute(
                        "UPDATE deployment_states SET status='pending',updated_at=? WHERE deployment_id=?",
                        (self._now(), deployment_id),
                    )
                append_event(connection, actor_id=actor_id, action="ai.deployment.config_changed",
                             resource_type="ai_deployment", resource_id=deployment_id,
                             detail={"previous_digest": current["config_digest"],
                                     "config_digest": config_digest,
                                     "kept_paused": state["status"] == "paused"},
                             occurred_at=self._now())
                return "ai_deployment", deployment_id, {"deployment_id": deployment_id,
                                                         "config_digest": config_digest}

            return self._idempotent(connection, request_id=request_id,
                                    action="update_ai_deployment_config", payload=payload, create=create)

    # ------------------------------------------------------------------ 三方会签

    def submit_appraisal(self, *, request_id: str, actor_id: str, deployment_id: str,
                         stage: str, decision: str, rationale: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "deployment_id": deployment_id, "stage": stage,
                   "decision": decision, "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if stage not in STAGE_ROLES:
                raise ValidationError("stage 必须是 technical/clinical/security")
            self._require(actor, STAGE_ROLES[stage])
            if decision not in ("approved", "rejected"):
                raise ValidationError("decision 必须是 approved/rejected")
            rationale = self._text(rationale, "rationale", 2000)
            deployment = self._load_deployment(connection, deployment_id)
            config_digest = deployment["config_digest"]

            # 幂等重放先于顺序与同人等状态性规则：同一请求的重复投递直接返回原回执
            replayed = self._replay_receipt(connection, request_id=request_id,
                                            action="submit_ai_appraisal", payload=payload)
            if replayed is not None:
                return replayed

            prior_signers = {
                row["decided_by"]
                for row in connection.execute(
                    "SELECT DISTINCT decided_by FROM approvals WHERE deployment_id=? AND config_digest=?",
                    (deployment_id, config_digest),
                )
            }
            if actor_id in prior_signers:
                raise PermissionDenied("技术验证、业务签署与安全审批必须由不同人员完成")
            stage_index = STAGE_INDEX[stage]
            if stage_index > 0:
                predecessor = STAGE_ORDER[stage_index - 1]
                approved = connection.execute(
                    "SELECT 1 FROM approvals WHERE deployment_id=? AND config_digest=? AND stage=? "
                    "AND decision='approved' LIMIT 1",
                    (deployment_id, config_digest, predecessor),
                ).fetchone()
                if approved is None:
                    raise ConflictError(f"必须先完成{STAGE_LABELS[predecessor]}阶段")
            rejected = connection.execute(
                "SELECT 1 FROM approvals WHERE deployment_id=? AND config_digest=? AND stage=? "
                "AND decision='rejected' LIMIT 1",
                (deployment_id, config_digest, stage),
            ).fetchone()
            if rejected is not None:
                raise ConflictError("该配置在本阶段已被否决，请修改配置后以新指纹重新提交")
            approval_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO approvals(approval_id,deployment_id,config_digest,stage,decision,"
                        "decided_by,rationale,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (approval_id, deployment_id, config_digest, stage, decision,
                         actor_id, rationale, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一人员对该配置的本阶段会签已存在") from exc
                detail = {"deployment_id": deployment_id, "config_digest": config_digest,
                          "stage": stage, "decision": decision}
                authorized = False
                if decision == "approved" and stage_index == len(STAGE_ORDER) - 1:
                    # 仅在三个阶段对当前指纹全部批准时才授权
                    count = connection.execute(
                        "SELECT COUNT(DISTINCT stage) AS count FROM approvals WHERE deployment_id=? "
                        "AND config_digest=? AND decision='approved'",
                        (deployment_id, config_digest),
                    ).fetchone()["count"]
                    if count == len(STAGE_ORDER):
                        state = connection.execute("SELECT * FROM deployment_states WHERE deployment_id=?",
                                                   (deployment_id,)).fetchone()
                        if state["status"] == "pending":
                            connection.execute(
                                "UPDATE deployment_states SET status='authorized',updated_at=? WHERE deployment_id=?",
                                (self._now(), deployment_id),
                            )
                            authorized = True
                detail["authorized"] = authorized
                append_event(connection, actor_id=actor_id, action="ai.appraisal.submitted",
                             resource_type="ai_appraisal", resource_id=approval_id,
                             detail=detail, occurred_at=self._now())
                return "ai_appraisal", approval_id, {"approval_id": approval_id, "authorized": authorized}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_ai_appraisal", payload=payload, create=create)

    # ------------------------------------------------------------------ 运行连线

    def log_recommendation(self, *, request_id: str, actor_id: str, deployment_id: str,
                           task_ref: str, patient_ref: str, payload: dict[str, Any]) -> WriteReceipt:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        body = {"actor_id": actor_id, "deployment_id": deployment_id, "task_ref": task_ref,
                "patient_ref": patient_ref, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "clinician")
            deployment, state = self._authorized_gate(connection, deployment_id)
            task_ref = self._text(task_ref, "task_ref", 128)
            patient_ref = self._text(patient_ref, "patient_ref", 128)
            self._ensure_not_in_pause_scope(state, deployment, patient_ref, task_ref)
            content_hash = digest(payload)
            recommendation_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO ai_recommendations(recommendation_id,deployment_id,task_ref,patient_ref,"
                    "content_hash,payload_json,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (recommendation_id, deployment_id, task_ref, patient_ref,
                     content_hash, canonical_json(payload), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="ai.recommendation.recorded",
                             resource_type="ai_recommendation", resource_id=recommendation_id,
                             detail={"deployment_id": deployment_id, "task_ref": task_ref,
                                     "patient_ref": patient_ref, "content_hash": content_hash},
                             occurred_at=self._now())
                return "ai_recommendation", recommendation_id, {"recommendation_id": recommendation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="log_ai_recommendation", payload=body, create=create)

    def record_decision(self, *, request_id: str, actor_id: str, recommendation_id: str,
                        disposition: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "recommendation_id": recommendation_id,
                   "disposition": disposition, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if disposition not in DECISION_DISPOSITIONS:
                raise ValidationError("disposition 必须是 accepted/overridden/deferred")
            reason = self._text(reason, "reason", 2000)
            recommendation = connection.execute(
                "SELECT * FROM ai_recommendations WHERE recommendation_id=?", (recommendation_id,)
            ).fetchone()
            if recommendation is None:
                raise NotFoundError("建议不存在")
            deployment = self._load_deployment(connection, recommendation["deployment_id"])
            takeover_roles = json.loads(deployment["takeover_roles_json"])
            if actor.role not in takeover_roles:
                raise PermissionDenied("当前岗位不在该部署允许接管的岗位范围内")
            decision_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO human_decisions(decision_id,recommendation_id,disposition,reason,"
                    "decided_by,decided_role,created_at) VALUES(?,?,?,?,?,?,?)",
                    (decision_id, recommendation_id, disposition, reason,
                     actor_id, actor.role, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="ai.decision.recorded",
                             resource_type="human_decision", resource_id=decision_id,
                             detail={"recommendation_id": recommendation_id, "disposition": disposition,
                                     "decided_role": actor.role, "reason_digest": digest(reason)},
                             occurred_at=self._now())
                return "human_decision", decision_id, {"decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_human_decision", payload=payload, create=create)

    def link_task(self, *, request_id: str, actor_id: str, recommendation_id: str,
                  task_ref: str, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "recommendation_id": recommendation_id,
                   "task_ref": task_ref, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "clinician", "quality_officer")
            if connection.execute("SELECT 1 FROM ai_recommendations WHERE recommendation_id=?",
                                  (recommendation_id,)).fetchone() is None:
                raise NotFoundError("建议不存在")
            task_ref = self._text(task_ref, "task_ref", 128)
            note = str(note).strip()[:500]
            related_task_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO related_tasks(related_task_id,recommendation_id,task_ref,note,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (related_task_id, recommendation_id, task_ref, note, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该建议已经关联同一任务") from exc
                append_event(connection, actor_id=actor_id, action="ai.task.linked",
                             resource_type="related_task", resource_id=related_task_id,
                             detail={"recommendation_id": recommendation_id, "task_ref": task_ref},
                             occurred_at=self._now())
                return "related_task", related_task_id, {"related_task_id": related_task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="link_related_task", payload=payload, create=create)

    def add_evidence(self, *, request_id: str, actor_id: str, recommendation_id: str,
                     evidence_ref: str, data: dict[str, Any]) -> WriteReceipt:
        if not isinstance(data, dict) or not data:
            raise ValidationError("data 必须是非空对象")
        payload = {"actor_id": actor_id, "recommendation_id": recommendation_id,
                   "evidence_ref": evidence_ref, "data": data}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "clinician", "quality_officer")
            if connection.execute("SELECT 1 FROM ai_recommendations WHERE recommendation_id=?",
                                  (recommendation_id,)).fetchone() is None:
                raise NotFoundError("建议不存在")
            evidence_ref = self._identifier(evidence_ref, "evidence_ref")
            content_hash = digest(data)
            evidence_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO evidences(evidence_id,recommendation_id,evidence_ref,content_hash,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (evidence_id, recommendation_id, evidence_ref, content_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该建议已经关联同一证据") from exc
                append_event(connection, actor_id=actor_id, action="ai.evidence.added",
                             resource_type="evidence", resource_id=evidence_id,
                             detail={"recommendation_id": recommendation_id, "evidence_ref": evidence_ref,
                                     "content_hash": content_hash},
                             occurred_at=self._now())
                return "evidence", evidence_id, {"evidence_id": evidence_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_evidence", payload=payload, create=create)

    # ------------------------------------------------------------------ 事故恢复

    def report_incident(self, *, request_id: str, actor_id: str, incident_key: str,
                        deployment_id: str, severity: str, summary: str,
                        patient_ref: str | None = None, task_ref: str | None = None) -> IncidentReceipt:
        """上报事故。同一 incident_key 的重复上报只形成一次处置。"""

        body = {"actor_id": actor_id, "incident_key": incident_key, "deployment_id": deployment_id,
                "severity": severity, "summary": summary, "patient_ref": patient_ref, "task_ref": task_ref}
        body_hash = digest(body)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "clinician", "quality_officer")
            incident_key = self._identifier(incident_key, "incident_key")
            if severity not in SEVERITIES:
                raise ValidationError("severity 必须是 low/medium/high/critical")
            summary = self._text(summary, "summary", 1000)
            patient_ref = self._optional_ref(patient_ref, "patient_ref")
            task_ref = self._optional_ref(task_ref, "task_ref")
            if connection.execute("SELECT 1 FROM ai_deployments WHERE deployment_id=?",
                                  (deployment_id,)).fetchone() is None:
                raise NotFoundError("部署不存在")
            request_id = self._identifier(request_id, "request_id")

            receipt = connection.execute("SELECT * FROM request_receipts WHERE request_id=?",
                                         (request_id,)).fetchone()
            if receipt:
                if receipt["action"] != "report_incident" or receipt["payload_hash"] != body_hash:
                    raise ConflictError("request_id 已被不同内容使用")
                duplicate = bool(json.loads(receipt["response_json"]).get("duplicate"))
                return IncidentReceipt(request_id, receipt["resource_id"], True, duplicate)

            existing = connection.execute("SELECT * FROM incidents WHERE incident_key=?",
                                          (incident_key,)).fetchone()
            if existing is not None:
                if (existing["deployment_id"] != deployment_id or existing["severity"] != severity
                        or existing["patient_ref"] != patient_ref or existing["task_ref"] != task_ref):
                    raise ConflictError("incident_key 已用于内容不同的事故")
                # 语义去重：不追加第二个 reported 动作，也不形成第二次处置
                self._insert_receipt(connection, request_id, "report_incident", body_hash,
                                     "incident", existing["incident_id"],
                                     {"incident_id": existing["incident_id"], "duplicate": True})
                return IncidentReceipt(request_id, existing["incident_id"], False, True)

            incident_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO incidents(incident_id,incident_key,deployment_id,patient_ref,task_ref,severity,"
                "summary,status,raised_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (incident_id, incident_key, deployment_id, patient_ref, task_ref, severity, summary,
                 "open", actor_id, self._now()),
            )
            self._add_action(connection, incident_id=incident_id, action_type="reported",
                             detail={"severity": severity, "summary": summary, "patient_ref": patient_ref,
                                     "task_ref": task_ref}, actor_id=actor_id)
            append_event(connection, actor_id=actor_id, action="ai.incident.reported",
                         resource_type="incident", resource_id=incident_id,
                         detail={"incident_key": incident_key, "deployment_id": deployment_id,
                                 "severity": severity},
                         occurred_at=self._now())
            self._insert_receipt(connection, request_id, "report_incident", body_hash,
                                 "incident", incident_id,
                                 {"incident_id": incident_id, "duplicate": False})
            return IncidentReceipt(request_id, incident_id, False, False)

    def pause_for_incident(self, *, request_id: str, actor_id: str, incident_id: str,
                           scope: dict[str, Any] | None = None,
                           review_due_at: str | None = None) -> WriteReceipt:
        """因严重异常暂停特定范围；暂停范围与复核时限持久化，重启后仍然有效。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "scope": scope,
                   "review_due_at": review_due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_officer", "security_officer")
            incident = self._load_incident(connection, incident_id)
            deployment = self._load_deployment(connection, incident["deployment_id"])
            scope = self._normalize_pause_scope(scope, deployment)
            review_due = self._normalize_due_at(review_due_at)
            state = connection.execute("SELECT * FROM deployment_states WHERE deployment_id=?",
                                       (deployment["deployment_id"],)).fetchone()
            replayed = self._replay_receipt(connection, request_id=request_id,
                                            action="pause_for_incident", payload=payload)
            if replayed is not None:
                return replayed
            if state["status"] not in ("authorized", "paused"):
                raise ConflictError("部署尚未授权，不能按事故暂停")

            def create() -> tuple[str, str, dict[str, Any]]:
                merged_due = review_due
                if state["status"] == "paused" and state["review_due_at"]:
                    if merged_due is None or state["review_due_at"] < merged_due:
                        merged_due = state["review_due_at"]
                connection.execute(
                    "UPDATE deployment_states SET status='paused',paused_scope_json=?,review_due_at=?,"
                    "pause_incident_id=?,updated_at=? WHERE deployment_id=?",
                    (canonical_json(scope), merged_due, incident_id, self._now(),
                     deployment["deployment_id"]),
                )
                action_id = self._add_action(connection, incident_id=incident_id, action_type="paused",
                                             detail={"deployment_id": deployment["deployment_id"], "scope": scope,
                                                     "review_due_at": merged_due}, actor_id=actor_id)
                if incident["status"] == "open":
                    connection.execute("UPDATE incidents SET status='tracking' WHERE incident_id=?",
                                       (incident_id,))
                append_event(connection, actor_id=actor_id, action="ai.incident.paused",
                             resource_type="incident", resource_id=incident_id,
                             detail={"deployment_id": deployment["deployment_id"], "scope": scope,
                                     "review_due_at": merged_due},
                             occurred_at=self._now())
                return "incident_action", action_id, {"action_id": action_id, "review_due_at": merged_due}

            return self._idempotent(connection, request_id=request_id,
                                    action="pause_for_incident", payload=payload, create=create)

    def start_tracking(self, *, request_id: str, actor_id: str, incident_id: str,
                       note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_officer")
            incident = self._load_incident(connection, incident_id)
            note = str(note).strip()[:1000]
            replayed = self._replay_receipt(connection, request_id=request_id,
                                            action="start_tracking", payload=payload)
            if replayed is not None:
                return replayed
            if self._action_exists(connection, incident_id, "tracking_started"):
                raise ConflictError("该事故已经启动追踪")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE incidents SET status='tracking' WHERE incident_id=?",
                                   (incident_id,))
                action_id = self._add_action(connection, incident_id=incident_id,
                                             action_type="tracking_started",
                                             detail={"note": note}, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="ai.incident.tracking_started",
                             resource_type="incident", resource_id=incident_id,
                             detail={"note": note}, occurred_at=self._now())
                return "incident_action", action_id, {"action_id": action_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="start_tracking", payload=payload, create=create)

    def complete_review(self, *, request_id: str, actor_id: str, incident_id: str,
                        findings: str, affected_patients: list[str],
                        affected_tasks: list[str]) -> WriteReceipt:
        """完成复核并圈定受影响患者与任务；必须先启动追踪。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "findings": findings,
                   "affected_patients": affected_patients, "affected_tasks": affected_tasks}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_officer")
            incident = self._load_incident(connection, incident_id)
            replayed = self._replay_receipt(connection, request_id=request_id,
                                            action="complete_review", payload=payload)
            if replayed is not None:
                return replayed
            if not self._action_exists(connection, incident_id, "tracking_started"):
                raise ConflictError("必须先启动追踪才能完成复核")
            findings = self._text(findings, "findings", 4000)
            patients = self._normalize_ref_list(affected_patients, "affected_patients")
            tasks = self._normalize_ref_list(affected_tasks, "affected_tasks")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE incidents SET status='reviewing' WHERE incident_id=?",
                                   (incident_id,))
                action_id = self._add_action(connection, incident_id=incident_id,
                                             action_type="review_completed",
                                             detail={"findings": findings, "affected_patients": patients,
                                                     "affected_tasks": tasks}, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="ai.incident.review_completed",
                             resource_type="incident", resource_id=incident_id,
                             detail={"affected_patients": patients, "affected_tasks": tasks},
                             occurred_at=self._now())
                return "incident_action", action_id, {"action_id": action_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_review", payload=payload, create=create)

    def recover_incident(self, *, request_id: str, actor_id: str, incident_id: str,
                         resolution: str) -> WriteReceipt:
        """恢复运行：只解除本事故造成的暂停，不修改任何建议、决定与证据。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "resolution": resolution}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_officer")
            incident = self._load_incident(connection, incident_id)
            replayed = self._replay_receipt(connection, request_id=request_id,
                                            action="recover_incident", payload=payload)
            if replayed is not None:
                return replayed
            if not self._action_exists(connection, incident_id, "review_completed"):
                raise ConflictError("必须先完成复核才能恢复")
            if incident["status"] not in INCIDENT_OPEN_STATUSES:
                raise ConflictError("事故已经恢复或关闭")
            resolution = self._text(resolution, "resolution", 4000)

            def create() -> tuple[str, str, dict[str, Any]]:
                state = connection.execute("SELECT * FROM deployment_states WHERE deployment_id=?",
                                           (incident["deployment_id"],)).fetchone()
                resumed = False
                if state["status"] == "paused" and state["pause_incident_id"] == incident_id:
                    deployment = self._load_deployment(connection, incident["deployment_id"])
                    # 恢复只解除暂停；若暂停期间配置已变化，回到待授权状态重新会签
                    new_status = ("authorized" if self._fully_approved(connection, deployment)
                                  else "pending")
                    connection.execute(
                        "UPDATE deployment_states SET status=?,paused_scope_json=NULL,review_due_at=NULL,"
                        "pause_incident_id=NULL,updated_at=? WHERE deployment_id=?",
                        (new_status, self._now(), deployment["deployment_id"]),
                    )
                    resumed = True
                connection.execute("UPDATE incidents SET status='recovered' WHERE incident_id=?",
                                   (incident_id,))
                action_id = self._add_action(connection, incident_id=incident_id, action_type="recovered",
                                             detail={"resolution": resolution, "pause_lifted": resumed},
                                             actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="ai.incident.recovered",
                             resource_type="incident", resource_id=incident_id,
                             detail={"resolution_digest": digest(resolution), "pause_lifted": resumed},
                             occurred_at=self._now())
                return "incident_action", action_id, {"action_id": action_id, "pause_lifted": resumed}

            return self._idempotent(connection, request_id=request_id,
                                    action="recover_incident", payload=payload, create=create)

    def close_incident(self, *, request_id: str, actor_id: str, incident_id: str,
                       note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_officer")
            incident = self._load_incident(connection, incident_id)
            replayed = self._replay_receipt(connection, request_id=request_id,
                                            action="close_incident", payload=payload)
            if replayed is not None:
                return replayed
            if incident["status"] != "recovered":
                raise ConflictError("只有已恢复的事故才能关闭")
            note = str(note).strip()[:1000]

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE incidents SET status='closed' WHERE incident_id=?",
                                   (incident_id,))
                action_id = self._add_action(connection, incident_id=incident_id, action_type="closed",
                                             detail={"note": note}, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="ai.incident.closed",
                             resource_type="incident", resource_id=incident_id,
                             detail={"note": note}, occurred_at=self._now())
                return "incident_action", action_id, {"action_id": action_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="close_incident", payload=payload, create=create)

    # ------------------------------------------------------------------ 查询追溯

    def incident_timeline(self, incident_id: str) -> dict[str, Any]:
        """从事故向后给出每项处置与恢复依据，以及受影响患者与任务。"""

        connection = self.database.connection
        incident = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if incident is None:
            raise NotFoundError("事故不存在")
        actions = [self._action_dict(row) for row in connection.execute(
            "SELECT * FROM incident_actions WHERE incident_id=? ORDER BY created_at, rowid", (incident_id,))]
        review = next((a for a in reversed(actions) if a["action_type"] == "review_completed"), None)
        state = connection.execute("SELECT * FROM deployment_states WHERE deployment_id=?",
                                   (incident["deployment_id"],)).fetchone()
        return {
            "incident": {
                "incident_id": incident["incident_id"], "incident_key": incident["incident_key"],
                "deployment_id": incident["deployment_id"], "severity": incident["severity"],
                "status": incident["status"], "summary": incident["summary"],
                "patient_ref": incident["patient_ref"], "task_ref": incident["task_ref"],
                "raised_by": incident["raised_by"], "created_at": incident["created_at"],
            },
            "actions": actions,
            "affected_patients": review["detail"]["affected_patients"] if review else [],
            "affected_tasks": review["detail"]["affected_tasks"] if review else [],
            "deployment_state": None if state is None else self._state_dict(state),
        }

    def recommendation_trace(self, recommendation_id: str) -> dict[str, Any]:
        """把一条自动建议与其人工决定、关联任务和证据连接起来。"""

        connection = self.database.connection
        row = connection.execute("SELECT * FROM ai_recommendations WHERE recommendation_id=?",
                                 (recommendation_id,)).fetchone()
        if row is None:
            raise NotFoundError("建议不存在")
        decisions = [{
            "decision_id": r["decision_id"], "disposition": r["disposition"], "reason": r["reason"],
            "decided_by": r["decided_by"], "decided_role": r["decided_role"], "created_at": r["created_at"],
        } for r in connection.execute(
            "SELECT * FROM human_decisions WHERE recommendation_id=? ORDER BY created_at,rowid",
            (recommendation_id,))]
        tasks = [{"task_ref": r["task_ref"], "note": r["note"], "created_by": r["created_by"],
                  "created_at": r["created_at"]}
                 for r in connection.execute(
                     "SELECT * FROM related_tasks WHERE recommendation_id=? ORDER BY created_at,rowid",
                     (recommendation_id,))]
        evidences = [{"evidence_ref": r["evidence_ref"], "content_hash": r["content_hash"],
                      "created_by": r["created_by"], "created_at": r["created_at"]}
                     for r in connection.execute(
                         "SELECT * FROM evidences WHERE recommendation_id=? ORDER BY created_at,rowid",
                         (recommendation_id,))]
        return {
            "recommendation": {
                "recommendation_id": row["recommendation_id"], "deployment_id": row["deployment_id"],
                "task_ref": row["task_ref"], "patient_ref": row["patient_ref"],
                "content_hash": row["content_hash"], "payload": json.loads(row["payload_json"]),
                "recorded_by": row["recorded_by"], "created_at": row["created_at"],
            },
            "decisions": decisions,
            "related_tasks": tasks,
            "evidences": evidences,
        }

    def version_authorization_profile(self, version_id: str) -> dict[str, Any]:
        """从任一应用版本向前列出当前授权范围、未闭环风险与可接管岗位。"""

        connection = self.database.connection
        version = connection.execute("SELECT * FROM ai_versions WHERE version_id=?", (version_id,)).fetchone()
        if version is None:
            raise NotFoundError("版本不存在")
        application = connection.execute("SELECT * FROM ai_applications WHERE application_id=?",
                                         (version["application_id"],)).fetchone()
        deployments: list[dict[str, Any]] = []
        unclosed_risks: list[dict[str, Any]] = []
        for dep in connection.execute("SELECT * FROM ai_deployments WHERE version_id=? ORDER BY created_at",
                                      (version_id,)):
            state = connection.execute("SELECT * FROM deployment_states WHERE deployment_id=?",
                                       (dep["deployment_id"],)).fetchone()
            approvals = [{
                "stage": r["stage"], "decision": r["decision"], "decided_by": r["decided_by"],
                "rationale": r["rationale"], "created_at": r["created_at"],
                "config_digest": r["config_digest"],
            } for r in connection.execute(
                "SELECT * FROM approvals WHERE deployment_id=? ORDER BY created_at,rowid",
                (dep["deployment_id"],))]
            current_approvals = [a for a in approvals if a["config_digest"] == dep["config_digest"]
                                 and a["decision"] == "approved"]
            open_incidents = [{
                "incident_id": r["incident_id"], "severity": r["severity"], "status": r["status"],
                "summary": r["summary"],
            } for r in connection.execute(
                "SELECT * FROM incidents WHERE deployment_id=? AND status IN ('open','tracking','reviewing') "
                "ORDER BY created_at", (dep["deployment_id"],))]
            state_dict = self._state_dict(state)
            if state_dict["status"] == "paused":
                unclosed_risks.append({"type": "paused_scope", "deployment_id": dep["deployment_id"],
                                       "scope": state_dict["paused_scope"],
                                       "review_due_at": state_dict["review_due_at"]})
            for item in open_incidents:
                unclosed_risks.append({"type": "open_incident", "deployment_id": dep["deployment_id"], **item})
            deployments.append({
                "deployment_id": dep["deployment_id"], "site_id": dep["site_id"],
                "department": dep["department"], "patient_population": dep["patient_population"],
                "thresholds": json.loads(dep["thresholds_json"]),
                "review_policy": json.loads(dep["review_policy_json"]),
                "takeover_roles": json.loads(dep["takeover_roles_json"]),
                "deactivation_plan": json.loads(dep["deactivation_plan_json"]),
                "config_digest": dep["config_digest"],
                "state": state_dict,
                "current_config_approvals": sorted(a["stage"] for a in current_approvals),
                "authorized_now": (state_dict["status"] == "authorized"
                                   and len({a["stage"] for a in current_approvals}) == len(STAGE_ORDER)),
                "open_incidents": open_incidents,
            })
        return {
            "application": {"application_id": application["application_id"], "name": application["name"],
                            "vendor": application["vendor"], "current_version_id": application["current_version_id"]},
            "version": {"version_id": version["version_id"], "application_id": version["application_id"],
                        "version_label": version["version_label"], "risk_level": version["risk_level"],
                        "validation_summary": json.loads(version["validation_summary"]),
                        "validation_digest": version["validation_digest"]},
            "deployments": deployments,
            "unclosed_risks": unclosed_risks,
        }

    def overdue_reviews(self) -> list[dict[str, Any]]:
        """列出已超过复核时限仍处于暂停的部署。"""

        now_text = self._now()
        rows = self.database.connection.execute(
            "SELECT * FROM deployment_states WHERE status='paused' AND review_due_at IS NOT NULL "
            "AND review_due_at<=? ORDER BY review_due_at", (now_text,))
        return [self._state_dict(row) for row in rows]

    # ------------------------------------------------------------------ 内部辅助

    @staticmethod
    def _config_material(version_id: str, site_id: str, department: str, patient_population: str,
                         thresholds: dict[str, Any], review_policy: dict[str, Any],
                         takeover_roles: list[str], deactivation_plan: dict[str, Any]) -> dict[str, Any]:
        return {"version_id": version_id, "site_id": site_id, "department": department,
                "patient_population": patient_population, "thresholds": thresholds,
                "review_policy": review_policy, "takeover_roles": sorted(takeover_roles),
                "deactivation_plan": deactivation_plan}

    def _validate_thresholds(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValidationError("thresholds 必须是非空对象")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            key = self._text(str(key), "thresholds 键名", 80)
            if not isinstance(item, dict):
                raise ValidationError(f"thresholds.{key} 必须是对象")
            bound = item.get("value")
            if not isinstance(bound, (int, float)) or isinstance(bound, bool):
                raise ValidationError(f"thresholds.{key}.value 必须是数值")
            comparator = item.get("op", ">=")
            if comparator not in (">", ">=", "<", "<=", "=="):
                raise ValidationError(f"thresholds.{key}.op 不合法")
            normalized[key] = {"op": comparator, "value": bound}
        return normalized

    def _validate_review_policy(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("review_policy 必须是对象")
        condition = value.get("manual_review_condition")
        if not isinstance(condition, str) or not condition.strip():
            raise ValidationError("review_policy.manual_review_condition 必须是非空字符串")
        required = bool(value.get("mandatory_reason_on_takeover", True))
        return {"manual_review_condition": condition.strip()[:1000],
                "mandatory_reason_on_takeover": required}

    def _validate_takeover_roles(self, value: Any) -> list[str]:
        from .service import ROLES
        if not isinstance(value, list) or not value:
            raise ValidationError("takeover_roles 必须是非空数组")
        roles: list[str] = []
        for item in value:
            if not isinstance(item, str) or item not in ROLES:
                raise ValidationError("takeover_roles 包含未登记的角色")
            if item not in roles:
                roles.append(item)
        return roles

    def _validate_deactivation_plan(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("deactivation_plan 必须是对象")
        steps = value.get("steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, str) and s.strip() for s in steps):
            raise ValidationError("deactivation_plan.steps 必须是非空字符串数组")
        owner = value.get("owner_role")
        from .service import ROLES
        if not isinstance(owner, str) or owner not in ROLES:
            raise ValidationError("deactivation_plan.owner_role 必须是已登记角色")
        return {"steps": [s.strip()[:500] for s in steps], "owner_role": owner}

    def _optional_ref(self, value: str | None, field: str) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if len(value) > 128:
            raise ValidationError(f"{field} 不能超过 128 个字符")
        return value

    def _normalize_ref_list(self, value: Any, field: str) -> list[str]:
        if not isinstance(value, list):
            raise ValidationError(f"{field} 必须是数组")
        result: list[str] = []
        for item in value:
            item = self._optional_ref(item, field)
            if item and item not in result:
                result.append(item)
        return result

    def _normalize_pause_scope(self, scope: Any, deployment) -> dict[str, Any]:
        if scope is None:
            return {"whole_deployment": True}
        if not isinstance(scope, dict) or not scope:
            return {"whole_deployment": True}
        normalized: dict[str, Any] = {}
        for key in ("department", "patient_population"):
            if key in scope and scope[key] is not None:
                text = self._text(scope[key], f"scope.{key}", 200)
                if text != deployment[key]:
                    raise ValidationError(f"暂停范围 {key} 超出该部署")
                normalized[key] = text
        for key in ("patient_ref", "task_ref"):
            text = self._optional_ref(scope.get(key), f"scope.{key}")
            if text:
                normalized[key] = text
        if not normalized:
            return {"whole_deployment": True}
        return normalized

    def _normalize_due_at(self, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("review_due_at 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError("review_due_at 必须带时区")
        return parsed.astimezone().isoformat().replace("+00:00", "Z")

    def _load_deployment(self, connection, deployment_id: str):
        row = connection.execute("SELECT * FROM ai_deployments WHERE deployment_id=?",
                                 (deployment_id,)).fetchone()
        if row is None:
            raise NotFoundError("部署不存在")
        return row

    def _load_incident(self, connection, incident_id: str):
        row = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            raise NotFoundError("事故不存在")
        return row

    def _authorized_gate(self, connection, deployment_id: str):
        deployment = self._load_deployment(connection, deployment_id)
        state = connection.execute("SELECT * FROM deployment_states WHERE deployment_id=?",
                                   (deployment_id,)).fetchone()
        if state is None or state["status"] == "pending":
            raise ConflictError("部署尚未完成授权")
        if state["status"] == "deactivated":
            raise ConflictError("部署已经停用")
        if not self._fully_approved(connection, deployment):
            raise ConflictError("配置发生变化，当前指纹尚未完成三方会签")
        return deployment, state

    def _fully_approved(self, connection, deployment) -> bool:
        count = connection.execute(
            "SELECT COUNT(DISTINCT stage) AS count FROM approvals WHERE deployment_id=? AND config_digest=? "
            "AND decision='approved'", (deployment["deployment_id"], deployment["config_digest"]),
        ).fetchone()["count"]
        return count == len(STAGE_ORDER)

    @staticmethod
    def _ensure_not_in_pause_scope(state, deployment, patient_ref: str, task_ref: str) -> None:
        if state["status"] != "paused":
            return
        scope = json.loads(state["paused_scope_json"]) if state["paused_scope_json"] else {}
        if scope.get("whole_deployment"):
            raise ConflictError("该部署处于暂停状态，不能记录新的自动建议")
        if scope.get("patient_ref") and scope["patient_ref"] == patient_ref:
            raise ConflictError("该患者处于暂停范围")
        if scope.get("task_ref") and scope["task_ref"] == task_ref:
            raise ConflictError("该任务处于暂停范围")

    @staticmethod
    def _action_exists(connection, incident_id: str, action_type: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM incident_actions WHERE incident_id=? AND action_type=? LIMIT 1",
            (incident_id, action_type),
        ).fetchone() is not None

    def _add_action(self, connection, *, incident_id: str, action_type: str,
                    detail: dict[str, Any], actor_id: str) -> str:
        action_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO incident_actions(action_id,incident_id,action_type,detail_json,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (action_id, incident_id, action_type, canonical_json(detail), actor_id, self._now()),
        )
        return action_id

    def _replay_receipt(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> WriteReceipt | None:
        """若请求编号已存在且内容一致，返回原回执；内容不同则冲突；否则返回 None。"""

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?",
                                 (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _insert_receipt(self, connection, request_id: str, action: str, payload_hash: str,
                        resource_type: str, resource_id: str, response: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )

    @staticmethod
    def _action_dict(row) -> dict[str, Any]:
        return {"action_id": row["action_id"], "action_type": row["action_type"],
                "detail": json.loads(row["detail_json"]), "actor_id": row["actor_id"],
                "created_at": row["created_at"]}

    @staticmethod
    def _state_dict(row) -> dict[str, Any]:
        return {"deployment_id": row["deployment_id"], "status": row["status"],
                "paused_scope": json.loads(row["paused_scope_json"]) if row["paused_scope_json"] else None,
                "review_due_at": row["review_due_at"],
                "pause_incident_id": row["pause_incident_id"]}


STAGE_INDEX = {stage: index for index, stage in enumerate(STAGE_ORDER)}
STAGE_LABELS = {"technical": "技术验证", "clinical": "业务签署", "security": "安全审批"}
