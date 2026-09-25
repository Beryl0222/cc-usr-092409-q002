"""医学教学数据沙箱的领域契约测试。

覆盖需求中的全部治理承诺：
限时切片与脱敏、披露检查与小样本阻断、跨校到期收权、
勘误/升级前向影响、同意撤回即时效力、已评分作业指纹冻结、
教师复现与身份隔离、结论四要素溯源。
"""

import unittest
from datetime import datetime

from domain import (
    EXPORT_APPROVED,
    EXPORT_BLOCKED,
    STATUS_GRADED,
    STATUS_REVOKED,
    STATUS_SANDBOX,
    AuthorizationError,
    Sandbox,
    SandboxError,
)

SEED = "fixtures/seed.json"
T = lambda s: datetime.fromisoformat(s)  # noqa: E731


def build_boundary_box():
    """构造两门课、两个病例的最小沙箱，用于同意编号边界测试。"""
    box = Sandbox()
    box.add_course("C-A", "甲课程", T("2026-01-01T00:00:00"),
                   T("2026-12-31T00:00:00"))
    box.add_course("C-B", "乙课程", T("2026-01-01T00:00:00"),
                   T("2026-04-30T00:00:00"))
    box.add_case("CASE-X", "病例 X", courses=["C-A", "C-B"])
    box.add_case("CASE-Y", "病例 Y", courses=["C-B"])
    box.policies["P1"] = {"id": "P1", "version": 1, "k_threshold": 1,
                          "transforms": {}}
    box.add_environment("ENV", 1, {"python": "3.11.9"},
                        T("2026-01-01T00:00:00"))
    rows = [{"patient_id": "P-1", "v": 1}, {"patient_id": "P-2", "v": 2}]
    box.add_snapshot("CASE-X", 1, rows, ["patient_id"],
                     T("2026-01-02T00:00:00"))
    box.add_snapshot("CASE-Y", 1, rows, ["patient_id"],
                     T("2026-01-02T00:00:00"))
    box.enroll("S1", "C-A", STATUS_SANDBOX)
    box.enroll("S2", "C-B", STATUS_SANDBOX)
    box.assign_teacher("T1", "C-A")
    box.create_task("TASK-A", "C-A", "CASE-X", ["v"], "P1", "ENV",
                    T("2026-02-01T00:00:00"))
    box.create_task("TASK-B", "C-B", "CASE-X", ["v"], "P1", "ENV",
                    T("2026-02-01T00:00:00"))
    box.create_task("TASK-BY", "C-B", "CASE-Y", ["v"], "P1", "ENV",
                    T("2026-02-01T00:00:00"))
    return box


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


class ConsentBoundaryTest(unittest.TestCase):
    """同意编号首次落账后的标识边界：重放幂等、换字段即冲突且不覆盖。"""

    GRANTED = T("2026-01-10T00:00:00")
    NOW = T("2026-02-10T00:00:00")

    def setUp(self):
        self.box = build_boundary_box()

    def test_first_landing_binds_four_key_fields(self):
        grant = self.box.grant_consent(
            "CONS-X", "CASE-X", "C-A", self.GRANTED,
            "仅甲课程脱敏教学", "sha256:abcd",
            source="batch-import", import_batch="B001",
            imported_at=T("2026-01-11T08:00:00"))
        self.assertEqual(grant["binding"]["case_id"], "CASE-X")
        self.assertEqual(grant["binding"]["course_id"], "C-A")
        self.assertEqual(grant["binding"]["granted_at"], "2026-01-10T00:00:00")
        self.assertEqual(grant["binding"]["content_summary"], "sha256:abcd")
        self.assertEqual(len(grant["binding_fingerprint"]), 16)
        self.assertEqual(grant["source"], "batch-import")
        # 首次落账写审计
        self.assertTrue(any(a["action"] == "同意首次落账"
                            and a["consent_id"] == "CONS-X"
                            for a in self.box.audit))

    def test_same_id_reused_across_cases_keeps_first_and_conflicts(self):
        # 事故复现：两个病例包误用同一同意编号
        first = self.box.grant_consent(
            "CONS-DUP", "CASE-X", "C-A", self.GRANTED, "甲课程用", "sum-x")
        second = self.box.grant_consent(
            "CONS-DUP", "CASE-Y", "C-B", self.GRANTED, "乙课程用", "sum-y")
        # 返回的仍是首次记录，首次事实四要素原样保留
        self.assertIs(second, first)
        self.assertEqual(first["case_id"], "CASE-X")
        self.assertEqual(first["course_id"], "C-A")
        self.assertEqual(first["content_summary"], "sum-x")
        # 冲突账登记，后到内容未裁定前不获得能力
        conflict = self.box.consent_conflicts["CONS-DUP"]
        self.assertEqual(conflict["status"], "待裁定")
        self.assertIn("病例", self.box.audit[-1]["changed_fields"])
        # 甲课程（首次）继续有同意；乙课程的病例 Y 不被后到内容授权
        self.assertTrue(self.box._active_consents("CASE-X", "C-A", self.NOW))
        self.assertFalse(self.box._active_consents("CASE-Y", "C-B", self.NOW))
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S2", "TASK-BY", self.NOW)
        # 首次课程的切片照常签发——首门课程不再被静默夺走授权
        session = self.box.issue_slice("S1", "TASK-A", self.NOW)
        self.assertEqual(session["status"], STATUS_SANDBOX)

    def test_same_case_changed_scope_conflicts_without_capability(self):
        self.box.grant_consent(
            "CONS-S", "CASE-X", "C-A", self.GRANTED, "仅甲课程", "sum-1")
        # 同病例、把范围从 C-A 改成通用（None）
        self.box.grant_consent(
            "CONS-S", "CASE-X", None, self.GRANTED, "全部教学用途", "sum-1")
        conflict = self.box.consent_conflicts["CONS-S"]
        self.assertEqual(conflict["status"], "待裁定")
        self.assertEqual(
            self.box._binding_diff(conflict["first_binding"],
                                   conflict["attempts"][-1]["binding"]),
            ["课程范围", "范围说明"])
        # 首次范围 C-A 有效；乙课程不能借后到内容获得覆盖
        self.assertTrue(self.box._active_consents("CASE-X", "C-A", self.NOW))
        self.assertFalse(self.box._active_consents("CASE-X", "C-B", self.NOW))

    def test_changed_time_or_summary_conflicts(self):
        self.box.grant_consent(
            "CONS-T", "CASE-X", "C-A", self.GRANTED, "说明", "sum-1")
        self.box.grant_consent(
            "CONS-T", "CASE-X", "C-A", T("2026-01-20T00:00:00"), "说明", "sum-1")
        self.assertEqual(
            self.box.consent_conflicts["CONS-T"]["attempts"][-1]
            ["binding"]["granted_at"], "2026-01-20T00:00:00")
        self.box.grant_consent(
            "CONS-T", "CASE-X", "C-A", self.GRANTED, "说明", "sum-2")
        self.assertEqual(len(self.box.consent_conflicts["CONS-T"]["attempts"]), 2)
        # 授权时间仍是首次时间
        self.assertEqual(self.box.consents["CONS-T"]["granted_at"], self.GRANTED)

    def test_exact_replay_returns_original_without_new_grant(self):
        first = self.box.grant_consent(
            "CONS-R", "CASE-X", "C-A", self.GRANTED, "说明", "sum-r",
            import_batch="B1")
        again = self.box.grant_consent(
            "CONS-R", "CASE-X", "C-A", self.GRANTED, "说明", "sum-r",
            import_batch="B2")
        self.assertIs(again, first)
        self.assertEqual(len(self.box.consents), 1)
        self.assertEqual(len(first["replays"]), 1)
        self.assertEqual(first["replays"][0]["import_batch"], "B2")
        # 同批次重放（恢复后重导）不重复登记
        self.box.grant_consent(
            "CONS-R", "CASE-X", "C-A", self.GRANTED, "说明", "sum-r",
            import_batch="B2")
        self.assertEqual(len(first["replays"]), 1)
        self.assertTrue(any(a["action"] == "同意重复导入重放"
                            for a in self.box.audit))

    def test_conflict_does_not_overwrite_risk_flags_or_audit(self):
        # 首次授权下产生已评分作业并因撤回打上风险标记
        self.box.grant_consent("CONS-K", "CASE-X", "C-A", self.GRANTED, "s", "k")
        session = self.box.issue_slice("S1", "TASK-A", self.NOW)
        self.box.submit_assignment(
            "AS-1", "S1", "TASK-A", "结论", [{"step": "a", "tool": "python"}],
            self.NOW, slice_ids=[session["id"]])
        self.box.grade_assignment("T1", "AS-1", "A", "通过", self.NOW)
        audit_before = len(self.box.audit)
        # 后到的跨病例导入：不得改写授权、风险标记或既有审计
        self.box.grant_consent(
            "CONS-K", "CASE-Y", "C-B", self.GRANTED, "s", "k2")
        self.box.withdraw_consent("CONS-K", T("2026-03-01T00:00:00"))
        flags = {f["type"] for f in self.box.assignments["AS-1"]["risk_flags"]}
        self.assertIn("同意撤回", flags)
        # 撤回审计仅新增，不被覆盖
        self.assertGreater(len(self.box.audit), audit_before)


class ConsentRaceTest(unittest.TestCase):
    """撤回、课程到期、签发与导出按已提交事实的时间边界判定。"""

    def setUp(self):
        self.box = build_boundary_box()
        self.granted = T("2026-01-10T00:00:00")
        self.box.grant_consent(
            "CONS-W", "CASE-X", None, self.granted, "教学通用", "sum-w")
        self.t0 = T("2026-02-10T09:00:00")

    def test_slice_and_export_decide_on_committed_fact_at_instant(self):
        session = self.box.issue_slice("S1", "TASK-A", self.t0)
        # 与撤回同一时刻到达的导出：撤回事实已提交即阻断（边界 <=）
        withdraw_at = T("2026-02-10T10:00:00")
        record = self.box.request_export(
            "EXP-E", "S1", session["id"],
            [{"key": "g", "count": 2}], ["v"], withdraw_at)
        self.assertEqual(record["decision"], EXPORT_APPROVED)
        self.box.withdraw_consent("CONS-W", withdraw_at)
        self.assertEqual(session["status"], STATUS_REVOKED)
        # 撤回后同刻再签发/导出都按“无有效同意”处理
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S2", "TASK-B", withdraw_at)
        after = self.box.request_export(
            "EXP-L", "S1", session["id"],
            [{"key": "g", "count": 2}], ["v"], withdraw_at)
        self.assertEqual(after["decision"], EXPORT_BLOCKED)
        self.assertTrue(any("撤回" in r for r in after["reasons"]))

    def test_conflicting_import_cannot_resurrect_withdrawn_consent(self):
        # 撤回与一个“修正版”导入并发：未裁定的后到内容不能让同意复活
        self.box.withdraw_consent("CONS-W", self.t0)
        self.box.grant_consent(
            "CONS-W", "CASE-X", None, self.granted, "教学通用（新版）", "sum-w2",
            imported_at=self.t0)
        self.assertEqual(self.box.consents["CONS-W"]["withdrawn_at"], self.t0)
        self.assertEqual(
            self.box.consent_conflicts["CONS-W"]["status"], "待裁定")
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S1", "TASK-A", self.t0)

    def test_course_expiry_and_consent_both_gate_independently(self):
        # 乙课程 4 月底到期；到期时同意仍在也收权
        expired = T("2026-05-01T00:00:00")
        self.box.sweep_expired(expired)
        self.assertEqual(
            self.box._enrollment("S2", "C-B")["status"], STATUS_REVOKED)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S2", "TASK-B", expired)
        # 甲课程未到期且同意有效，不受影响
        self.assertEqual(
            self.box.issue_slice("S1", "TASK-A", expired)["status"],
            STATUS_SANDBOX)


class ConsentCorrectionTest(unittest.TestCase):
    """授权人员可追加更正，但旧记录不可改写；裁定让后到内容前向生效。"""

    GRANTED = T("2026-01-10T00:00:00")

    def setUp(self):
        self.box = build_boundary_box()
        self.first = self.box.grant_consent(
            "CONS-C", "CASE-X", "C-A", self.GRANTED, "仅甲课程", "sum-a")

    def test_officer_correction_is_append_only_and_forward_effective(self):
        before = T("2026-02-01T00:00:00")
        effective = T("2026-03-01T00:00:00")
        # 更正生效前，乙课程无覆盖
        self.assertFalse(self.box._active_consents("CASE-X", "C-B", before))
        corr = self.box.append_consent_correction(
            "CONS-C", "CASE-X", None, self.GRANTED, "扩为教学通用", "sum-b",
            effective, note="管理员补登记跨课程授权")
        self.assertTrue(corr["id"].startswith("CORR-"))
        # 旧记录字段原样保留
        self.assertEqual(self.first["course_id"], "C-A")
        self.assertEqual(self.first["scope_note"], "仅甲课程")
        self.assertEqual(self.first["content_summary"], "sum-a")
        # 更正只向前生效，不回溯
        self.assertFalse(self.box._active_consents("CASE-X", "C-B", before))
        self.assertTrue(self.box._active_consents("CASE-X", "C-B", effective))

    def test_uphold_resolution_keeps_first_fact(self):
        self.box.grant_consent(
            "CONS-C", "CASE-X", "C-B", self.GRANTED, "乙课程", "sum-b")
        conflict = self.box.resolve_consent_conflict(
            "CONS-C", "OFFICER-1", "uphold", T("2026-02-05T00:00:00"),
            note="编号误用，维持首次授权")
        self.assertEqual(conflict["status"], "维持首次授权")
        self.assertFalse(self.box._active_consents(
            "CASE-X", "C-B", T("2026-02-06T00:00:00")))
        with self.assertRaises(SandboxError):
            self.box.resolve_consent_conflict(
                "CONS-C", "OFFICER-1", "uphold", T("2026-02-06T00:00:00"))

    def test_correct_resolution_appends_correction_without_rewrite(self):
        self.box.grant_consent(
            "CONS-C", "CASE-X", None, self.GRANTED, "教学通用", "sum-b")
        decide_at = T("2026-02-05T00:00:00")
        conflict = self.box.resolve_consent_conflict(
            "CONS-C", "OFFICER-1", "correct", decide_at, note="确认跨课程")
        self.assertEqual(conflict["status"], "已更正")
        # 裁定时刻之前乙课程无覆盖（不回溯）；之后有覆盖
        self.assertFalse(self.box._active_consents(
            "CASE-X", "C-B", T("2026-02-04T00:00:00")))
        self.assertTrue(self.box._active_consents("CASE-X", "C-B", decide_at))
        # 首次事实仍未被改写
        self.assertEqual(self.first["course_id"], "C-A")
        correction = self.box.consent_corrections[-1]
        self.assertEqual(correction["source"], "conflict-resolution")
        self.assertEqual(correction["officer_id"], "OFFICER-1")


class ConsentRecoveryTest(unittest.TestCase):
    """种子装载兼容、序列化恢复后继续保持编号边界。"""

    GRANTED = T("2026-01-10T00:00:00")

    def test_legacy_seed_loads_without_content_summary(self):
        box = Sandbox.from_seed(SEED)
        grant = box.consents["CONS-AML-TEACH"]
        self.assertEqual(grant["content_summary"], "")
        self.assertEqual(len(grant["binding_fingerprint"]), 16)
        # 旧数据仍覆盖两门课程
        now = T("2026-04-01T09:00:00")
        self.assertTrue(box._active_consents("CASE-AML", "C-LOCAL", now))
        self.assertTrue(box._active_consents("CASE-AML", "C-CROSS", now))

    def test_roundtrip_preserves_conflicts_replays_and_corrections(self):
        box = build_boundary_box()
        box.grant_consent("CONS-V", "CASE-X", "C-A", self.GRANTED, "s", "sum-1",
                          import_batch="B1")
        box.grant_consent("CONS-V", "CASE-X", "C-A", self.GRANTED, "s", "sum-1",
                          import_batch="B2")  # 重放
        box.grant_consent("CONS-V", "CASE-Y", "C-B", self.GRANTED, "s", "sum-2")
        box.append_consent_correction(
            "CONS-V", "CASE-X", None, self.GRANTED, "通用", "sum-3",
            T("2026-03-01T00:00:00"))

        restored = Sandbox.from_state(box.to_state())
        grant = restored.consents["CONS-V"]
        self.assertEqual(grant["case_id"], "CASE-X")
        self.assertEqual(len(grant["replays"]), 1)
        self.assertEqual(restored.consent_conflicts["CONS-V"]["status"],
                         "待裁定")
        self.assertEqual(len(restored.consent_corrections), 1)
        self.assertTrue(restored._active_consents(
            "CASE-X", "C-B", T("2026-03-02T00:00:00")))
        self.assertFalse(restored._active_consents(
            "CASE-Y", "C-B", T("2026-03-02T00:00:00")))

    def test_reimport_after_recovery_keeps_boundary(self):
        box = build_boundary_box()
        box.grant_consent("CONS-V", "CASE-X", "C-A", self.GRANTED, "s", "sum-1",
                          import_batch="B1")
        restored = Sandbox.from_state(box.to_state())
        # 恢复后完全重放：返回原记录、不再落新账
        replay = restored.grant_consent(
            "CONS-V", "CASE-X", "C-A", self.GRANTED, "s", "sum-1",
            import_batch="B3")
        self.assertEqual(replay["binding_fingerprint"],
                         box.consents["CONS-V"]["binding_fingerprint"])
        self.assertEqual(len(restored.consents), 1)
        # 恢复后换字段导入：仍是冲突，首次事实不动
        restored.grant_consent(
            "CONS-V", "CASE-Y", "C-B", self.GRANTED, "s", "sum-9")
        self.assertEqual(restored.consents["CONS-V"]["case_id"], "CASE-X")
        self.assertEqual(
            restored.consent_conflicts["CONS-V"]["status"], "待裁定")

    def test_legacy_state_without_binding_is_backfilled(self):
        # 旧版转储没有 binding/content_summary：恢复时只读补齐
        box = build_boundary_box()
        box.grant_consent("CONS-OLD", "CASE-X", "C-A", self.GRANTED)
        state = box.to_state()
        state["consents"][-1].pop("binding")
        state["consents"][-1].pop("binding_fingerprint")
        restored = Sandbox.from_state(state)
        grant = restored.consents["CONS-OLD"]
        self.assertEqual(grant["binding"]["case_id"], "CASE-X")
        self.assertEqual(len(grant["binding_fingerprint"]), 16)


class ConsentTraceTest(unittest.TestCase):
    """追溯结果解释首次来源、重放、冲突及其影响面。"""

    GRANTED = T("2026-01-10T00:00:00")

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_trace_explains_first_source_replay_and_conflict(self):
        grant = self.box.grant_consent(
            "CONS-AML-TEACH", "CASE-AML", None,
            T("2026-02-25T00:00:00"),
            "覆盖两门口径内教学课程，仅限脱敏切片在限时沙箱内使用，禁止再识别",
            "", import_batch="REPLAY")
        self.assertIs(grant, self.box.consents["CONS-AML-TEACH"])
        # 换范围导入产生冲突
        self.box.grant_consent(
            "CONS-AML-TEACH", "CASE-T2D", "C-LOCAL",
            T("2026-02-25T00:00:00"), "误挂到对照病例", "sum-x")
        view = self.box.trace("AS-LIN-01")["课程授权"]["consents"][0]
        self.assertEqual(view["source"], "seed")
        self.assertTrue(any(r["import_batch"] == "REPLAY"
                            for r in view["replays"]))
        self.assertEqual(view["conflict"]["status"], "待裁定")
        self.assertIn("病例", view["conflict"]["changed_fields"])
        self.assertIn("课程范围", view["conflict"]["changed_fields"])

    def test_consent_report_covers_courses_sessions_and_graded_work(self):
        day_before = T("2026-05-04T09:00:00")
        self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", day_before,
                             ttl_minutes=2880)
        cross_session = self.box.issue_slice(
            "S-WANG", "TASK-CROSS-Q1", day_before, ttl_minutes=2880)
        self.box.withdraw_consent("CONS-AML-TEACH", T("2026-05-05T12:00:00"))
        report = self.box.consent_report("CONS-AML-TEACH")
        self.assertEqual(report["首次来源"]["source"], "seed")
        self.assertEqual(report["影响面"]["covered_cases"], ["CASE-AML"])
        self.assertEqual(set(report["影响面"]["covered_courses"]),
                         {"C-LOCAL", "C-CROSS"})
        graded = {a["assignment_id"]
                  for a in report["影响面"]["graded_assignments"]}
        self.assertEqual(graded, {"AS-LIN-01", "AS-GAO-01"})
        session_rows = report["影响面"]["sessions"]
        self.assertIn(cross_session["id"],
                      {s["slice_id"] for s in session_rows})
        self.assertTrue(all(s["status"] == STATUS_REVOKED
                            for s in session_rows))
        graded_flags = report["影响面"]["graded_assignments"][0]["risk_flags"]
        self.assertTrue(any(f["type"] == "同意撤回" for f in graded_flags))

    def test_report_explains_pending_conflict_grants_no_capability(self):
        self.box.grant_consent(
            "CONS-AML-TEACH", "CASE-T2D", "C-LOCAL",
            T("2026-02-25T00:00:00"), "试图挂到 T2D 病例", "sum-x")
        report = self.box.consent_report("CONS-AML-TEACH")
        self.assertEqual(report["冲突"]["status"], "待裁定")
        self.assertTrue(any("CASE-T2D" in line
                            for line in report["冲突"]["后到内容能力"]))
        self.assertNotIn("CASE-T2D", report["影响面"]["covered_cases"])


if __name__ == "__main__":
    unittest.main()
