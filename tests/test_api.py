import unittest
from datetime import datetime, timezone

from digital_trade_foundation.api import route
from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.governance import OversightService
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)

    def tearDown(self):
        self.database.close()

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_governance_routes_disabled_on_plain_service(self):
        status, payload = route(
            self.service, "POST", "/ai/applications",
            {"request_id": "app", "application_id": "a1", "name": "辅诊", "vendor": "v"},
            {"X-Actor-Id": "bootstrap"})
        self.assertEqual(503, status)
        self.assertEqual("governance_disabled", payload["error"])


class GovernanceApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = OversightService(self.database,
                                        FixedClock(datetime(2026, 10, 5, 8, tzinfo=timezone.utc)))
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap", organization_id="h1", name="医院")
        s.register_actor(request_id="root", actor_id="bootstrap", new_actor_id="admin1",
                         display_name="管理员", role="admin", organization_id="h1")
        for actor_id, role in [("op1", "operator"), ("tech1", "technical_reviewer"),
                               ("clin1", "clinical_owner"), ("sec1", "security_officer"),
                               ("doc1", "clinician")]:
            s.register_actor(request_id="actor-" + actor_id, actor_id="admin1", new_actor_id=actor_id,
                             display_name=actor_id, role=role, organization_id="h1")
        s.register_site(request_id="site", actor_id="op1", site_id="d1", organization_id="h1",
                        name="急诊", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _headers(self, actor_id):
        return {"X-Actor-Id": actor_id}

    def _authorized_deployment(self):
        route(self.service, "POST", "/ai/applications",
              {"request_id": "app", "application_id": "app1", "name": "辅诊", "vendor": "X"},
              self._headers("op1"))
        route(self.service, "POST", "/ai/versions",
              {"request_id": "ver", "application_id": "app1", "version_id": "v1", "version_label": "1",
               "risk_level": "high",
               "validation_summary": {"sample_size": 100, "metrics": {"auc": 0.9}}},
              self._headers("tech1"))
        status, payload = route(self.service, "POST", "/ai/deployments",
                                {"request_id": "dep", "version_id": "v1", "site_id": "d1",
                                 "department": "急诊", "patient_population": "成人",
                                 "thresholds": {"score": {"op": ">=", "value": 0.8}},
                                 "review_policy": {"manual_review_condition": "高风险"},
                                 "takeover_roles": ["clinician"],
                                 "deactivation_plan": {"steps": ["停用"], "owner_role": "operator"}},
                                self._headers("op1"))
        self.assertEqual(201, status)
        deployment_id = payload["resource_id"]
        for request_id, actor, stage in [("apr-t", "tech1", "technical"),
                                         ("apr-c", "clin1", "clinical"),
                                         ("apr-s", "sec1", "security")]:
            status, _ = route(self.service, "POST", "/ai/appraisals",
                              {"request_id": request_id, "deployment_id": deployment_id, "stage": stage,
                               "decision": "approved", "rationale": "ok"}, self._headers(actor))
            self.assertEqual(201, status)
        return deployment_id

    def test_full_governance_flow_over_http(self):
        deployment_id = self._authorized_deployment()
        status, payload = route(self.service, "POST", "/ai/recommendations",
                                {"request_id": "rec", "deployment_id": deployment_id, "task_ref": "task-1",
                                 "patient_ref": "p-1", "payload": {"score": 0.95}},
                                self._headers("doc1"))
        self.assertEqual(201, status)
        recommendation_id = payload["resource_id"]

        # 越序会签在 HTTP 层返回 409
        status, _ = route(self.service, "POST", "/ai/incidents",
                          {"request_id": "inc", "incident_key": "K1", "deployment_id": deployment_id,
                           "severity": "critical", "summary": "异常"}, self._headers("doc1"))
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/ai/versions/profile", None)
        self.assertEqual(400, status)
        status, payload = route(self.service, "GET", "/ai/versions/profile?version_id=v1", None)
        self.assertEqual(200, status)
        self.assertTrue(payload["deployments"][0]["authorized_now"])
        status, payload = route(self.service, "GET",
                                f"/ai/recommendations/trace?recommendation_id={recommendation_id}", None)
        self.assertEqual(200, status)
        self.assertEqual("p-1", payload["recommendation"]["patient_ref"])

    def test_governance_write_requires_actor(self):
        status, payload = route(self.service, "POST", "/ai/applications",
                                {"request_id": "appx", "application_id": "a9", "name": "x", "vendor": "y"},
                                self._headers("nobody"))
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
