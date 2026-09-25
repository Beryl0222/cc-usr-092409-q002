"""医学教学数据沙箱的领域契约测试。

覆盖需求中的全部治理承诺：
限时切片与脱敏、披露检查与小样本阻断、跨校到期收权、
勘误/升级前向影响、同意撤回即时效力、已评分作业指纹冻结、
教师复现与身份隔离、结论四要素溯源、
同意编号的标识边界（绑定/重放/冲突/更正/竞态/恢复）。
"""

import json
import unittest
from datetime import datetime

from domain import (
    CONFLICT_PENDING,
    CONFLICT_RESOLVED,
    CONSENT_CONFLICT,
    CONSENT_REGISTERED,
    CONSENT_REPLAYED,
    EXPORT_APPROVED,
    EXPORT_BLOCKED,
    STATUS_GRADED,
    STATUS_REVOKED,
    STATUS_SANDBOX,
    AuthorizationError,
    NotFound,
    Sandbox,
    SandboxError,
)

SEED = "fixtures/seed.json"
T = lambda s: datetime.fromisoformat(s)  # noqa: E731

# 种子中 CONS-AML-TEACH 的首次落账内容（跨病例复用等场景以此为基准重放/改动）
CONSENT_GRANTED = T("2026-02-25T00:00:00")
CONSENT_NOTE = "覆盖两门口径内教学课程，仅限脱敏切片在限时沙箱内使用，禁止再识别"


def reimport_consent(box, **overrides):
    """以种子中的首次事实为基准重新导入同一编号，按需改动关键字段。"""
    payload = {"consent_id": "CONS-AML-TEACH", "case_id": "CASE-AML",
               "course_id": None, "granted_at": CONSENT_GRANTED,
               "scope_note": CONSENT_NOTE}
    payload.update(overrides)
    return box.grant_consent(**payload)


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_shared_case_enters_two_courses(self):
        case = self.box.cases["CASE-AML"]
        self.assertEqual(set(case["courses"]), {"C-LOCAL", "C-CROSS"})

    def test_six_ledgers_are_separate(self):
        # 六类分账各自独立存放，不混入一个总表
        for ledger in (self.box.cases, self.box.snapshots, self.box.consents,
                       self.box.policies, self.box.environment_versions,
                       self.box.assignments):
            self.assertGreater(len(ledger), 0)
        # 病例账不含数据行，也不含作业结论——分账不混存
        self.assertNotIn("rows", self.box.cases["CASE-AML"])
        self.assertNotIn("conclusion", self.box.cases["CASE-AML"])


class TimeLimitedSliceTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.now = T("2026-04-01T09:00:00")

    def test_slice_is_deidentified_and_objective_scoped(self):
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.now)
        self.assertEqual(session["status"], STATUS_SANDBOX)
        row = session["rows"][0]
        # 只含教学目标字段，且身份字段永不下发、城市被丢弃
        self.assertEqual(set(row), {"age", "diagnosis", "cell_type", "marker"})
        self.assertNotIn("patient_id", row)
        self.assertNotIn("city", row)
        # 年龄按十岁段泛化
        self.assertEqual(row["age"], "40-49")
        self.assertTrue(session["expires_at"] > self.now)

    def test_slice_expires(self):
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.now,
                                       ttl_minutes=240)
        after = session["expires_at"]
        dead = self.box._session_live(session, after)
        self.assertEqual(dead, "切片过期")

    def test_student_from_other_course_cannot_get_slice(self):
        # 王学生只注册跨校课程，不能取得本校课程切片
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-WANG", "TASK-LOCAL-Q1", self.now)


class DisclosureTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.now = T("2026-04-01T09:00:00")
        self.session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.now)

    def _export(self, export_id, groups, columns):
        return self.box.request_export(
            export_id, "S-LIN", self.session["id"], groups, columns, self.now,
            assignment_id="AS-LIN-01")

    def test_small_sample_group_is_blocked(self):
        # AML-M5 只有 3 例，低于 k≥5
        record = self._export("EXP-1", [{"key": "AML-M5", "count": 3}],
                              ["diagnosis", "cell_type"])
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertTrue(any("小样本" in reason for reason in record["reasons"]))

    def test_compliant_export_is_approved(self):
        record = self._export("EXP-2", [{"key": "AML-M2", "count": 5}],
                              ["diagnosis", "cell_type", "age"])
        self.assertEqual(record["decision"], EXPORT_APPROVED)
        self.assertEqual(record["reasons"], [])

    def test_identity_column_is_blocked_even_with_enough_rows(self):
        record = self._export("EXP-3", [{"key": "AML-M2", "count": 8}],
                              ["diagnosis", "patient_name"])
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertTrue(any("身份字段" in reason for reason in record["reasons"]))

    def test_export_after_slice_expiry_is_blocked(self):
        later = T("2026-04-01T14:00:00")  # 默认 240 分钟后
        record = self.box.request_export(
            "EXP-4", "S-LIN", self.session["id"],
            [{"key": "AML-M2", "count": 5}], ["diagnosis"], later)
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertIn("切片过期", record["reasons"])


class CrossCourseWithdrawalTest(unittest.TestCase):
    """同一病例进入两个课程，教学中途撤回同意。"""

    WITHDRAW_AT = T("2026-05-05T12:00:00")

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        day_before = T("2026-05-04T09:00:00")
        self.local_session = self.box.issue_slice(
            "S-LIN", "TASK-LOCAL-Q1", day_before, ttl_minutes=2880)
        self.cross_session = self.box.issue_slice(
            "S-WANG", "TASK-CROSS-Q1", day_before, ttl_minutes=2880)
        # 撤回前：合规导出曾获批；小样本导出在披露队列
        self.box.request_export(
            "EXP-OK", "S-LIN", self.local_session["id"],
            [{"key": "AML-M2", "count": 5}], ["diagnosis"], day_before)
        self.box.request_export(
            "EXP-SMALL", "S-WANG", self.cross_session["id"],
            [{"key": "AML-M5", "count": 3}], ["diagnosis"], day_before)

    def test_withdrawal_blocks_small_sample_and_live_exports(self):
        self.assertEqual(self.box.exports["EXP-SMALL"]["decision"], EXPORT_BLOCKED)
        self.assertEqual(self.box.exports["EXP-OK"]["decision"], EXPORT_APPROVED)
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        # 小样本导出保持阻断；曾获批的导出因撤回立即失效
        self.assertEqual(self.box.exports["EXP-SMALL"]["decision"], EXPORT_BLOCKED)
        self.assertEqual(self.box.exports["EXP-OK"]["decision"], EXPORT_BLOCKED)
        self.assertIn("同意撤回", self.box.exports["EXP-OK"]["reasons"])

    def test_withdrawal_revokes_sessions_in_both_courses(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        self.assertEqual(self.local_session["status"], STATUS_REVOKED)
        self.assertEqual(self.cross_session["status"], STATUS_REVOKED)
        # 撤回后两门课都不能再取切片
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.WITHDRAW_AT)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-WANG", "TASK-CROSS-Q1", self.WITHDRAW_AT)

    def test_affected_assignments_are_listed_across_both_courses(self):
        report = self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        ids = {row["assignment_id"] for row in report["assignments"]}
        self.assertEqual(ids, {"AS-LIN-01", "AS-WANG-01", "AS-GAO-01"})
        courses = {row["course_id"] for row in report["assignments"]}
        self.assertEqual(courses, {"C-LOCAL", "C-CROSS"})

    def test_graded_work_keeps_fingerprint_and_is_flagged(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        lin = self.box.assignments["AS-LIN-01"]
        self.assertEqual(lin["status"], STATUS_GRADED)
        self.assertEqual(lin["grade"], "A")
        self.assertEqual(lin["conclusion"],
                         "AML-M2 组原始粒细胞占比高，CD34 阳性为主")
        self.assertEqual(lin["fingerprint"]["snapshot"]["version"], 1)
        self.assertEqual(lin["fingerprint"]["environment"]["version"], 1)
        types = {flag["type"] for flag in lin["risk_flags"]}
        self.assertIn("同意撤回", types)

    def test_new_small_sample_request_after_withdrawal_stays_blocked(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        # 撤回后仍在试图导出小样本组：会话已撤回且小样本，双重阻断
        record = self.box.request_export(
            "EXP-AFTER", "S-WANG", self.cross_session["id"],
            [{"key": "AML-M5", "count": 3}], ["diagnosis"], self.WITHDRAW_AT)
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertTrue(any("小样本" in r for r in record["reasons"]))
        self.assertTrue(any("同意撤回" in r or "撤回" in r for r in record["reasons"]))

    def test_double_withdraw_is_rejected(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        with self.assertRaises(SandboxError):
            self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)


class ErratumAndUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_graded_fingerprint_pins_old_versions(self):
        lin = self.box.assignments["AS-LIN-01"]
        self.assertEqual(lin["fingerprint"]["snapshot"]["version"], 1)
        self.assertEqual(lin["fingerprint"]["environment"]["version"], 1)
        types = {f["type"] for f in lin["risk_flags"]}
        self.assertEqual(types, {"病例勘误", "工具升级"})

    def test_old_snapshot_remains_immutable_after_erratum(self):
        # 勘误以新版本发布，v1 内容绝不被改写
        v1 = self.box.snapshots[("CASE-AML", 1)]
        self.assertEqual(v1["rows"][2]["cell_type"], "早幼粒细胞")
        self.assertEqual(self.box.snapshots[("CASE-AML", 2)]["rows"][2]["cell_type"],
                         "异常早幼粒细胞")

    def test_new_tasks_pin_latest_versions_only_at_creation(self):
        # 5 月新建的任务自动钉到新版本
        may_task = self.box.tasks["TASK-LOCAL-Q2"]
        self.assertEqual((may_task["snapshot_version"], may_task["environment_version"]),
                         (2, 2))
        # 但 4 月时点创建的任务只能钉到当时已发布的版本
        april_task = self.box.create_task(
            "TASK-CHECK", "C-LOCAL", "CASE-AML",
            ["diagnosis"], "POL-K5", "ENV-SCANPY", now=T("2026-04-01T00:00:00"))
        self.assertEqual((april_task["snapshot_version"], april_task["environment_version"]),
                         (1, 1))

    def test_grading_is_append_only(self):
        with self.assertRaises(SandboxError):
            self.box.grade_assignment(
                "T-CHEN", "AS-LIN-01", "C", "改分", T("2026-05-20T00:00:00"))


class TeacherReproduceTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_teacher_reproduces_in_frozen_environment_without_identity(self):
        report = self.box.reproduce_report(
            "T-CHEN", "AS-LIN-01", T("2026-05-20T00:00:00"))
        self.assertEqual(report["status"], "复现一致")
        self.assertTrue(report["fingerprint_intact"])
        self.assertTrue(report["rerun"]["matches_graded"])
        self.assertEqual(report["identity_access"], "拒绝")
        self.assertIn("patient_id", report["identity_fields"])

    def test_teacher_cannot_reproduce_other_course(self):
        # 陈教师无权复现跨校课程的作业，即便病例相同
        with self.assertRaises(AuthorizationError):
            self.box.reproduce_report("T-CHEN", "AS-GAO-01",
                                      T("2026-05-20T00:00:00"))
        # 赵教师可以复现自己课程的作业
        report = self.box.reproduce_report(
            "T-ZHAO", "AS-GAO-01", T("2026-05-20T00:00:00"))
        self.assertEqual(report["status"], "复现一致")

    def test_teacher_capability_never_includes_identity_read(self):
        with self.assertRaises(AuthorizationError):
            self.box.read_patient_identity("T-CHEN", "CASE-AML")

    def test_repro_detects_tool_missing_in_frozen_image(self):
        # 作业使用了冻结镜像里不存在的工具 → 复现必须判为不一致
        now = T("2026-04-01T09:00:00")
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", now)
        self.box.submit_assignment(
            "AS-TAMPER", "S-LIN", "TASK-LOCAL-Q1", "可疑结论",
            [{"step": "用外部工具重聚类", "tool": "seurat"}], now,
            slice_ids=[session["id"]])
        self.box.grade_assignment(
            "T-CHEN", "AS-TAMPER", "C", "工具来源存疑", T("2026-04-05T00:00:00"))
        report = self.box.reproduce_report(
            "T-CHEN", "AS-TAMPER", T("2026-05-20T00:00:00"))
        self.assertEqual(report["status"], "复现不一致")
        self.assertEqual(report["rerun"]["missing_tools"], ["seurat"])


class ExpiryTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.session = self.box.issue_slice(
            "S-WANG", "TASK-CROSS-Q1", T("2026-05-14T09:00:00"),
            ttl_minutes=2880)

    def test_cross_institutional_course_expiry_revokes_access(self):
        revoked = self.box.sweep_expired(T("2026-05-16T00:00:00"))
        self.assertIn("S-WANG@C-CROSS", revoked)
        self.assertIn("S-GAO@C-CROSS", revoked)
        # 进行中的沙箱会话一并收回
        self.assertEqual(self.session["status"], STATUS_REVOKED)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-WANG", "TASK-CROSS-Q1",
                                 T("2026-05-16T09:00:00"))

    def test_local_course_unaffected_when_cross_course_expires(self):
        self.box.sweep_expired(T("2026-05-16T00:00:00"))
        enrollment = self.box._enrollment("S-LIN", "C-LOCAL")
        self.assertNotEqual(enrollment["status"], STATUS_REVOKED)
        # 本校课程仍开放，切片正常
        session = self.box.issue_slice(
            "S-LIN", "TASK-LOCAL-Q2", T("2026-05-16T09:00:00"))
        self.assertEqual(session["status"], STATUS_SANDBOX)

    def test_sweep_is_idempotent(self):
        first = self.box.sweep_expired(T("2026-07-01T00:00:00"))
        second = self.box.sweep_expired(T("2026-07-02T00:00:00"))
        self.assertIn("S-LIN@C-LOCAL", first)
        self.assertEqual(second, [])


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_trace_has_four_required_parts(self):
        trace = self.box.trace("AS-LIN-01")
        self.assertIn("数据范围", trace)
        self.assertIn("处理步骤", trace)
        self.assertIn("课程授权", trace)
        self.assertIn("教师复核", trace)

    def test_trace_points_to_exact_data_scope(self):
        scope = self.box.trace("AS-LIN-01")["数据范围"]
        self.assertEqual(scope["case_id"], "CASE-AML")
        self.assertEqual(scope["snapshot_version"], 1)
        self.assertEqual(scope["objective_fields"],
                         ["age", "diagnosis", "cell_type", "marker"])
        self.assertEqual(len(scope["content_hash"]), 16)

    def test_trace_records_authorization_lineage(self):
        auth = self.box.trace("AS-LIN-01")["课程授权"]
        self.assertEqual(auth["course_id"], "C-LOCAL")
        self.assertEqual(auth["student_id"], "S-LIN")
        self.assertEqual(auth["consents"][0]["consent_id"], "CONS-AML-TEACH")
        self.assertEqual(auth["consents"][0]["withdrawn_at"], None)

    def test_trace_records_teacher_review(self):
        reviews = self.box.trace("AS-LIN-01")["教师复核"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["teacher_id"], "T-CHEN")
        self.assertEqual(reviews[0]["grade"], "A")

    def test_trace_shows_withdrawal_after_it_happens(self):
        self.box.withdraw_consent("CONS-AML-TEACH", T("2026-05-05T12:00:00"))
        consent = self.box.trace("AS-LIN-01")["课程授权"]["consents"][0]
        self.assertEqual(consent["withdrawn_at"], "2026-05-05T12:00:00")
        self.assertTrue(
            any(f["type"] == "同意撤回" for f in self.box.trace("AS-LIN-01")["风险标记"]))

    def test_case_listing_covers_both_courses(self):
        listing = self.box.assignments_for_case("CASE-AML")
        self.assertEqual({row["assignment_id"] for row in listing},
                         {"AS-LIN-01", "AS-WANG-01", "AS-GAO-01"})
        self.assertEqual({row["course_id"] for row in listing},
                         {"C-LOCAL", "C-CROSS"})


class ConsentIdentityBoundaryTest(unittest.TestCase):
    """同意编号的标识边界：首次落账绑定关键字段，重放幂等，冲突不覆盖。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.now = T("2026-04-01T09:00:00")

    def test_first_landing_binds_key_fields(self):
        grant = self.box.consents["CONS-AML-TEACH"]
        self.assertEqual(grant["case_id"], "CASE-AML")
        self.assertIsNone(grant["course_id"])
        self.assertEqual(grant["granted_at"], CONSENT_GRANTED)
        self.assertEqual(grant["scope_note"], CONSENT_NOTE)
        self.assertEqual(grant["source"], "种子装载")
        self.assertEqual(len(grant["binding_hash"]), 16)

    def test_new_consent_id_registers_normally(self):
        result = self.box.grant_consent(
            "CONS-T2D-LOCAL", "CASE-T2D", "C-LOCAL",
            T("2026-03-01T00:00:00"), "仅限本校课程", now=self.now)
        self.assertEqual(result["outcome"], CONSENT_REGISTERED)
        self.assertIsNone(result["conflict"])
        self.assertEqual(self.box.consents["CONS-T2D-LOCAL"]["case_id"], "CASE-T2D")

    def test_exact_replay_returns_original_record(self):
        result = reimport_consent(self.box)
        self.assertEqual(result["outcome"], CONSENT_REPLAYED)
        self.assertIs(result["grant"], self.box.consents["CONS-AML-TEACH"])
        self.assertIsNone(result["conflict"])
        grant = self.box.consents["CONS-AML-TEACH"]
        self.assertEqual(grant["replay_count"], 1)
        self.assertEqual(self.box.consent_conflicts, [])
        self.assertTrue(any(entry["action"] == "同意重放"
                            for entry in self.box.audit))

    def test_cross_case_reuse_registers_conflict_and_keeps_first_fact(self):
        # 批量导入事故：另一个病例包误用了同一编号
        result = reimport_consent(self.box, case_id="CASE-T2D")
        self.assertEqual(result["outcome"], CONSENT_CONFLICT)
        self.assertEqual(result["conflict"]["changed_fields"], ["case_id"])
        self.assertEqual(result["conflict"]["status"], CONFLICT_PENDING)
        # 首次事实保留：第一门课程下一次签发切片不再被误判为无有效同意
        grant = self.box.consents["CONS-AML-TEACH"]
        self.assertEqual(grant["case_id"], "CASE-AML")
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.now)
        self.assertEqual(session["status"], STATUS_SANDBOX)
        # 后到内容不获得能力：CASE-T2D 依旧没有有效同意
        self.box.add_snapshot("CASE-T2D", 1, [{"age": 55, "diagnosis": "T2D"}],
                              identity_fields=[], released_at=T("2026-03-01T00:00:00"))
        self.box.create_task("TASK-T2D-Q1", "C-LOCAL", "CASE-T2D",
                             ["age", "diagnosis"], "POL-K5", "ENV-SCANPY",
                             now=self.now)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-LIN", "TASK-T2D-Q1", self.now)

    def test_same_case_changed_scope_registers_conflict(self):
        # 同病例换范围：通用授权被改成课程专用
        result = reimport_consent(self.box, course_id="C-LOCAL")
        self.assertEqual(result["outcome"], CONSENT_CONFLICT)
        self.assertEqual(result["conflict"]["changed_fields"], ["course_id"])
        # 首次事实是“教学通用”：跨校课程照常签发（若被覆盖将失去授权）
        self.assertIsNone(self.box.consents["CONS-AML-TEACH"]["course_id"])
        session = self.box.issue_slice("S-WANG", "TASK-CROSS-Q1", self.now)
        self.assertEqual(session["status"], STATUS_SANDBOX)

    def test_any_key_field_change_registers_conflict(self):
        variants = [
            ({"case_id": "CASE-T2D"}, ["case_id"]),
            ({"course_id": "C-LOCAL"}, ["course_id"]),
            ({"granted_at": T("2026-03-01T00:00:00")}, ["granted_at"]),
            ({"scope_note": "改写后的授权说明"}, ["scope_note"]),
        ]
        for overrides, expected_fields in variants:
            with self.subTest(overrides=overrides):
                box = Sandbox.from_seed(SEED)
                result = reimport_consent(box, **overrides)
                self.assertEqual(result["outcome"], CONSENT_CONFLICT)
                self.assertEqual(result["conflict"]["changed_fields"],
                                 expected_fields)
                grant = box.consents["CONS-AML-TEACH"]
                self.assertEqual(grant["case_id"], "CASE-AML")
                self.assertIsNone(grant["course_id"])
                self.assertEqual(grant["granted_at"], CONSENT_GRANTED)
                self.assertEqual(grant["scope_note"], CONSENT_NOTE)

    def test_conflict_does_not_overwrite_authorization_flags_or_audit(self):
        before_grant = dict(self.box.consents["CONS-AML-TEACH"])
        before_flags = {aid: list(a["risk_flags"])
                        for aid, a in self.box.assignments.items()}
        before_audit = [dict(entry) for entry in self.box.audit]
        reimport_consent(self.box, case_id="CASE-T2D")
        # 已提交授权原样保留
        self.assertEqual(dict(self.box.consents["CONS-AML-TEACH"]), before_grant)
        # 风险标记不受影响
        after_flags = {aid: list(a["risk_flags"])
                       for aid, a in self.box.assignments.items()}
        self.assertEqual(after_flags, before_flags)
        # 审计只追加、不改写
        self.assertEqual(self.box.audit[:len(before_audit)], before_audit)
        self.assertTrue(any(entry["action"] == "同意冲突"
                            for entry in self.box.audit[len(before_audit):]))


class ConsentRaceTest(unittest.TestCase):
    """撤回、课程到期、导出检查与导入并发：一律按已提交事实决定结果。"""

    WITHDRAW_AT = T("2026-05-05T12:00:00")

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_replay_after_withdrawal_does_not_resurrect_consent(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        # 撤回后补发完全一致的导入：返回原记录，但撤回事实不变
        result = reimport_consent(self.box)
        self.assertEqual(result["outcome"], CONSENT_REPLAYED)
        self.assertEqual(result["grant"]["withdrawn_at"], self.WITHDRAW_AT)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", T("2026-05-06T09:00:00"))

    def test_changed_import_after_withdrawal_stays_powerless(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        # 后到内容试图把授权时间改到撤回之后：只登记冲突，已撤回事实不动
        result = reimport_consent(self.box, granted_at=T("2026-05-06T00:00:00"))
        self.assertEqual(result["outcome"], CONSENT_CONFLICT)
        self.assertEqual(result["conflict"]["changed_fields"], ["granted_at"])
        self.assertEqual(self.box.consents["CONS-AML-TEACH"]["withdrawn_at"],
                         self.WITHDRAW_AT)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-WANG", "TASK-CROSS-Q1", T("2026-05-06T09:00:00"))

    def test_export_check_uses_committed_facts_during_import_race(self):
        day_before = T("2026-05-04T09:00:00")
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", day_before,
                                       ttl_minutes=2880)
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        reimport_consent(self.box, granted_at=T("2026-05-06T00:00:00"))
        record = self.box.request_export(
            "EXP-RACE", "S-LIN", session["id"],
            [{"key": "AML-M2", "count": 5}], ["diagnosis"], T("2026-05-06T10:00:00"))
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertIn("同意撤回", record["reasons"])

    def test_expiry_race_import_does_not_restore_access(self):
        # 跨校课程到期收权后，冲突导入不能让该课程恢复能力
        self.box.sweep_expired(T("2026-05-16T00:00:00"))
        result = reimport_consent(self.box, course_id="C-CROSS")
        self.assertEqual(result["outcome"], CONSENT_CONFLICT)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-WANG", "TASK-CROSS-Q1", T("2026-05-16T09:00:00"))


class ConsentCorrectionTest(unittest.TestCase):
    """授权人员可追加更正记录并裁定冲突，但旧记录不可改写。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.now = T("2026-04-02T10:00:00")

    def test_privileged_correction_appends_without_rewriting(self):
        conflict = reimport_consent(self.box, case_id="CASE-T2D")["conflict"]
        before = dict(self.box.consents["CONS-AML-TEACH"])
        correction = self.box.correct_consent(
            "CONS-AML-TEACH", "D-ADMIN",
            "确认首次登记有效，后到病例包系误装，退回重发",
            self.now, resolves=conflict["id"])
        grant = self.box.consents["CONS-AML-TEACH"]
        # 旧记录关键字段一律未改写
        for field in ("case_id", "course_id", "granted_at", "scope_note",
                      "binding_hash", "source"):
            self.assertEqual(grant[field], before[field])
        self.assertIn(correction, grant["corrections"])
        self.assertEqual(conflict["status"], CONFLICT_RESOLVED)
        self.assertEqual(conflict["resolution"]["by"], "D-ADMIN")
        # 裁定不等于让后到内容生效：CASE-T2D 仍无有效同意
        self.assertEqual(self.box._active_consents("CASE-T2D", "C-LOCAL",
                                                   self.now), [])

    def test_unprivileged_actor_cannot_correct(self):
        with self.assertRaises(AuthorizationError):
            self.box.correct_consent("CONS-AML-TEACH", "T-CHEN", "越权更正",
                                     self.now)
        with self.assertRaises(AuthorizationError):
            self.box.correct_consent("CONS-AML-TEACH", "S-LIN", "越权更正",
                                     self.now)

    def test_resolving_unknown_conflict_is_rejected(self):
        with self.assertRaises(NotFound):
            self.box.correct_consent("CONS-AML-TEACH", "D-ADMIN", "裁定",
                                     self.now, resolves="CONFLICT-9999")


class ConsentTraceImpactTest(unittest.TestCase):
    """追溯解释首次来源、重放与冲突对课程、会话、已评分作业的影响。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.now = T("2026-04-01T09:00:00")

    def test_consent_trace_explains_source_replay_and_conflict_impact(self):
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.now)
        reimport_consent(self.box)  # 完全一致的重放
        # 另一个病例包误用同一编号；CASE-T2D 已有任务，影响面可指认
        self.box.add_snapshot("CASE-T2D", 1, [{"age": 55, "diagnosis": "T2D"}],
                              identity_fields=[], released_at=T("2026-03-01T00:00:00"))
        self.box.create_task("TASK-T2D-Q1", "C-LOCAL", "CASE-T2D",
                             ["age", "diagnosis"], "POL-K5", "ENV-SCANPY",
                             now=self.now)
        conflict = reimport_consent(self.box, case_id="CASE-T2D")["conflict"]

        info = self.box.consent_trace("CONS-AML-TEACH")
        self.assertEqual(info["首次登记"]["source"], "种子装载")
        self.assertEqual(info["首次登记"]["case_id"], "CASE-AML")
        self.assertEqual(info["重放次数"], 1)
        self.assertEqual(len(info["冲突"]), 1)
        entry = info["冲突"][0]
        self.assertEqual(entry["id"], conflict["id"])
        self.assertEqual(entry["changed_fields"], ["case_id"])
        self.assertEqual(entry["status"], CONFLICT_PENDING)
        # 后到内容若生效会波及 CASE-T2D 所在课程
        self.assertEqual(entry["影响"]["courses"], ["C-LOCAL"])
        # 已提交事实当前覆盖两门课程、进行中的会话与已评分作业
        committed = info["已提交影响"]
        self.assertEqual(set(committed["courses"]), {"C-LOCAL", "C-CROSS"})
        self.assertIn(session["id"], committed["sessions"])
        self.assertEqual(set(committed["graded_assignments"]),
                         {"AS-LIN-01", "AS-GAO-01"})

    def test_assignment_trace_links_consent_binding_and_conflicts(self):
        reimport_consent(self.box, case_id="CASE-T2D")
        consents = self.box.trace("AS-LIN-01")["课程授权"]["consents"]
        self.assertEqual(consents[0]["source"], "种子装载")
        self.assertEqual(len(consents[0]["binding_hash"]), 16)
        self.assertEqual(consents[0]["replay_count"], 0)
        self.assertEqual(len(consents[0]["conflicts"]), 1)


class ConsentSerializationTest(unittest.TestCase):
    """序列化恢复：绑定关系随快照往返，恢复后再次导入判定一致。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def _roundtrip(self, box):
        payload = json.loads(json.dumps(box.to_snapshot(), ensure_ascii=False))
        return Sandbox.from_snapshot(payload)

    def test_snapshot_roundtrip_preserves_ledgers_and_trace(self):
        restored = self._roundtrip(self.box)
        for ledger in (restored.cases, restored.snapshots, restored.consents,
                       restored.policies, restored.environment_versions,
                       restored.assignments):
            self.assertGreater(len(ledger), 0)
        original = self.box.consents["CONS-AML-TEACH"]
        recovered = restored.consents["CONS-AML-TEACH"]
        self.assertEqual(recovered["binding_hash"], original["binding_hash"])
        self.assertEqual(recovered["source"], "种子装载")
        self.assertEqual(restored.audit, self.box.audit)
        trace = restored.trace("AS-LIN-01")
        self.assertEqual(trace["课程授权"]["consents"][0]["consent_id"],
                         "CONS-AML-TEACH")

    def test_reimport_after_recovery_keeps_identity_boundary(self):
        # 恢复前制造一次重放与一次冲突
        reimport_consent(self.box)
        reimport_consent(self.box, case_id="CASE-T2D")
        restored = self._roundtrip(self.box)
        # 恢复后完全一致仍是重放，且重放次数连续
        result = reimport_consent(restored)
        self.assertEqual(result["outcome"], CONSENT_REPLAYED)
        self.assertEqual(restored.consents["CONS-AML-TEACH"]["replay_count"], 2)
        # 恢复后关键字段变化仍登记冲突，首次事实不丢
        result = reimport_consent(restored, course_id="C-LOCAL")
        self.assertEqual(result["outcome"], CONSENT_CONFLICT)
        self.assertIsNone(restored.consents["CONS-AML-TEACH"]["course_id"])
        self.assertEqual(len(restored.consent_conflicts), 2)
        # 恢复后已提交事实照常支撑切片签发
        session = restored.issue_slice("S-LIN", "TASK-LOCAL-Q1",
                                       T("2026-04-01T09:00:00"))
        self.assertEqual(session["status"], STATUS_SANDBOX)

    def test_recovery_from_legacy_data_without_binding(self):
        # 旧格式：同意记录缺少绑定摘要、来源等字段，恢复时按关键字段补齐
        payload = json.loads(json.dumps(self.box.to_snapshot(),
                                        ensure_ascii=False))
        for key in ("binding_hash", "source", "registered_at", "replay_count",
                    "replays", "corrections"):
            payload["consents"][0].pop(key, None)
        payload.pop("consent_conflicts", None)
        restored = Sandbox.from_snapshot(payload)
        result = reimport_consent(restored)
        self.assertEqual(result["outcome"], CONSENT_REPLAYED)
        result = reimport_consent(restored, case_id="CASE-T2D")
        self.assertEqual(result["outcome"], CONSENT_CONFLICT)
        self.assertEqual(restored.consents["CONS-AML-TEACH"]["case_id"],
                         "CASE-AML")


if __name__ == "__main__":
    unittest.main()
