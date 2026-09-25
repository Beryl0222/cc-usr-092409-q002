"""医学教学数据沙箱的领域规则。

六类业务记录分账管理，互不混存，彼此只通过不可变版本引用：

- 课程案例 ``cases``：教学目标、病例与课程的多对多关系；
- 临床数据快照 ``snapshots``：病例数据的版本化快照，勘误以“新版本”发布，旧版永不改写；
- 数据使用同意 ``consents``：按病例授权（可限定课程），可撤回，撤回即时生效；
- 脱敏策略 ``policies``：字段变换规则与小样本（k 匿名）阈值；
- 分析环境 ``environments``：工具镜像的版本与摘要；
- 作业产物 ``assignments``：学生结论、导出申请、评分时冻结的环境指纹与复核记录。

限时切片（``sessions``）与披露决策（``exports``）属于运行记录，不并入上述六账。

关键规则：

1. 学生只能在限时沙箱会话中取得与任务教学目标匹配的脱敏数据切片，身份字段永不下发；
2. 导出先过披露检查：会话有效、课程授权有效、同意未撤回、不含身份字段、每组样本数达到 k 阈值；
3. 教师可以按冻结指纹复现实验，但教师身份不具备患者身份读取能力，且只能访问任课课程；
4. 跨校课程到期后，注册关系与沙箱会话自动收回；
5. 病例勘误、工具升级只影响之后创建的新任务；已评分作业保留当时环境指纹，
   另以风险标记标出后续变化；同意撤回则即时阻断相关导出并撤回会话；
6. 任一结论都可溯源到：数据范围、处理步骤、课程授权、教师复核四部分。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

SERVICE_ID = "medical-sandbox"

# 与 fixtures/domain.json 保持一致的状态词汇
STATUS_PENDING = "待授权"
STATUS_SANDBOX = "沙箱运行"
STATUS_REVIEW = "待披露检查"
STATUS_GRADED = "已评分"
STATUS_REVOKED = "已撤权"
EXPORT_APPROVED = "通过"
EXPORT_BLOCKED = "阻断"
ASSIGNMENT_OPEN = "进行中"
# 同意导入的冲突状态：待裁定 / 已维持首次事实 / 后到内容经更正生效
CONFLICT_PENDING = "待裁定"
CONFLICT_UPHELD = "维持首次授权"
CONFLICT_CORRECTED = "已更正"
# 授权时间语义下的“不确定”（旧数据缺字段或时间不明），参与指纹但不臆造
TIME_UNKNOWN = "未知时间"


class SandboxError(Exception):
    """沙箱规则违例的基类。"""


class NotFound(SandboxError):
    """引用了不存在的记录。"""


class AuthorizationError(SandboxError):
    """身份不具备所需能力，或授权已失效。"""


def parse_time(value: str) -> datetime:
    """解析夹具中的 ISO 时间。"""
    return datetime.fromisoformat(value)


def canon(value: Any) -> str:
    """供摘要计算的规范化 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any, length: int = 16) -> str:
    return hashlib.sha256(canon(value).encode("utf-8")).hexdigest()[:length]


def _apply_transform(value: Any, rule: str) -> Any:
    if rule == "drop":
        return None
    if rule.startswith("generalize:"):
        width = int(rule.split(":", 1)[1])
        if isinstance(value, (int, float)):
            lower = int(value) // width * width
            return f"{lower}-{lower + width - 1}"
    return value


class Sandbox:
    """保存六类分账记录并执行全部治理规则。"""

    def __init__(self) -> None:
        self.actors: dict[str, dict] = {}
        self.courses: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        # 快照按 (病例, 版本) 存放，任何版本一经发布不可改写
        self.snapshots: dict[tuple[str, int], dict] = {}
        # 数据使用同意：键为同意编号，编号一经首次落账即绑定病例、课程范围、
        # 授权时间与内容摘要（binding），此后不可改写
        self.consents: dict[str, dict] = {}
        # 同意编号冲突账：同编号导入但关键字段变化时，只在此追加，绝不覆盖授权
        self.consent_conflicts: dict[str, dict] = {}
        # 授权人员对授权事实的更正记录：只追加，旧记录保持原样
        self.consent_corrections: list[dict] = []
        self.policies: dict[str, dict] = {}
        # 分析环境按 (镜像, 版本) 存放；任务创建时钉住当时版本，升级不回溯
        self.environment_versions: dict[tuple[str, int], dict] = {}
        self.tasks: dict[str, dict] = {}
        self.assignments: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.exports: dict[str, dict] = {}
        self.enrollments: list[dict] = []
        self.teacher_courses: list[dict] = []
        self.audit: list[dict] = []
        self._seq = 0
        self._correction_seq = 0

    # ---- 装载 -----------------------------------------------------------

    @classmethod
    def from_seed(cls, path: str | Path) -> "Sandbox":
        """从种子夹具装载一个可联调的沙箱。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        box = cls()
        for actor in data.get("actors", []):
            box.actors[actor["id"]] = dict(actor)
        for course in data.get("courses", []):
            box.add_course(
                course["id"], course["name"],
                starts_at=parse_time(course["starts_at"]),
                ends_at=parse_time(course["ends_at"]),
                institutions=course.get("institutions"),
                cross_institutional=course.get("cross_institutional", False),
            )
        for case in data.get("cases", []):
            box.add_case(case["id"], case["title"],
                         description=case.get("description", ""),
                         courses=case.get("courses"))
        for policy in data.get("policies", []):
            box.policies[policy["id"]] = dict(policy)
        for env in data.get("environments", []):
            box.add_environment(
                env["id"], env["version"], env["tools"],
                released_at=parse_time(env["released_at"]), note=env.get("note", ""),
            )
        for snapshot in data.get("snapshots", []):
            box.add_snapshot(
                snapshot["case_id"], snapshot["version"], snapshot["rows"],
                identity_fields=snapshot.get("identity_fields", []),
                released_at=parse_time(snapshot["released_at"]),
                replaces=snapshot.get("replaces"),
                erratum_note=snapshot.get("erratum_note", ""),
            )
        for grant in data.get("consents", []):
            box.grant_consent(
                grant["id"], grant["case_id"],
                course_id=grant.get("course_id"),
                granted_at=parse_time(grant["granted_at"]),
                scope_note=grant.get("scope_note", ""),
                content_summary=grant.get("content_summary", ""),
                source=grant.get("source", "seed"),
                import_batch=grant.get("import_batch"),
                imported_at=parse_time(grant["imported_at"])
                if grant.get("imported_at") else None,
            )
        for correction in data.get("consent_corrections", []):
            box._load_correction(correction)
        for conflict in data.get("consent_conflicts", []):
            box._load_conflict(conflict)
        for enrollment in data.get("enrollments", []):
            box.enroll(enrollment["student_id"], enrollment["course_id"],
                       status=enrollment.get("status"))
        for link in data.get("teachers", []):
            box.assign_teacher(link["teacher_id"], link["course_id"])
        for task in data.get("tasks", []):
            box.create_task(
                task["id"], task["course_id"], task["case_id"],
                task["objective_fields"], task["policy_id"], task["environment_id"],
                now=parse_time(task["created_at"]),
                snapshot_version=task.get("snapshot_version"),
            )
        for assignment in data.get("assignments", []):
            box._load_assignment(assignment)
        box._reconcile_flags()
        return box

    def _load_assignment(self, data: dict) -> None:
        task = self.tasks[data["task_id"]]
        recipe = data.get("recipe", [])
        assignment = {
            "id": data["id"],
            "task_id": data["task_id"],
            "student_id": data["student_id"],
            "conclusion": data["conclusion"],
            "recipe": recipe,
            "result_hash": data.get("result_hash"),
            "status": data["status"],
            "submitted_at": parse_time(data["submitted_at"]),
            "graded_at": parse_time(data["graded_at"]) if data.get("graded_at") else None,
            "grade": data.get("grade"),
            "fingerprint": data.get("fingerprint"),
            "reviews": data.get("reviews", []),
            "risk_flags": data.get("risk_flags", []),
            "slice_ids": data.get("slice_ids", []),
            "export_ids": data.get("export_ids", []),
        }
        # 已评分作业：指纹与结果摘要从各版本账重算，保证“当时环境”可独立验证
        if assignment["status"] == STATUS_GRADED:
            if assignment["fingerprint"] is None:
                assignment["fingerprint"] = self.environment_fingerprint(task, assignment)
            if not assignment["result_hash"]:
                assignment["result_hash"] = digest(
                    {"rows": self._project_rows(task), "recipe": recipe})
        self.assignments[assignment["id"]] = assignment

    def _reconcile_flags(self) -> None:
        """装载后对账：给已评分作业补上评分之后发生的勘误、工具升级、同意撤回标记。"""
        for assignment in self.assignments.values():
            if assignment["status"] != STATUS_GRADED:
                continue
            task = self.tasks[assignment["task_id"]]
            graded_at = assignment["graded_at"]
            fp = assignment["fingerprint"]
            newer_snapshots = sorted(
                (snap for (cid, ver), snap in self.snapshots.items()
                 if cid == task["case_id"] and ver > fp["snapshot"]["version"]
                 and snap["released_at"] > graded_at),
                key=lambda snap: snap["version"])
            for snap in newer_snapshots:
                self._flag(assignment, {
                    "type": "病例勘误",
                    "at": snap["released_at"].isoformat(),
                    "detail": snap["erratum_note"]
                    or f"病例已发布 v{snap['version']}，结论基于 v{fp['snapshot']['version']}",
                    "current_version": snap["version"],
                })
            newer_envs = sorted(
                (rec for (eid, ver), rec in self.environment_versions.items()
                 if eid == task["environment_id"] and ver > fp["environment"]["version"]
                 and rec["released_at"] > graded_at),
                key=lambda rec: rec["version"])
            for rec in newer_envs:
                self._flag(assignment, {
                    "type": "工具升级",
                    "at": rec["released_at"].isoformat(),
                    "detail": rec["note"] or f"分析环境已升级到 v{rec['version']}",
                    "current": f"{rec['id']}:v{rec['version']}",
                })
            for grant in self.consents.values():
                covers = [
                    (b["case_id"], b["course_id"])
                    for b in self._effective_bindings(
                        grant, datetime.max if grant["withdrawn_at"] is None
                        else grant["withdrawn_at"])
                ]
                if not any(case_id == task["case_id"]
                           and (course_id is None or course_id == task["course_id"])
                           for case_id, course_id in covers):
                    continue
                if grant["withdrawn_at"]:
                    self._flag(assignment, {
                        "type": "同意撤回",
                        "at": grant["withdrawn_at"].isoformat(),
                        "detail": f"授权 {grant['id']} 已撤回，结论所依据的授权不再有效",
                        "consent_id": grant["id"],
                    })

    # ---- 基础登记 -------------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def add_course(self, course_id: str, name: str, starts_at: datetime,
                   ends_at: datetime, institutions: Optional[list[str]] = None,
                   cross_institutional: bool = False) -> None:
        self.courses[course_id] = {
            "id": course_id,
            "name": name,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "institutions": institutions or [],
            "cross_institutional": cross_institutional or len(institutions or []) > 1,
        }

    def add_case(self, case_id: str, title: str, description: str = "",
                 courses: Optional[list[str]] = None) -> None:
        self.cases[case_id] = {
            "id": case_id,
            "title": title,
            "description": description,
            "courses": list(courses or []),
        }

    def add_snapshot(self, case_id: str, version: int, rows: list[dict],
                     identity_fields: list[str], released_at: datetime,
                     replaces: Optional[int] = None,
                     erratum_note: str = "") -> dict:
        if case_id not in self.cases:
            raise NotFound(f"未知病例：{case_id}")
        record = {
            "case_id": case_id,
            "version": version,
            "rows": rows,
            "identity_fields": identity_fields,
            "released_at": released_at,
            "replaces": replaces,
            "erratum_note": erratum_note,
            "content_hash": digest(rows),
        }
        self.snapshots[(case_id, version)] = record
        # 新版本发布：给引用旧版本的已评分作业追加风险标记
        if replaces is not None:
            for assignment in self.assignments.values():
                fp = assignment.get("fingerprint")
                if (fp and fp["snapshot"]["case_id"] == case_id
                        and fp["snapshot"]["version"] == replaces):
                    self._flag(assignment, {
                        "type": "病例勘误",
                        "at": released_at.isoformat(),
                        "detail": erratum_note or f"病例已发布 v{version}，结论基于 v{replaces}",
                        "current_version": version,
                    })
        return record

    def add_environment(self, env_id: str, version: int, tools: dict[str, str],
                        released_at: datetime, note: str = "") -> dict:
        record = {
            "id": env_id,
            "version": version,
            "tools": dict(tools),
            "released_at": released_at,
            "digest": digest(tools),
            "note": note,
        }
        self.environment_versions[(env_id, version)] = record
        # 升级：给引用旧版本的已评分作业追加风险标记（前向影响、不冻结改写）
        prior = [v for (eid, v), rec in self.environment_versions.items()
                 if eid == env_id and v < version]
        if prior:
            old_version = max(prior)
            for assignment in self.assignments.values():
                fp = assignment.get("fingerprint")
                if (fp and fp["environment"]["id"] == env_id
                        and fp["environment"]["version"] == old_version):
                    self._flag(assignment, {
                        "type": "工具升级",
                        "at": released_at.isoformat(),
                        "detail": note or f"分析环境已升级到 v{version}",
                        "current": f"{env_id}:v{version}",
                    })
        return record

    def _environment(self, env_id: str, version: int) -> dict:
        record = self.environment_versions.get((env_id, version))
        if record is None:
            raise NotFound(f"分析环境 {env_id}:v{version} 不存在")
        return record

    # ---- 同意登记：编号边界、重放、冲突 ----------------------------------

    @staticmethod
    def _consent_binding(case_id: str, course_id: Optional[str],
                         granted_at: datetime, scope_note: str,
                         content_summary: str) -> dict:
        """同意编号首次落账时绑定的关键字段；任一字段变化即构成冲突。"""
        return {
            "case_id": case_id,
            "course_id": course_id,  # None 表示覆盖所有教学用途
            "granted_at": granted_at.isoformat() if granted_at else TIME_UNKNOWN,
            "scope_note": scope_note or "",
            "content_summary": content_summary or "",
        }

    @staticmethod
    def _binding_fingerprint(binding: dict) -> str:
        return digest(binding)

    @classmethod
    def _consent_fingerprint(cls, case_id: str, course_id: Optional[str],
                             granted_at: datetime, scope_note: str,
                             content_summary: str) -> tuple[dict, str]:
        binding = cls._consent_binding(
            case_id, course_id, granted_at, scope_note, content_summary)
        return binding, cls._binding_fingerprint(binding)

    def grant_consent(self, consent_id: str, case_id: str,
                      course_id: Optional[str], granted_at: datetime,
                      scope_note: str = "", content_summary: str = "",
                      *, source: str = "import",
                      import_batch: Optional[str] = None,
                      imported_at: Optional[datetime] = None) -> dict:
        """登记一份数据使用同意（批量导入的落账口）。

        编号首次落账后即绑定病例、课程范围、授权时间与内容摘要四要素：

        - 四要素完全一致的重复导入：视为重放，原样返回首次记录，不产生新授权；
        - 任一关键字段变化：保留首次事实不动，在冲突账登记一笔“待裁定”，
          后到内容不获得任何能力（不覆盖授权、风险标记或审计）。

        旧数据（无内容摘要）按空摘要参与绑定，保证与既有种子兼容。
        """
        if case_id not in self.cases:
            raise NotFound(f"未知病例：{case_id}")
        if course_id is not None and course_id not in self.courses:
            raise NotFound(f"未知课程：{course_id}")
        binding, fp = self._consent_fingerprint(
            case_id, course_id, granted_at, scope_note, content_summary)
        arrived_at = imported_at or granted_at

        existing = self.consents.get(consent_id)
        if existing is None:
            grant = {
                "id": consent_id,
                "case_id": case_id,
                "course_id": course_id,
                "granted_at": granted_at,
                "withdrawn_at": None,
                "scope_note": scope_note or "",
                "content_summary": content_summary or "",
                "binding": binding,
                "binding_fingerprint": fp,
                "source": source,
                "first_import_batch": import_batch,
                "first_imported_at": arrived_at,
                "replays": [],
            }
            self.consents[consent_id] = grant
            self.audit.append({
                "at": arrived_at.isoformat() if arrived_at else None,
                "action": "同意首次落账", "consent_id": consent_id,
                "case_id": case_id, "course_id": course_id,
                "binding_fingerprint": fp, "source": source,
                "import_batch": import_batch,
            })
            return grant

        if existing["binding_fingerprint"] == fp:
            # 完全一致的重复导入：幂等重放，返回原记录，不覆盖任何事实
            replay = {
                "at": arrived_at.isoformat() if arrived_at else None,
                "source": source,
                "import_batch": import_batch,
            }
            # 同批次的重复装载（例如恢复后重放种子）不重复计数
            if not any(r["import_batch"] == import_batch and import_batch is not None
                       for r in existing["replays"]):
                existing["replays"].append(replay)
                self.audit.append({
                    "at": replay["at"], "action": "同意重复导入重放",
                    "consent_id": consent_id, "import_batch": import_batch,
                    "binding_fingerprint": fp, "result": "返回首次记录",
                })
            return existing

        # 关键字段变化：绝不覆盖，登记冲突（同编号、同入向指纹只登记一次）
        return self._register_conflict(
            existing, binding, fp, arrived_at=arrived_at, source=source,
            import_batch=import_batch)

    def _register_conflict(self, existing: dict, incoming_binding: dict,
                           incoming_fp: str, *, arrived_at: Optional[datetime],
                           source: str, import_batch: Optional[str]) -> dict:
        consent_id = existing["id"]
        conflict = self.consent_conflicts.get(consent_id)
        attempt = {
            "at": arrived_at.isoformat() if arrived_at else None,
            "source": source,
            "import_batch": import_batch,
            "binding": incoming_binding,
            "binding_fingerprint": incoming_fp,
        }
        if conflict is None:
            conflict = {
                "consent_id": consent_id,
                "status": CONFLICT_PENDING,
                "first_binding": existing["binding"],
                "first_binding_fingerprint": existing["binding_fingerprint"],
                "first_source": existing["source"],
                "first_imported_at": (existing["first_imported_at"].isoformat()
                                      if existing["first_imported_at"] else None),
                "attempts": [],
                "resolution": None,
            }
            self.consent_conflicts[consent_id] = conflict
        if not any(a["binding_fingerprint"] == incoming_fp
                   and a["import_batch"] == import_batch
                   for a in conflict["attempts"]):
            conflict["attempts"].append(attempt)
        self.audit.append({
            "at": attempt["at"], "action": "同意编号冲突",
            "consent_id": consent_id, "status": CONFLICT_PENDING,
            "first_binding_fingerprint": existing["binding_fingerprint"],
            "incoming_binding_fingerprint": incoming_fp,
            "changed_fields": self._binding_diff(existing["binding"],
                                                 incoming_binding),
            "import_batch": import_batch,
            "result": "保留首次事实，后到内容不授予能力",
        })
        # 始终返回首次记录：调用方拿到的授权事实不变
        return existing

    @staticmethod
    def _binding_diff(first: dict, incoming: dict) -> list[str]:
        labels = {
            "case_id": "病例", "course_id": "课程范围",
            "granted_at": "授权时间", "scope_note": "范围说明",
            "content_summary": "内容摘要",
        }
        return [labels[k] for k in
                ("case_id", "course_id", "granted_at", "scope_note",
                 "content_summary")
                if first.get(k) != incoming.get(k)]

    def _load_conflict(self, data: dict) -> None:
        """从序列化状态恢复冲突账，不重放导入判定。"""
        self.consent_conflicts[data["consent_id"]] = {
            "consent_id": data["consent_id"],
            "status": data.get("status", CONFLICT_PENDING),
            "first_binding": data["first_binding"],
            "first_binding_fingerprint": data["first_binding_fingerprint"],
            "first_source": data.get("first_source", "import"),
            "first_imported_at": data.get("first_imported_at"),
            "attempts": list(data.get("attempts", [])),
            "resolution": data.get("resolution"),
        }

    def resolve_consent_conflict(self, consent_id: str, officer_id: str,
                                 decision: str, now: datetime,
                                 note: str = "") -> dict:
        """隐私审核员裁定编号冲突。

        - ``uphold``（默认）：维持首次授权事实，后到内容继续不获得能力；
        - ``correct``：以“追加更正记录”的方式让后到内容生效，旧记录原样保留，
          更正自裁定时刻起生效，绝不回溯改写授权时间与历史能力判定。
        """
        conflict = self.consent_conflicts.get(consent_id)
        if conflict is None:
            raise NotFound(f"同意编号 {consent_id} 无待裁定冲突")
        if conflict["status"] != CONFLICT_PENDING:
            raise SandboxError(f"冲突已裁定：{conflict['status']}，不能重复裁定")
        if decision not in ("uphold", "correct"):
            raise SandboxError("裁定只能是 uphold（维持首次）或 correct（追加更正）")

        chosen = conflict["attempts"][-1]["binding"]
        resolution = {
            "officer_id": officer_id,
            "at": now.isoformat(),
            "decision": decision,
            "chosen_binding_fingerprint": (
                conflict["attempts"][-1]["binding_fingerprint"]
                if decision == "correct"
                else conflict["first_binding_fingerprint"]),
            "note": note,
        }
        conflict["status"] = (CONFLICT_UPHELD if decision == "uphold"
                              else CONFLICT_CORRECTED)
        conflict["resolution"] = resolution
        self.audit.append({
            "at": now.isoformat(),
            "action": "同意冲突裁定", "consent_id": consent_id,
            "decision": decision, "officer_id": officer_id, "note": note,
        })
        if decision == "correct":
            self._append_correction(
                consent_id, chosen["case_id"], chosen["course_id"],
                now, chosen["scope_note"], chosen["content_summary"],
                effective_at=now, source="conflict-resolution",
                officer_id=officer_id, note=note)
        return conflict

    def append_consent_correction(self, consent_id: str, case_id: str,
                                  course_id: Optional[str], authorized_at: datetime,
                                  scope_note: str, content_summary: str,
                                  now: datetime, *, note: str = "") -> dict:
        """授权人员追加更正记录。旧记录保持原样，更正自指定时刻起生效。"""
        if consent_id not in self.consents:
            raise NotFound(f"未知同意记录：{consent_id}")
        return self._append_correction(
            consent_id, case_id, course_id, authorized_at, scope_note,
            content_summary, effective_at=now, source="officer-amendment",
            note=note)

    def _append_correction(self, consent_id: str, case_id: str,
                           course_id: Optional[str], authorized_at: datetime,
                           scope_note: str, content_summary: str,
                           *, effective_at: datetime, source: str,
                           officer_id: Optional[str] = None,
                           note: str = "") -> dict:
        if case_id not in self.cases:
            raise NotFound(f"未知病例：{case_id}")
        if course_id is not None and course_id not in self.courses:
            raise NotFound(f"未知课程：{course_id}")
        binding, fp = self._consent_fingerprint(
            case_id, course_id, authorized_at, scope_note, content_summary)
        self._correction_seq += 1
        record = {
            "id": f"CORR-{self._correction_seq:04d}",
            "consent_id": consent_id,
            "binding": binding,
            "binding_fingerprint": fp,
            "case_id": case_id,
            "course_id": course_id,
            "authorized_at": authorized_at.isoformat() if authorized_at else None,
            "effective_at": effective_at.isoformat(),
            "source": source,
            "officer_id": officer_id,
            "note": note,
        }
        self.consent_corrections.append(record)
        self.audit.append({
            "at": effective_at.isoformat(), "action": "同意更正追加",
            "consent_id": consent_id, "correction_id": record["id"],
            "binding_fingerprint": fp, "source": source,
            "effective_at": record["effective_at"],
        })
        return record

    def _load_correction(self, data: dict) -> None:
        """从序列化状态恢复更正记录，保持编号与时间不变。"""
        record = {
            "id": data["id"],
            "consent_id": data["consent_id"],
            "binding": data["binding"],
            "binding_fingerprint": data["binding_fingerprint"],
            "case_id": data["case_id"],
            "course_id": data["course_id"],
            "authorized_at": data.get("authorized_at"),
            "effective_at": data["effective_at"],
            "source": data.get("source", "officer-amendment"),
            "officer_id": data.get("officer_id"),
            "note": data.get("note", ""),
        }
        self.consent_corrections.append(record)
        suffix = int(record["id"].rsplit("-", 1)[1])
        self._correction_seq = max(self._correction_seq, suffix)

    def _corrections_for(self, consent_id: str, now: datetime) -> list[dict]:
        return [c for c in self.consent_corrections
                if c["consent_id"] == consent_id
                and parse_time(c["effective_at"]) <= now]

    def _effective_bindings(self, grant: dict, now: datetime) -> list[dict]:
        """一份同意在 ``now`` 时点承认的全部绑定：首次事实 + 已生效更正。"""
        candidates = [{
            "case_id": grant["case_id"],
            "course_id": grant["course_id"],
            "granted_at": grant["granted_at"],
            "withdrawn_at": grant["withdrawn_at"],
            "via": "首次落账",
        }]
        for correction in self._corrections_for(grant["id"], now):
            candidates.append({
                "case_id": correction["case_id"],
                "course_id": correction["course_id"],
                "granted_at": parse_time(correction["authorized_at"])
                if correction["authorized_at"] else None,
                "withdrawn_at": grant["withdrawn_at"],
                "via": f"更正 {correction['id']}",
            })
        return candidates

    def _grant_covers(self, grant: dict, case_id: str, course_id: str,
                      now: datetime) -> bool:
        """按已提交事实判定一份同意（含追加更正）是否覆盖病例+课程。

        冲突未裁定前，后到内容既不改变首次事实、也不产生覆盖；更正只在其
        生效时刻之后按新绑定覆盖，绝不回溯。
        """
        for cand in self._effective_bindings(grant, now):
            if cand["case_id"] != case_id:
                continue
            if cand["course_id"] is not None and cand["course_id"] != course_id:
                continue
            if cand["granted_at"] is not None and cand["granted_at"] > now:
                continue
            if cand["withdrawn_at"] is not None and cand["withdrawn_at"] <= now:
                continue
            return True
        return False

    def enroll(self, student_id: str, course_id: str,
               status: str = STATUS_PENDING) -> dict:
        record = {"student_id": student_id, "course_id": course_id, "status": status}
        self.enrollments.append(record)
        return record

    def assign_teacher(self, teacher_id: str, course_id: str) -> None:
        self.teacher_courses.append(
            {"teacher_id": teacher_id, "course_id": course_id})

    def create_task(self, task_id: str, course_id: str, case_id: str,
                    objective_fields: list[str], policy_id: str,
                    environment_id: str, now: datetime,
                    snapshot_version: Optional[int] = None) -> dict:
        if course_id not in self.courses:
            raise NotFound(f"未知课程：{course_id}")
        if case_id not in self.cases:
            raise NotFound(f"未知病例：{case_id}")
        if snapshot_version is None:
            # 任务创建时钉住“当时最新”的快照版本；之后勘误不改变本任务
            available = [v for (cid, v), snap in self.snapshots.items()
                         if cid == case_id and snap["released_at"] <= now]
            if not available:
                raise SandboxError(f"病例 {case_id} 在 {now} 尚无已发布快照")
            snapshot_version = max(available)
        if (case_id, snapshot_version) not in self.snapshots:
            raise NotFound(f"快照 {case_id}:v{snapshot_version} 不存在")
        # 任务创建时钉住“当时最新”的分析环境版本；之后工具升级不回溯本任务
        env_versions = [v for (eid, v), rec in self.environment_versions.items()
                        if eid == environment_id and rec["released_at"] <= now]
        if not env_versions:
            raise SandboxError(f"分析环境 {environment_id} 在 {now} 尚无已发布版本")
        environment_version = max(env_versions)
        if course_id not in self.cases[case_id]["courses"]:
            self.cases[case_id]["courses"].append(course_id)
        task = {
            "id": task_id,
            "course_id": course_id,
            "case_id": case_id,
            "objective_fields": list(objective_fields),
            "snapshot_version": snapshot_version,
            "policy_id": policy_id,
            "environment_id": environment_id,
            "environment_version": environment_version,
            "created_at": now,
        }
        self.tasks[task_id] = task
        return task

    # ---- 授权判定 -------------------------------------------------------

    def _enrollment(self, student_id: str, course_id: str) -> Optional[dict]:
        for record in self.enrollments:
            if record["student_id"] == student_id and record["course_id"] == course_id:
                return record
        return None

    def _is_teacher(self, teacher_id: str, course_id: str) -> bool:
        return any(link["teacher_id"] == teacher_id and link["course_id"] == course_id
                   for link in self.teacher_courses)

    def _active_consents(self, case_id: str, course_id: str,
                         now: datetime) -> list[dict]:
        """返回当下覆盖该病例+课程且未撤回的同意。

        只承认已提交事实：首次落账记录与其已生效更正；待裁定冲突中的后到
        内容不参与判定（见 :meth:`grant_consent` 与 :meth:`_grant_covers`）。
        """
        return [grant for grant in self.consents.values()
                if self._grant_covers(grant, case_id, course_id, now)]

    def _course_open(self, course_id: str, now: datetime) -> bool:
        course = self.courses[course_id]
        return course["starts_at"] <= now < course["ends_at"]

    def sweep_expired(self, now: datetime) -> list[str]:
        """到期收权：课程窗口结束后，注册关系与进行中的沙箱会话自动收回。

        幂等；返回本次新收回的注册记录标识。跨校课程同样适用，且到期后无宽限。
        """
        revoked = []
        for enrollment in self.enrollments:
            if enrollment["status"] == STATUS_REVOKED:
                continue
            if not self._course_open(enrollment["course_id"], now):
                enrollment["status"] = STATUS_REVOKED
                revoked.append(f"{enrollment['student_id']}@{enrollment['course_id']}")
                for session in self.sessions.values():
                    task = self.tasks[session["task_id"]]
                    if (session["student_id"] == enrollment["student_id"]
                            and task["course_id"] == enrollment["course_id"]
                            and session["status"] == STATUS_SANDBOX):
                        self._revoke_session(session, "课程到期", now)
        return revoked

    # ---- 限时数据切片 ---------------------------------------------------

    def _project_rows(self, task: dict) -> list[dict]:
        """按教学目标字段与脱敏策略投影数据；身份字段一律剔除。"""
        snapshot = self.snapshots[(task["case_id"], task["snapshot_version"])]
        policy = self.policies[task["policy_id"]]
        identity = set(snapshot["identity_fields"])
        allowed = [f for f in task["objective_fields"] if f not in identity]
        projected = []
        for row in snapshot["rows"]:
            item = {}
            for field in allowed:
                rule = policy.get("transforms", {}).get(field)
                value = row.get(field)
                if rule is not None:
                    value = _apply_transform(value, rule)
                if value is not None:
                    item[field] = value
            projected.append(item)
        return projected

    def issue_slice(self, student_id: str, task_id: str, now: datetime,
                    ttl_minutes: int = 240) -> dict:
        """在限时沙箱中签发与任务教学目标匹配的数据切片。"""
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFound(f"未知任务：{task_id}")
        course_id = task["course_id"]
        enrollment = self._enrollment(student_id, course_id)
        if enrollment is None:
            raise AuthorizationError(f"学生 {student_id} 未注册课程 {course_id}")
        self.sweep_expired(now)
        if enrollment["status"] == STATUS_REVOKED or not self._course_open(course_id, now):
            raise AuthorizationError(f"课程 {course_id} 授权已到期收回")
        if not self._active_consents(task["case_id"], course_id, now):
            raise AuthorizationError(f"病例 {task['case_id']} 缺少有效数据使用同意")

        slice_id = self._new_id("SLICE")
        session = {
            "id": slice_id,
            "task_id": task_id,
            "student_id": student_id,
            "rows": self._project_rows(task),
            "issued_at": now,
            "expires_at": now + timedelta(minutes=ttl_minutes),
            "status": STATUS_SANDBOX,
            "revoke_reason": None,
        }
        self.sessions[slice_id] = session
        return session

    def _revoke_session(self, session: dict, reason: str, now: datetime) -> None:
        session["status"] = STATUS_REVOKED
        session["revoke_reason"] = reason
        self.audit.append({"at": now.isoformat(), "action": "会话撤回",
                           "slice_id": session["id"], "reason": reason})

    def _session_live(self, session: dict, now: datetime) -> Optional[str]:
        if session["status"] == STATUS_REVOKED:
            return session["revoke_reason"] or "会话已撤回"
        if now >= session["expires_at"]:
            return "切片过期"
        return None

    # ---- 作业与环境指纹 -------------------------------------------------

    def submit_assignment(self, assignment_id: str, student_id: str, task_id: str,
                          conclusion: str, recipe: list[dict], now: datetime,
                          slice_ids: Optional[list[str]] = None) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFound(f"未知任务：{task_id}")
        rows = []
        for slice_id in slice_ids or []:
            session = self.sessions.get(slice_id)
            if session is None:
                raise NotFound(f"未知切片：{slice_id}")
            if session["student_id"] != student_id or session["task_id"] != task_id:
                raise AuthorizationError("切片不属于当前学生或任务")
            if self._session_live(session, now):
                raise AuthorizationError("引用的沙箱切片已失效，不能用于提交")
            rows = session["rows"]
        result_hash = digest({"rows": rows, "recipe": recipe})
        assignment = {
            "id": assignment_id,
            "task_id": task_id,
            "student_id": student_id,
            "conclusion": conclusion,
            "recipe": recipe,
            "result_hash": result_hash,
            "status": ASSIGNMENT_OPEN,
            "submitted_at": now,
            "graded_at": None,
            "grade": None,
            "fingerprint": None,
            "reviews": [],
            "risk_flags": [],
            "slice_ids": list(slice_ids or []),
            "export_ids": [],
        }
        self.assignments[assignment_id] = assignment
        return assignment

    def environment_fingerprint(self, task: dict, assignment: dict) -> dict:
        """评分时冻结的环境指纹：数据版本、脱敏策略、工具镜像、处理步骤四要素齐备。"""
        snapshot = self.snapshots[(task["case_id"], task["snapshot_version"])]
        policy = self.policies[task["policy_id"]]
        env = self._environment(task["environment_id"], task["environment_version"])
        return {
            "snapshot": {
                "case_id": task["case_id"],
                "version": snapshot["version"],
                "content_hash": snapshot["content_hash"],
            },
            "policy": {"id": policy["id"], "version": policy["version"],
                       "hash": digest({k: v for k, v in policy.items() if k != "id"})},
            "environment": {"id": env["id"], "version": env["version"],
                            "digest": env["digest"]},
            "recipe": assignment["recipe"],
            "recipe_hash": digest(assignment["recipe"]),
        }

    def grade_assignment(self, teacher_id: str, assignment_id: str, grade: str,
                         review_note: str, now: datetime) -> dict:
        """教师评分：冻结当时环境指纹并留下复核记录；此后作业内容不可改写。"""
        assignment = self.assignments.get(assignment_id)
        if assignment is None:
            raise NotFound(f"未知作业：{assignment_id}")
        task = self.tasks[assignment["task_id"]]
        if not self._is_teacher(teacher_id, task["course_id"]):
            raise AuthorizationError("只有任课教师可以评分")
        if assignment["status"] == STATUS_GRADED:
            raise SandboxError("作业已评分，评分记录只能追加不能覆盖")
        assignment["status"] = STATUS_GRADED
        assignment["grade"] = grade
        assignment["graded_at"] = now
        assignment["fingerprint"] = self.environment_fingerprint(task, assignment)
        assignment["reviews"].append({
            "teacher_id": teacher_id, "at": now.isoformat(),
            "grade": grade, "note": review_note, "stage": "评分",
        })
        # 评分时刻之后才出现的风险由勘误/升级/撤回事件追加
        return assignment

    @staticmethod
    def _flag(assignment: dict, flag: dict) -> None:
        if any(existing.get("type") == flag["type"]
               and existing.get("current_version") == flag.get("current_version")
               and existing.get("at") == flag.get("at")
               for existing in assignment["risk_flags"]):
            return
        assignment["risk_flags"].append(flag)

    # ---- 导出与披露检查 -------------------------------------------------

    def request_export(self, export_id: str, student_id: str, slice_id: str,
                       groups: list[dict], columns: list[str], now: datetime,
                       assignment_id: Optional[str] = None) -> dict:
        """提交导出申请并立即执行披露检查；小样本等情形返回“阻断”而非放行。"""
        session = self.sessions.get(slice_id)
        if session is None:
            raise NotFound(f"未知切片：{slice_id}")
        if session["student_id"] != student_id:
            raise AuthorizationError("切片不属于该学生")
        task = self.tasks[session["task_id"]]
        policy = self.policies[task["policy_id"]]
        record = {
            "id": export_id,
            "slice_id": slice_id,
            "assignment_id": assignment_id,
            "student_id": student_id,
            "groups": groups,
            "columns": columns,
            "requested_at": now,
            "decision": STATUS_REVIEW,
            "reasons": [],
            "decided_at": None,
        }
        self.exports[export_id] = record
        self._disclose(record, now)
        if assignment_id and assignment_id in self.assignments:
            self.assignments[assignment_id]["export_ids"].append(export_id)
        return record

    def _disclose(self, record: dict, now: datetime) -> None:
        session = self.sessions[record["slice_id"]]
        task = self.tasks[session["task_id"]]
        reasons: list[str] = []

        dead = self._session_live(session, now)
        if dead:
            reasons.append(dead)
        enrollment = self._enrollment(record["student_id"], task["course_id"])
        if enrollment is None or enrollment["status"] == STATUS_REVOKED \
                or not self._course_open(task["course_id"], now):
            reasons.append("课程授权失效")
        if not self._active_consents(task["case_id"], task["course_id"], now):
            reasons.append("同意撤回")

        snapshot = self.snapshots[(task["case_id"], task["snapshot_version"])]
        forbidden = set(snapshot["identity_fields"])
        leaked = sorted(forbidden.intersection(record["columns"]))
        if leaked:
            reasons.append(f"含身份字段：{','.join(leaked)}")

        threshold = self.policies[task["policy_id"]]["k_threshold"]
        for group in record["groups"]:
            if group["count"] < threshold:
                reasons.append(
                    f"小样本组 {group['key']} 仅 {group['count']} 例（k≥{threshold}）")

        record["reasons"] = reasons
        record["decision"] = EXPORT_BLOCKED if reasons else EXPORT_APPROVED
        record["decided_at"] = now
        self.audit.append({
            "at": now.isoformat(), "action": "披露检查",
            "export_id": record["id"], "decision": record["decision"],
            "reasons": reasons,
        })

    # ---- 同意撤回：即时效力 ---------------------------------------------

    def withdraw_consent(self, consent_id: str, now: datetime) -> dict:
        """撤回同意：进行中的相关会话立即撤回，待披露导出阻断，受影响作业列清。

        已评分作业不被删除或改写，保留环境指纹并追加“同意撤回”风险标记。
        """
        grant = self.consents.get(consent_id)
        if grant is None:
            raise NotFound(f"未知同意记录：{consent_id}")
        if grant["withdrawn_at"] is not None:
            raise SandboxError("同意已撤回，不能重复撤回")
        # 撤回效力按撤回前已提交事实（首次绑定 + 已生效更正）确定影响面
        covered_task_ids = {
            task["id"] for task in self.tasks.values()
            if self._grant_covers(grant, task["case_id"], task["course_id"], now)
        }
        grant["withdrawn_at"] = now

        affected: list[dict] = []
        for task in self.tasks.values():
            if task["id"] not in covered_task_ids:
                continue
            # 撤回该病例相关的进行中会话
            for session in self.sessions.values():
                if session["task_id"] == task["id"] and session["status"] == STATUS_SANDBOX:
                    self._revoke_session(session, "同意撤回", now)
            # 仍在披露队列中的导出立即阻断
            for export in self.exports.values():
                sess = self.sessions[export["slice_id"]]
                if sess["task_id"] == task["id"] and export["decision"] != EXPORT_BLOCKED:
                    export["decision"] = EXPORT_BLOCKED
                    export["reasons"] = list(dict.fromkeys(
                        export["reasons"] + ["同意撤回"]))
                    export["decided_at"] = now
            for assignment in self.assignments.values():
                if assignment["task_id"] != task["id"]:
                    continue
                impact = ("已评分：保留冻结指纹并标记风险"
                          if assignment["status"] == STATUS_GRADED
                          else "未评分：会话撤回且导出阻断")
                if assignment["status"] == STATUS_GRADED:
                    self._flag(assignment, {
                        "type": "同意撤回",
                        "at": now.isoformat(),
                        "detail": f"授权 {consent_id} 已撤回，结论所依据的授权不再有效",
                        "consent_id": consent_id,
                    })
                affected.append({
                    "assignment_id": assignment["id"],
                    "course_id": task["course_id"],
                    "student_id": assignment["student_id"],
                    "status": assignment["status"],
                    "impact": impact,
                })
        report = {
            "consent_id": consent_id,
            "case_id": grant["case_id"],
            "withdrawn_at": now.isoformat(),
            "assignments": affected,
        }
        self.audit.append({"at": now.isoformat(), "action": "同意撤回", **report})
        return report

    # ---- 教师复现与身份隔离 ---------------------------------------------

    def read_patient_identity(self, actor_id: str, case_id: str) -> None:
        """尝试读取患者身份字段。

        学生、教师角色没有 ``patient_identity:read`` 能力；即便某身份持有该
        临床侧能力，教学沙箱也不会在任何教学会话中下发身份字段。两道闸缺一不可。
        """
        actor = self.actors.get(actor_id, {"roles": []})
        if "patient_identity:read" not in actor.get("roles", []):
            raise AuthorizationError(
                f"身份 {actor_id} 无权读取病例 {case_id} 的患者身份字段")
        raise AuthorizationError(
            f"教学沙箱不向任何会话下发病例 {case_id} 的患者身份字段")

    def reproduce_report(self, teacher_id: str, assignment_id: str,
                         now: datetime) -> dict:
        """教师按评分时冻结的指纹复现实验：先校验任课身份，再在冻结环境中重放。

        教师可以复现实验，但全程只有脱敏视图；非任课课程的作业不可访问。
        """
        assignment = self.assignments.get(assignment_id)
        if assignment is None:
            raise NotFound(f"未知作业：{assignment_id}")
        if assignment["status"] != STATUS_GRADED:
            raise SandboxError("只能复现已评分作业，以保证指纹已冻结")
        task = self.tasks[assignment["task_id"]]
        if not self._is_teacher(teacher_id, task["course_id"]):
            raise AuthorizationError("非任课教师不能复现该课程作业")

        snapshot = self.snapshots[(task["case_id"], task["snapshot_version"])]
        env = self._environment(task["environment_id"], task["environment_version"])
        # 教师在能力层面即被拒绝读取患者身份（独立于课程授权的一道闸）
        actor = self.actors.get(teacher_id, {"roles": []})
        identity_denied = "patient_identity:read" not in actor.get("roles", [])

        # 在冻结环境中重放：每个步骤所用工具必须存在于当时镜像
        missing = [step["tool"] for step in assignment["recipe"]
                   if step["tool"] not in env["tools"]]
        rerun_rows = self._project_rows(task)
        rerun_hash = digest({"rows": rerun_rows, "recipe": assignment["recipe"]})
        match = not missing and rerun_hash == assignment["result_hash"]

        current_fp = self.environment_fingerprint(task, assignment)
        return {
            "assignment_id": assignment_id,
            "teacher_id": teacher_id,
            "status": "复现一致" if match else "复现不一致",
            "frozen_fingerprint": assignment["fingerprint"],
            "fingerprint_intact": current_fp == assignment["fingerprint"],
            "rerun": {
                "missing_tools": missing,
                "result_hash": rerun_hash,
                "matches_graded": match,
            },
            "identity_access": "拒绝" if identity_denied else "异常放行",
            "identity_fields": snapshot["identity_fields"],
            "risk_flags": assignment["risk_flags"],
        }

    # ---- 结论溯源 -------------------------------------------------------

    def trace(self, assignment_id: str) -> dict:
        """把任一分析结论追到数据范围、处理步骤、课程授权、教师复核四部分。"""
        assignment = self.assignments.get(assignment_id)
        if assignment is None:
            raise NotFound(f"未知作业：{assignment_id}")
        task = self.tasks[assignment["task_id"]]
        course = self.courses[task["course_id"]]
        snapshot = self.snapshots[(task["case_id"], task["snapshot_version"])]
        policy = self.policies[task["policy_id"]]
        env = self._environment(task["environment_id"], task["environment_version"])
        enrollment = self._enrollment(assignment["student_id"], task["course_id"])

        consent_view = []
        for grant in self.consents.values():
            # 展示首次事实与所有已追加更正中覆盖本病例/本课程的绑定，
            # 待裁定冲突的后到内容不进入授权视图
            bindings = [b for b in self._effective_bindings(grant, datetime.max)
                        if b["case_id"] == task["case_id"]
                        and (b["course_id"] is None
                             or b["course_id"] == task["course_id"])]
            if not bindings:
                continue
            consent_view.append({
                "consent_id": grant["id"],
                "granted_at": grant["granted_at"].isoformat(),
                "withdrawn_at": grant["withdrawn_at"].isoformat()
                if grant["withdrawn_at"] else None,
                "scope": "课程专用" if grant["course_id"] else "教学通用",
                "source": grant.get("source", "import"),
                "first_import_batch": grant.get("first_import_batch"),
                "binding_fingerprint": grant["binding_fingerprint"],
                "replays": list(grant.get("replays", [])),
                "corrections": [
                    {
                        "correction_id": c["id"],
                        "case_id": c["case_id"],
                        "course_id": c["course_id"],
                        "authorized_at": c["authorized_at"],
                        "effective_at": c["effective_at"],
                        "binding_fingerprint": c["binding_fingerprint"],
                        "source": c["source"],
                    }
                    for c in self.consent_corrections
                    if c["consent_id"] == grant["id"]
                ],
                "conflict": self._conflict_view(grant["id"]),
            })
        return {
            "assignment_id": assignment_id,
            "conclusion": assignment["conclusion"],
            "数据范围": {
                "case_id": task["case_id"],
                "snapshot_version": snapshot["version"],
                "content_hash": snapshot["content_hash"],
                "objective_fields": task["objective_fields"],
                "slice_ids": assignment["slice_ids"],
            },
            "处理步骤": {
                "policy": {"id": policy["id"], "version": policy["version"]},
                "environment": {"id": env["id"], "version": env["version"],
                                "digest": env["digest"]},
                "recipe": assignment["recipe"],
                "recipe_hash": digest(assignment["recipe"]),
            },
            "课程授权": {
                "course_id": task["course_id"],
                "course_name": course["name"],
                "cross_institutional": course["cross_institutional"],
                "window": [course["starts_at"].isoformat(),
                           course["ends_at"].isoformat()],
                "student_id": assignment["student_id"],
                "enrollment_status": enrollment["status"] if enrollment else None,
                "consents": consent_view,
            },
            "教师复核": list(assignment["reviews"]),
            "风险标记": list(assignment["risk_flags"]),
            "fingerprint": assignment["fingerprint"],
        }

    def _conflict_view(self, consent_id: str) -> Optional[dict]:
        conflict = self.consent_conflicts.get(consent_id)
        if conflict is None:
            return None
        return {
            "status": conflict["status"],
            "changed_fields": self._binding_diff(
                conflict["first_binding"],
                conflict["attempts"][-1]["binding"]) if conflict["attempts"] else [],
            "attempt_count": len(conflict["attempts"]),
            "resolution": conflict["resolution"],
        }

    def consent_report(self, consent_id: str) -> dict:
        """按同意编号追溯：首次来源、重放、冲突及其对课程/会话/作业的影响。

        用于事故复盘：解释编号绑定边界、后到内容是否获得能力（未裁定前不获得），
        以及首次事实与冲突分别影响哪些课程、进行中会话与已评分作业。
        """
        grant = self.consents.get(consent_id)
        if grant is None:
            raise NotFound(f"未知同意记录：{consent_id}")

        bindings = self._effective_bindings(grant, datetime.max)
        covered_cases = sorted({b["case_id"] for b in bindings})

        def binding_matches(case_id: str, course_id: str) -> bool:
            return any(b["case_id"] == case_id
                       and (b["course_id"] is None or b["course_id"] == course_id)
                       for b in bindings)

        covered_courses: set[str] = set()
        sessions, graded, open_assignments = [], [], []
        for task in self.tasks.values():
            if not binding_matches(task["case_id"], task["course_id"]):
                continue
            covered_courses.add(task["course_id"])
            for session in self.sessions.values():
                if session["task_id"] != task["id"]:
                    continue
                sessions.append({
                    "slice_id": session["id"], "course_id": task["course_id"],
                    "case_id": task["case_id"], "student_id": session["student_id"],
                    "status": session["status"],
                    "revoke_reason": session.get("revoke_reason"),
                })
            for assignment in self.assignments.values():
                if assignment["task_id"] != task["id"]:
                    continue
                row = {
                    "assignment_id": assignment["id"],
                    "course_id": task["course_id"],
                    "student_id": assignment["student_id"],
                    "status": assignment["status"],
                    "risk_flags": assignment["risk_flags"],
                }
                (graded if assignment["status"] == STATUS_GRADED
                 else open_assignments).append(row)

        conflict = self.consent_conflicts.get(consent_id)
        incoming_impact: list[str] = []
        if conflict is not None and conflict["status"] == CONFLICT_PENDING:
            # 后到内容未获能力：说明它本会扩到哪些课程/病例
            for attempt in conflict["attempts"]:
                b = attempt["binding"]
                incoming_impact.append(
                    f"待裁定后到内容（{','.join(self._binding_diff(conflict['first_binding'], b))}"
                    f"）未获得能力：病例 {b['case_id']} / "
                    f"{'教学通用' if b['course_id'] is None else b['course_id']}")

        return {
            "consent_id": consent_id,
            "首次来源": {
                "source": grant.get("source", "import"),
                "import_batch": grant.get("first_import_batch"),
                "imported_at": self._iso(grant.get("first_imported_at")),
                "binding": grant["binding"],
                "binding_fingerprint": grant["binding_fingerprint"],
                "withdrawn_at": self._iso(grant["withdrawn_at"]),
            },
            "重放": list(grant.get("replays", [])),
            "更正": [
                {
                    "correction_id": c["id"], "binding": c["binding"],
                    "authorized_at": c["authorized_at"],
                    "effective_at": c["effective_at"], "source": c["source"],
                    "officer_id": c["officer_id"], "note": c["note"],
                } for c in self.consent_corrections
                if c["consent_id"] == consent_id
            ],
            "冲突": ({
                "status": conflict["status"],
                "first_binding": conflict["first_binding"],
                "attempts": conflict["attempts"],
                "resolution": conflict["resolution"],
                "后到内容能力": incoming_impact,
            } if conflict is not None else None),
            "影响面": {
                "covered_cases": covered_cases,
                "covered_courses": sorted(covered_courses),
                "sessions": sessions,
                "graded_assignments": graded,
                "open_assignments": open_assignments,
            },
        }

    # ---- 序列化与恢复 ---------------------------------------------------

    @staticmethod
    def _iso(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if isinstance(value, datetime) else value

    def _dump_consent(self, grant: dict) -> dict:
        return {
            "id": grant["id"],
            "case_id": grant["case_id"],
            "course_id": grant["course_id"],
            "granted_at": self._iso(grant["granted_at"]),
            "withdrawn_at": self._iso(grant["withdrawn_at"]),
            "scope_note": grant.get("scope_note", ""),
            "content_summary": grant.get("content_summary", ""),
            "binding": grant.get("binding"),
            "binding_fingerprint": grant.get("binding_fingerprint"),
            "source": grant.get("source", "import"),
            "first_import_batch": grant.get("first_import_batch"),
            "first_imported_at": self._iso(grant.get("first_imported_at")),
            "replays": grant.get("replays", []),
        }

    def to_state(self) -> dict:
        """导出可 JSON 序列化的完整账本状态（含冲突、更正与审计）。"""
        return {
            "format_version": 2,
            "actors": list(self.actors.values()),
            "courses": [{
                **c,
                "starts_at": self._iso(c["starts_at"]),
                "ends_at": self._iso(c["ends_at"]),
            } for c in self.courses.values()],
            "cases": list(self.cases.values()),
            "policies": list(self.policies.values()),
            "environments": [{
                "id": eid, "version": ver, **{
                    k: (self._iso(v) if k == "released_at" else v)
                    for k, v in rec.items() if k not in ("id", "version")},
            } for (eid, ver), rec in self.environment_versions.items()],
            "snapshots": [{
                **{k: v for k, v in rec.items()},
                "released_at": self._iso(rec["released_at"]),
            } for (_, _ver), rec in self.snapshots.items()],
            "consents": [self._dump_consent(g) for g in self.consents.values()],
            "consent_conflicts": list(self.consent_conflicts.values()),
            "consent_corrections": list(self.consent_corrections),
            "enrollments": list(self.enrollments),
            "teachers": list(self.teacher_courses),
            "tasks": [{**t, "created_at": self._iso(t["created_at"])}
                      for t in self.tasks.values()],
            "assignments": [{
                **a,
                "submitted_at": self._iso(a["submitted_at"]),
                "graded_at": self._iso(a["graded_at"]),
            } for a in self.assignments.values()],
            "sessions": [{
                **s,
                "issued_at": self._iso(s["issued_at"]),
                "expires_at": self._iso(s["expires_at"]),
            } for s in self.sessions.values()],
            "exports": [{
                **e,
                "requested_at": self._iso(e["requested_at"]),
                "decided_at": self._iso(e["decided_at"]),
            } for e in self.exports.values()],
            "audit": list(self.audit),
            "seq": self._seq,
            "correction_seq": self._correction_seq,
        }

    def dump_state(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_state(), ensure_ascii=False, indent=2),
            encoding="utf-8")

    @classmethod
    def from_state(cls, state: dict) -> "Sandbox":
        """从 :meth:`to_state` 的产物恢复；兼容缺少绑定字段的旧版转储。"""
        box = cls()
        for actor in state.get("actors", []):
            box.actors[actor["id"]] = dict(actor)
        for course in state.get("courses", []):
            box.courses[course["id"]] = {
                **course,
                "starts_at": parse_time(course["starts_at"]),
                "ends_at": parse_time(course["ends_at"]),
            }
        for case in state.get("cases", []):
            box.cases[case["id"]] = dict(case)
        for policy in state.get("policies", []):
            box.policies[policy["id"]] = dict(policy)
        for env in state.get("environments", []):
            rec = dict(env)
            eid, ver = rec["id"], rec["version"]
            rec["released_at"] = parse_time(rec["released_at"])
            box.environment_versions[(eid, ver)] = rec
        for snap in state.get("snapshots", []):
            rec = dict(snap)
            key = (rec["case_id"], rec["version"])
            rec["released_at"] = parse_time(rec["released_at"])
            box.snapshots[key] = rec
        for data in state.get("consents", []):
            box._restore_consent(data)
        for correction in state.get("consent_corrections", []):
            box._load_correction(correction)
        for conflict in state.get("consent_conflicts", []):
            box._load_conflict(conflict)
        for enrollment in state.get("enrollments", []):
            box.enrollments.append(dict(enrollment))
        for link in state.get("teachers", []):
            box.teacher_courses.append(dict(link))
        for data in state.get("tasks", []):
            task = dict(data)
            task["created_at"] = parse_time(task["created_at"])
            box.tasks[task["id"]] = task
        for data in state.get("assignments", []):
            record = dict(data)
            record["submitted_at"] = parse_time(record["submitted_at"])
            if record.get("graded_at"):
                record["graded_at"] = parse_time(record["graded_at"])
            box.assignments[record["id"]] = record
        for data in state.get("sessions", []):
            session = dict(data)
            session["issued_at"] = parse_time(session["issued_at"])
            session["expires_at"] = parse_time(session["expires_at"])
            box.sessions[session["id"]] = session
        for data in state.get("exports", []):
            export = dict(data)
            export["requested_at"] = parse_time(export["requested_at"])
            if export.get("decided_at"):
                export["decided_at"] = parse_time(export["decided_at"])
            box.exports[export["id"]] = export
        box.audit.extend(state.get("audit", []))
        box._seq = state.get("seq", 0)
        box._correction_seq = state.get("correction_seq", 0)
        return box

    def _restore_consent(self, data: dict) -> None:
        """恢复一份同意；旧版转储缺少绑定时按记录字段补齐（只读兼容）。"""
        granted_at = parse_time(data["granted_at"]) if data.get("granted_at") else None
        binding = data.get("binding")
        if binding is None:
            binding = self._consent_binding(
                data["case_id"], data.get("course_id"),
                granted_at, data.get("scope_note", ""),
                data.get("content_summary", ""))
        fp = data.get("binding_fingerprint") or self._binding_fingerprint(binding)
        first_imported_at = data.get("first_imported_at")
        grant = {
            "id": data["id"],
            "case_id": data["case_id"],
            "course_id": data.get("course_id"),
            "granted_at": granted_at,
            "withdrawn_at": parse_time(data["withdrawn_at"])
            if data.get("withdrawn_at") else None,
            "scope_note": data.get("scope_note", ""),
            "content_summary": data.get("content_summary", ""),
            "binding": binding,
            "binding_fingerprint": fp,
            "source": data.get("source", "import"),
            "first_import_batch": data.get("first_import_batch"),
            "first_imported_at": parse_time(first_imported_at)
            if first_imported_at else None,
            "replays": data.get("replays", []),
        }
        self.consents[grant["id"]] = grant

    @classmethod
    def load_state(cls, path: str | Path) -> "Sandbox":
        return cls.from_state(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- 查询 -----------------------------------------------------------

    def assignments_for_case(self, case_id: str) -> list[dict]:
        """列出依赖某病例的全部作业（跨课程），供撤回/勘误时清点影响面。"""
        result = []
        for assignment in self.assignments.values():
            task = self.tasks[assignment["task_id"]]
            if task["case_id"] == case_id:
                result.append({
                    "assignment_id": assignment["id"],
                    "course_id": task["course_id"],
                    "student_id": assignment["student_id"],
                    "status": assignment["status"],
                    "risk_flags": assignment["risk_flags"],
                })
        return result


def self_check(seed_path: str | Path) -> None:
    """装载种子并核对六账齐备、同病例跨课程关系成立。"""
    box = Sandbox.from_seed(seed_path)
    assert box.cases and box.snapshots and box.consents
    assert box.policies and box.environment_versions and box.assignments
    shared = next(iter(box.cases.values()))
    assert len(shared["courses"]) == 2, "种子中应有一个病例同时进入两门课程"
    print(f"领域检查通过：{len(box.cases)} 个病例，{len(box.snapshots)} 个快照版本，"
          f"{len(box.assignments)} 份作业，病例 {shared['id']} 进入 {shared['courses']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="医学教学数据沙箱领域规则")
    parser.add_argument("--check", action="store_true", help="装载种子夹具并自检")
    parser.add_argument("--seed", default="fixtures/seed.json")
    args = parser.parse_args()
    if args.check:
        self_check(args.seed)


if __name__ == "__main__":
    main()
