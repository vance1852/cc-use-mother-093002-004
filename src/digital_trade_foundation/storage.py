"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 医疗智能辅助监管域 -------------------------------------------------
-- 应用或模型（同一智能辅助产品的稳定标识）
CREATE TABLE IF NOT EXISTS ai_applications (
    application_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    vendor TEXT NOT NULL,
    current_version_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 不可变版本：验证数据摘要与风险分级随版本固化
CREATE TABLE IF NOT EXISTS ai_versions (
    version_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES ai_applications(application_id),
    version_label TEXT NOT NULL,
    validation_summary TEXT NOT NULL,
    validation_digest TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(application_id, version_label)
);
-- 部署范围：一个版本在一个科室+人群上的授权单元，携带阈值、复核条件、接管岗位、停用预案
CREATE TABLE IF NOT EXISTS ai_deployments (
    deployment_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES ai_versions(version_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    department TEXT NOT NULL,
    patient_population TEXT NOT NULL,
    thresholds_json TEXT NOT NULL,
    review_policy_json TEXT NOT NULL,
    takeover_roles_json TEXT NOT NULL,
    deactivation_plan_json TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, site_id, department, patient_population)
);
-- 部署的当前状态：authorized / paused / deactivated（暂停范围与复核时限持久化于此）
CREATE TABLE IF NOT EXISTS deployment_states (
    deployment_id TEXT PRIMARY KEY REFERENCES ai_deployments(deployment_id),
    status TEXT NOT NULL CHECK(status IN ('pending','authorized','paused','deactivated')),
    paused_scope_json TEXT,
    review_due_at TEXT,
    pause_incident_id TEXT,
    updated_at TEXT NOT NULL
);
-- 三方会签：技术验证 -> 业务签署 -> 安全审批，绑定部署配置指纹
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL REFERENCES ai_deployments(deployment_id),
    config_digest TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('technical','clinical','security')),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    decided_by TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(deployment_id, config_digest, stage, decided_by)
);
-- 运行期自动建议（临床事实，只追加）
CREATE TABLE IF NOT EXISTS ai_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL REFERENCES ai_deployments(deployment_id),
    task_ref TEXT NOT NULL,
    patient_ref TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 人工决定（临床事实，只追加；接管必须给原因且岗位有权接管）
CREATE TABLE IF NOT EXISTS human_decisions (
    decision_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL REFERENCES ai_recommendations(recommendation_id),
    disposition TEXT NOT NULL CHECK(disposition IN ('accepted','overridden','deferred')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 关联任务（只追加）
CREATE TABLE IF NOT EXISTS related_tasks (
    related_task_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL REFERENCES ai_recommendations(recommendation_id),
    task_ref TEXT NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(recommendation_id, task_ref)
);
-- 支撑证据（只追加）
CREATE TABLE IF NOT EXISTS evidences (
    evidence_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL REFERENCES ai_recommendations(recommendation_id),
    evidence_ref TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(recommendation_id, evidence_ref)
);
-- 事故：按业务键语义去重，重复上报只形成一次处置
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    incident_key TEXT NOT NULL UNIQUE,
    deployment_id TEXT NOT NULL REFERENCES ai_deployments(deployment_id),
    patient_ref TEXT,
    task_ref TEXT,
    severity TEXT NOT NULL CHECK(severity IN ('low','medium','high','critical')),
    summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','tracking','reviewing','recovered','closed')),
    raised_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 事故处置动作时间线（追踪/复核/恢复，只追加）
CREATE TABLE IF NOT EXISTS incident_actions (
    action_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    action_type TEXT NOT NULL CHECK(action_type IN
        ('reported','paused','tracking_started','review_completed','recovered','closed','note')),
    detail_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incident_actions_incident ON incident_actions(incident_id, created_at, action_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_deployment ON ai_recommendations(deployment_id, created_at);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
