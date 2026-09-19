"""名著多角度阅读档案——领域层。

设计要点
========

1. 只追加事件日志（append-only）：任何状态改变都先落为一条带序号、时间戳、
   操作者、原因的事件，再从事件重放出当前状态。因此教师调整路线、撤回批注、
   换用新版教材都不会销毁历史——过去班级看到的内容可以按任意时间点复原。

2. 作品版本 + 章节锚点：引用位置由"文本版本 + 章节定位 + 起止字符偏移"
   固定，不同纸书版本互不串用；观点（个人 / 小组 / 教师）各自携带证据
   锚点，互相冲突的解释是并存的多条观点，而不是对同一条记录的覆盖。

3. 发布是一条决定链：草稿 -> 发布（可见范围、最低年龄）-> 撤回 / 编辑。
   每个观点都能回答"出自谁、依据哪个文本版本、经过哪些发布决定"。

4. 可见范围由班级成员身份、年龄门禁、监护关系共同决定；学生离校后
   凭证失效（不能再访问内部材料），但其身份与已发布讨论保留，引用不断链。

5. 改编作品（如《悟空传》）只登记与原著的关联和必要元数据，正文不入库。

本模块只依赖标准库，可独立于 HTTP 层使用。
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# ---------------------------------------------------------------------------
# 常量与异常
# ---------------------------------------------------------------------------

ROLE_STUDENT = "student"
ROLE_TEACHER = "teacher"
ROLE_GUARDIAN = "guardian"
ROLE_ADMIN = "admin"

AUDIENCE_PERSONAL = "personal"        # 个人笔记
AUDIENCE_GROUP = "group"              # 小组
AUDIENCE_CLASS = "class"              # 全班
AUDIENCE_GUARDIANS = "guardians"      # 家长（监护人）

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_WITHDRAWN = "withdrawn"

RELATION_ADAPTATION = "adaptation"    # 改编作品 -> 原著

# 未满足可见性时的区分，便于上层给出准确提示，而不是把存在性暴露出去
DENY_NOT_FOUND = "not_found"
DENY_AGE = "age_restricted"
DENY_SCOPE = "outside_scope"
DENY_DRAFT = "unpublished"
DENY_WITHDRAWN = "withdrawn"
DENY_LEFT = "access_revoked"          # 已离校，凭证失效
DENY_FORBIDDEN = "forbidden"

_STAFF = {ROLE_TEACHER, ROLE_ADMIN}


class ArchiveError(Exception):
    """所有领域规则冲突的基类。"""


class NotFoundError(ArchiveError):
    """引用的对象不存在。"""


class PermissionError(ArchiveError):  # noqa: A001 - 领域内刻意与内建同名语义
    """操作者无权执行该动作。"""


class ConflictError(ArchiveError):
    """与当前状态冲突（重复注册、对象已停用等）。"""


class ValidationError(ArchiveError):
    """入参不满足领域约束。"""


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Anchor:
    """章节锚点：把引用固定到某个文本版本中的具体位置。

    chapter_path 是章节逻辑定位（如 "第027回"），跨版本稳定；
    label 是给人看的章节标题（如"第二十七回 尸魔三戏唐三藏"）；
    start/end 是该版本正文中的字符偏移（左闭右开），quote 回填原文片段。
    """

    version_id: str
    chapter_path: str
    start: int = 0
    end: int = 0
    quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "chapter_path": self.chapter_path,
            "start": self.start,
            "end": self.end,
            "quote": self.quote,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Anchor":
        return cls(
            version_id=data["version_id"],
            chapter_path=data["chapter_path"],
            start=int(data.get("start", 0)),
            end=int(data.get("end", 0)),
            quote=data.get("quote", ""),
        )


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: str
    birth_year: Optional[int] = None
    active: bool = True
    left_at: Optional[str] = None


@dataclass(frozen=True)
class RelatedWork:
    """改编作品：只保留与原著的关联和必要元数据，不收录正文。"""

    id: str
    title: str
    kind: str
    relation: str
    source_work_id: str
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Viewpoint:
    """一条观点（个人笔记 / 小组观点 / 教师观点）。

    冲突解释各自成条、共存在同一锚点下；编辑只替换正文，证据链
    （作者、依据版本、发布决定链）不可改写。
    """

    id: str
    author_id: str
    audience: str
    work_id: str
    anchor: Anchor
    body: str
    group_id: Optional[str] = None
    class_id: Optional[str] = None
    min_age: int = 0
    status: str = STATUS_DRAFT
    created_at: Optional[str] = None
    published_at: Optional[str] = None
    withdrawn_at: Optional[str] = None
    withdraw_reason: str = ""
    decisions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Anchor] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)
    supersedes: Optional[str] = None
    deleted: bool = False


# ---------------------------------------------------------------------------
# 事件日志
# ---------------------------------------------------------------------------

class EventStore:
    """只追加事件日志；内存实现，可选 JSONL 持久化。

    每条事件形如：
      {"seq": 1, "ts": "2026-09-01T08:00:00Z", "type": "...",
       "actor": "teacher-1", "reason": "...", "data": {...}}
    """

    def __init__(self, path: Optional[str | Path] = None, clock: Optional[Callable[[], str]] = None):
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.RLock()
        self._clock = clock or _utc_now
        self.path = Path(path) if path else None
        self._events: list[dict[str, Any]] = []
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._events.append(json.loads(line))

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def next_seq(self) -> int:
        with self._lock:
            return len(self._events) + 1

    def append(self, event_type: str, actor: str, data: dict[str, Any],
               reason: str = "") -> dict[str, Any]:
        with self._lock:
            event = {
                "seq": len(self._events) + 1,
                "ts": self._clock(),
                "type": event_type,
                "actor": actor,
                "reason": reason,
                "data": data,
            }
            self._events.append(event)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            for listener in list(self._listeners):
                listener(event)
            return event

    def seed(self, events: Iterable[dict[str, Any]]) -> "EventStore":
        """预填事件（仅供时间点快照等只读重放使用）。"""
        import copy
        with self._lock:
            self._require_state_empty()
            self._events.extend(copy.deepcopy(list(events)))
        return self

    def _require_state_empty(self) -> None:
        if self._events:
            raise ConflictError("事件流非空，不能预填")

    def listen(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 阅读档案
# ---------------------------------------------------------------------------

class ReadingArchive:
    """事件溯源的阅读档案；命令写事件，查询走投影。"""

    # ----- 构造与重放 -----------------------------------------------------

    def __init__(self, store: Optional[EventStore] = None):
        self.store = store or EventStore()
        self._lock = threading.RLock()

        self.works: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.chapters: dict[str, dict[str, Any]] = {}          # chapter_id -> 元数据
        self.anchor_by_chapter: dict[str, dict[str, str]] = {} # (version_id, chapter_path) -> chapter_id
        self.people: dict[str, Person] = {}
        self.classes: dict[str, dict[str, Any]] = {}
        self.memberships: dict[str, set[str]] = defaultdict(set)   # class_id -> {person_id}
        self.guardianships: dict[str, set[str]] = defaultdict(set) # student_id -> {guardian_id}
        self.groups: dict[str, dict[str, Any]] = {}
        self.group_members: dict[str, set[str]] = defaultdict(set)
        self.routes: dict[str, dict[str, Any]] = {}
        self.route_entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.viewpoints: dict[str, Viewpoint] = {}
        self.related: dict[str, RelatedWork] = {}
        self._deleted: set[str] = set()

        for event in self.store.events:
            self._apply(event)
        # 此后新追加的事件实时投影
        self.store.listen(self._apply)

    # ----- 通用校验 -------------------------------------------------------

    @staticmethod
    def _require(condition: bool, message: str, exc: type[ArchiveError] = ValidationError) -> None:
        if not condition:
            raise exc(message)

    def _person(self, person_id: str) -> Person:
        person = self.people.get(person_id)
        if person is None:
            raise NotFoundError(f"人员不存在：{person_id}")
        return person

    def _version(self, version_id: str) -> dict[str, Any]:
        version = self.versions.get(version_id)
        if version_id in self._deleted or version is None:
            raise NotFoundError(f"文本版本不存在：{version_id}")
        return version

    def _require_staff(self, actor: str) -> Person:
        person = self._person(actor)
        if person.role not in _STAFF:
            raise PermissionError("仅教师或管理员可执行该操作")
        return person

    def _resolve_anchor(self, anchor_data: dict[str, Any]) -> Anchor:
        anchor = Anchor.from_dict(anchor_data)
        version = self._version(anchor.version_id)
        key = (anchor.version_id, anchor.chapter_path)
        self._require(
            key in self.anchor_by_chapter,
            f"版本 {anchor.version_id} 中不存在章节 {anchor.chapter_path}",
            NotFoundError,
        )
        self._require(0 <= anchor.start <= anchor.end, "锚点字符偏移非法（需 0 ≤ start ≤ end）")
        if anchor.end > 0 and version.get("char_count"):
            self._require(anchor.end <= version["char_count"], "锚点超出版本正文范围")
        return anchor

    # ----- 作品与版本 -----------------------------------------------------

    def register_work(self, actor: str, work_id: str, title: str, author: str = "",
                      *, reason: str = "") -> dict[str, Any]:
        self._require_staff(actor)
        self._require(work_id and title, "作品 id 与标题必填")
        with self._lock:
            self._require(work_id not in self.works, f"作品已存在：{work_id}", ConflictError)
            return self.store.append("work.registered", actor,
                                     {"work_id": work_id, "title": title, "author": author}, reason)

    def register_version(self, actor: str, version_id: str, work_id: str, label: str,
                         publisher: str = "", year: Optional[int] = None,
                         char_count: int = 0, *, isbn: str = "", reason: str = "",
                         replaces: Optional[str] = None) -> dict[str, Any]:
        """登记纸书/教材版本。replaces 表示本版本接替某个旧版教材。"""
        self._require_staff(actor)
        with self._lock:
            self._require(work_id in self.works, f"作品不存在：{work_id}", NotFoundError)
            self._require(version_id not in self.versions, f"版本已存在：{version_id}", ConflictError)
            if replaces:
                self._require(replaces in self.versions, f"被接替版本不存在：{replaces}", NotFoundError)
            return self.store.append("version.registered", actor, {
                "version_id": version_id, "work_id": work_id, "label": label,
                "publisher": publisher, "year": year, "isbn": isbn,
                "char_count": char_count, "replaces": replaces,
            }, reason)

    def register_chapter(self, actor: str, chapter_id: str, version_id: str,
                         chapter_path: str, title: str, order: int = 0,
                         *, reason: str = "") -> dict[str, Any]:
        """登记版本中的章节，锚点以 (版本, 章节路径) 定位。"""
        self._require_staff(actor)
        with self._lock:
            version = self._version(version_id)
            key = (version_id, chapter_path)
            self._require(key not in self.anchor_by_chapter,
                          f"章节锚点已存在：{version_id}/{chapter_path}", ConflictError)
            return self.store.append("chapter.registered", actor, {
                "chapter_id": chapter_id, "version_id": version_id,
                "work_id": version["work_id"], "chapter_path": chapter_path,
                "title": title, "order": order,
            }, reason)

    # ----- 改编作品（最小导入）-------------------------------------------

    def register_related_work(self, actor: str, related_id: str, title: str, kind: str,
                              source_work_id: str, *, note: str = "",
                              metadata: Optional[dict[str, Any]] = None,
                              reason: str = "") -> dict[str, Any]:
        """登记改编作品（如《悟空传》）。只记录关联与必要元数据，不收录正文。"""
        self._require_staff(actor)
        with self._lock:
            self._require(related_id not in self.related, f"改编作品已存在：{related_id}", ConflictError)
            self._require(source_work_id in self.works,
                          f"所关联原著不存在：{source_work_id}", NotFoundError)
            return self.store.append("related.registered", actor, {
                "related_id": related_id, "title": title, "kind": kind,
                "relation": RELATION_ADAPTATION, "source_work_id": source_work_id,
                "note": note, "metadata": metadata or {},
            }, reason)

    # ----- 人员、班级、监护关系 -------------------------------------------

    def register_person(self, actor: str, person_id: str, name: str, role: str,
                        *, birth_year: Optional[int] = None, reason: str = "") -> dict[str, Any]:
        # 引导规则：系统中尚无人员时，首位教师/管理员可直接登记；之后一律需在职教师
        if not self.people:
            self._require(role in {ROLE_TEACHER, ROLE_ADMIN},
                          "首位登记人员必须是教师或管理员")
        else:
            self._require_staff(actor)
        self._require(role in {ROLE_STUDENT, ROLE_TEACHER, ROLE_GUARDIAN, ROLE_ADMIN},
                      f"未知角色：{role}")
        with self._lock:
            self._require(person_id not in self.people, f"人员已存在：{person_id}", ConflictError)
            return self.store.append("person.registered", actor, {
                "person_id": person_id, "name": name, "role": role,
                "birth_year": birth_year,
            }, reason)

    def register_class(self, actor: str, class_id: str, name: str,
                       grade: str = "", *, reason: str = "") -> dict[str, Any]:
        self._require_staff(actor)
        with self._lock:
            self._require(class_id not in self.classes, f"班级已存在：{class_id}", ConflictError)
            return self.store.append("class.registered", actor,
                                     {"class_id": class_id, "name": name, "grade": grade}, reason)

    def add_class_member(self, actor: str, class_id: str, person_id: str,
                         *, reason: str = "") -> dict[str, Any]:
        self._require_staff(actor)
        with self._lock:
            self._require(class_id in self.classes, f"班级不存在：{class_id}", NotFoundError)
            person = self._person(person_id)
            self._require(person.role in {ROLE_STUDENT, ROLE_TEACHER},
                          "班级成员仅可为学生或教师")
            if person_id in self.memberships[class_id]:
                raise ConflictError(f"{person_id} 已在班级 {class_id}")
            return self.store.append("class.member_added", actor,
                                     {"class_id": class_id, "person_id": person_id}, reason)

    def remove_class_member(self, actor: str, class_id: str, person_id: str,
                            *, reason: str = "") -> dict[str, Any]:
        self._require_staff(actor)
        with self._lock:
            self._require(person_id in self.memberships.get(class_id, ()),
                          f"{person_id} 不在班级 {class_id}", NotFoundError)
            return self.store.append("class.member_removed", actor,
                                     {"class_id": class_id, "person_id": person_id}, reason)

    def link_guardian(self, actor: str, guardian_id: str, student_id: str,
                      *, reason: str = "") -> dict[str, Any]:
        """登记监护关系：家长只能看到被监护学生相关、且显式对家长发布的内容。"""
        self._require_staff(actor)
        with self._lock:
            guardian = self._person(guardian_id)
            student = self._person(student_id)
            self._require(guardian.role == ROLE_GUARDIAN, "监护方必须是监护人角色")
            self._require(student.role == ROLE_STUDENT, "被监护方必须是学生角色")
            if guardian_id in self.guardianships[student_id]:
                raise ConflictError("监护关系已存在")
            return self.store.append("guardian.linked", actor,
                                     {"guardian_id": guardian_id, "student_id": student_id}, reason)

    def mark_person_left(self, actor: str, person_id: str, *, reason: str = "") -> dict[str, Any]:
        """学生离校：凭证立即失效，但身份与历史讨论保留以维持引用不断链。"""
        self._require_staff(actor)
        with self._lock:
            person = self._person(person_id)
            self._require(person.active, "该人员已离校", ConflictError)
            return self.store.append("person.left", actor, {"person_id": person_id}, reason)

    # ----- 小组 -----------------------------------------------------------

    def create_group(self, actor: str, group_id: str, class_id: str, name: str,
                     *, reason: str = "") -> dict[str, Any]:
        self._require_staff(actor)
        with self._lock:
            self._require(class_id in self.classes, f"班级不存在：{class_id}", NotFoundError)
            self._require(group_id not in self.groups, f"小组已存在：{group_id}", ConflictError)
            return self.store.append("group.created", actor,
                                     {"group_id": group_id, "class_id": class_id, "name": name}, reason)

    def add_group_member(self, actor: str, group_id: str, person_id: str,
                         *, reason: str = "") -> dict[str, Any]:
        self._require_staff(actor)
        with self._lock:
            self._require(group_id in self.groups, f"小组不存在：{group_id}", NotFoundError)
            person = self._person(person_id)
            self._require(person.role == ROLE_STUDENT, "小组成员仅可为学生")
            if person_id in self.group_members[group_id]:
                raise ConflictError("已在该小组")
            return self.store.append("group.member_added", actor,
                                     {"group_id": group_id, "person_id": person_id}, reason)

    # ----- 阅读路线（可调、可复原）----------------------------------------

    def define_route(self, actor: str, route_id: str, class_id: str, name: str,
                     entries: list[dict[str, Any]], *, reason: str = "") -> dict[str, Any]:
        """为班级定义阅读路线；entries 为章节锚点的有序编排。

        每个 entry: {"chapter_id", "assign_version_id"（可选，指定班级用版本）}。
        """
        self._require_staff(actor)
        with self._lock:
            self._require(class_id in self.classes, f"班级不存在：{class_id}", NotFoundError)
            self._require(route_id not in self.routes, f"路线已存在：{route_id}", ConflictError)
            normalized = self._normalize_entries(entries)
            return self.store.append("route.defined", actor, {
                "route_id": route_id, "class_id": class_id,
                "name": name, "entries": normalized,
            }, reason)

    def adjust_route(self, actor: str, route_id: str, entries: list[dict[str, Any]],
                     *, name: Optional[str] = None, reason: str = "") -> dict[str, Any]:
        """调整路线：整体替换编排，旧编排保留在事件流中，可按时间点复原。"""
        self._require_staff(actor)
        with self._lock:
            self._require(route_id in self.routes, f"路线不存在：{route_id}", NotFoundError)
            normalized = self._normalize_entries(entries)
            return self.store.append("route.adjusted", actor, {
                "route_id": route_id, "name": name,
                "entries": normalized,
            }, reason)

    def _normalize_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._require(isinstance(entries, list) and entries, "路线至少包含一个章节")
        normalized: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            chapter_id = entry.get("chapter_id")
            self._require(chapter_id in self.chapters, f"路线第 {index + 1} 项章节不存在：{chapter_id}",
                          NotFoundError)
            assign_version = entry.get("assign_version_id")
            if assign_version:
                chapter = self.chapters[chapter_id]
                self._require(assign_version in self.versions,
                              f"指定版本不存在：{assign_version}", NotFoundError)
                self._require(self.versions[assign_version]["work_id"] == chapter["work_id"],
                              "路线指定版本与章节不属于同一作品")
            normalized.append({
                "chapter_id": chapter_id,
                "assign_version_id": assign_version,
            })
        return normalized

    # ----- 观点与发布决定 -------------------------------------------------

    def create_viewpoint(self, actor: str, viewpoint_id: str, audience: str,
                         anchor: dict[str, Any], body: str, *,
                         group_id: Optional[str] = None, class_id: Optional[str] = None,
                         min_age: int = 0, evidence: Optional[list[dict[str, Any]]] = None,
                         publish: bool = False, reason: str = "") -> dict[str, Any]:
        """创建观点（个人 / 小组 / 班级 / 家长）。publish=True 时直接发布。"""
        with self._lock:
            person = self._person(actor)
            self._require(person.active, "已离校账号不能新建观点", PermissionError)
            self._require(audience in {AUDIENCE_PERSONAL, AUDIENCE_GROUP,
                                       AUDIENCE_CLASS, AUDIENCE_GUARDIANS},
                          f"未知受众：{audience}")
            resolved_anchor = self._resolve_anchor(anchor)
            work_id = self.versions[resolved_anchor.version_id]["work_id"]

            if audience == AUDIENCE_GROUP:
                self._require(group_id, "小组观点必须指定 group_id")
                group = self.groups.get(group_id)
                self._require(group is not None, "小组不存在", NotFoundError)
                class_id = group["class_id"]
                if person.role == ROLE_STUDENT:
                    self._require(actor in self.group_members[group_id],
                                  "只有本组成员能代表小组发表观点", PermissionError)
            elif audience == AUDIENCE_CLASS:
                self._require(class_id, "全班观点必须指定 class_id")
                self._require(class_id in self.classes, "班级不存在", NotFoundError)
            elif audience == AUDIENCE_GUARDIANS:
                self._require(class_id, "对家长的发布必须指定 class_id")
                self._require(class_id in self.classes, "班级不存在", NotFoundError)
                self._require(person.role in _STAFF, "只有教师能向家长发布", PermissionError)
            else:
                class_id = class_id or self._primary_class(actor)

            resolved_evidence = [self._resolve_anchor(a) for a in (evidence or [])]

            self._require(viewpoint_id not in self.viewpoints,
                          f"观点已存在：{viewpoint_id}", ConflictError)
            event = self.store.append("viewpoint.created", actor, {
                "viewpoint_id": viewpoint_id, "audience": audience,
                "anchor": resolved_anchor.to_dict(), "work_id": work_id,
                "body": body, "group_id": group_id, "class_id": class_id,
                "min_age": int(min_age),
                "evidence": [a.to_dict() for a in resolved_evidence],
                "status": STATUS_PUBLISHED if publish else STATUS_DRAFT,
            }, reason)
            if publish:
                self.store.append("viewpoint.published", actor,
                                   {"viewpoint_id": viewpoint_id, "min_age": int(min_age)}, reason)
            return event

    def edit_viewpoint(self, actor: str, viewpoint_id: str, body: str, *,
                       reason: str = "") -> dict[str, Any]:
        """编辑正文；作者或教师可改，旧正文与决定留在历史里。"""
        with self._lock:
            view = self._live_viewpoint(viewpoint_id)
            person = self._person(actor)
            self._require(actor == view.author_id or person.role in _STAFF,
                          "只能编辑自己的观点", PermissionError)
            return self.store.append("viewpoint.edited", actor,
                                     {"viewpoint_id": viewpoint_id, "body": body}, reason)

    def publish_viewpoint(self, actor: str, viewpoint_id: str, *,
                          min_age: Optional[int] = None, reason: str = "") -> dict[str, Any]:
        """发布决定：草稿进入对受众可见状态。教师可设最低年龄门禁。"""
        with self._lock:
            view = self._live_viewpoint(viewpoint_id)
            person = self._person(actor)
            if actor != view.author_id and person.role not in _STAFF:
                raise PermissionError("只有作者或教师能发布该观点")
            if view.status == STATUS_PUBLISHED and min_age is None:
                raise ConflictError("观点已发布")
            effective_age = view.min_age if min_age is None else int(min_age)
            return self.store.append("viewpoint.published", actor, {
                "viewpoint_id": viewpoint_id, "min_age": effective_age,
            }, reason)

    def withdraw_viewpoint(self, actor: str, viewpoint_id: str, *,
                           reason: str = "") -> dict[str, Any]:
        """撤回批注：对所有人立即不可见，但事件保留，可审计、可复原。"""
        with self._lock:
            view = self._live_viewpoint(viewpoint_id)
            person = self._person(actor)
            self._require(actor == view.author_id or person.role in _STAFF,
                          "只有作者或教师能撤回该观点", PermissionError)
            self._require(view.status == STATUS_PUBLISHED, "只能撤回已发布观点", ConflictError)
            return self.store.append("viewpoint.withdrawn", actor,
                                     {"viewpoint_id": viewpoint_id}, reason)

    def restore_viewpoint(self, actor: str, viewpoint_id: str, *,
                          reason: str = "") -> dict[str, Any]:
        """恢复被撤回的观点（纠正误撤），本身也是一条发布决定。"""
        self._require_staff(actor)
        with self._lock:
            view = self._live_viewpoint(viewpoint_id)
            self._require(view.status == STATUS_WITHDRAWN, "观点不处于撤回状态", ConflictError)
            return self.store.append("viewpoint.restored", actor,
                                     {"viewpoint_id": viewpoint_id}, reason)

    def supersede_viewpoint(self, actor: str, old_id: str, new_id: str,
                            anchor: dict[str, Any], body: str, *,
                            min_age: Optional[int] = None, reason: str = "") -> dict[str, Any]:
        """教师发布一条替代观点（如换教材后重新落位）；旧观点保留并标注被接替。"""
        with self._lock:
            old = self._live_viewpoint(old_id)
            self._require_staff(actor)
            new_anchor = self._resolve_anchor(anchor)
            self._require(new_id not in self.viewpoints, f"观点已存在：{new_id}", ConflictError)
            effective_age = old.min_age if min_age is None else int(min_age)
            self.store.append("viewpoint.created", actor, {
                "viewpoint_id": new_id, "audience": old.audience,
                "anchor": new_anchor.to_dict(),
                "work_id": self.versions[new_anchor.version_id]["work_id"],
                "body": body, "group_id": old.group_id, "class_id": old.class_id,
                "min_age": effective_age, "evidence": [],
                "status": STATUS_PUBLISHED, "supersedes": old_id,
            }, reason)
            self.store.append("viewpoint.published", actor,
                               {"viewpoint_id": new_id, "min_age": effective_age}, reason)
            return self.store.append("viewpoint.superseded", actor,
                                      {"viewpoint_id": old_id, "replaced_by": new_id}, reason)

    def _live_viewpoint(self, viewpoint_id: str) -> Viewpoint:
        view = self.viewpoints.get(viewpoint_id)
        if view is None or view.deleted:
            raise NotFoundError(f"观点不存在：{viewpoint_id}")
        return view

    def _primary_class(self, person_id: str) -> Optional[str]:
        for class_id, members in self.memberships.items():
            if person_id in members:
                return class_id
        return None

    # ---------------------------------------------------------------------
    # 查询：可见性
    # ---------------------------------------------------------------------

    def can_view(self, viewpoint_id: str, viewer_id: str, *,
                 current_year: Optional[int] = None) -> tuple[bool, str]:
        """判定某读者当前能否看到某观点，返回 (是否可见, 原因码)。"""
        view = self.viewpoints.get(viewpoint_id)
        if view is None or view.deleted:
            return False, DENY_NOT_FOUND
        viewer = self.people.get(viewer_id)
        if viewer is None:
            return False, DENY_NOT_FOUND
        if not viewer.active:
            return False, DENY_LEFT
        return self._visibility(view, viewer, current_year)

    def _visibility(self, view: Viewpoint, viewer: Person,
                    current_year: Optional[int]) -> tuple[bool, str]:
        if viewer.role in _STAFF:
            # 教师/管理员：同班教师可见全部状态（含草稿、已撤回）以便审校
            if view.class_id and viewer.id in self.memberships.get(view.class_id, ()):
                return True, "ok"
            if view.author_id == viewer.id:
                return True, "ok"
            # 非本班教师：只可见已发布内容
            if view.status != STATUS_PUBLISHED:
                return False, DENY_DRAFT if view.status == STATUS_DRAFT else DENY_WITHDRAWN
            return self._published_scope(view, viewer, current_year)

        if view.status == STATUS_DRAFT:
            if view.author_id == viewer.id:
                return True, "ok"
            return False, DENY_DRAFT
        if view.status == STATUS_WITHDRAWN:
            return False, DENY_WITHDRAWN

        return self._published_scope(view, viewer, current_year)

    def _published_scope(self, view: Viewpoint, viewer: Person,
                         current_year: Optional[int]) -> tuple[bool, str]:
        """已发布观点按受众、班级、年龄、监护关系过滤。"""
        # 年龄门禁（任何角色都受限；家长按自身年龄视为成年，门禁主要约束学生）
        if view.min_age and viewer.role == ROLE_STUDENT:
            age = self._age_of(viewer, current_year)
            if age is not None and age < view.min_age:
                return False, DENY_AGE

        if viewer.id == view.author_id:
            return True, "ok"

        if viewer.role == ROLE_GUARDIAN:
            # 家长：必须有被监护学生与该观点同班级，且观点显式面向家长或全班
            if view.audience not in {AUDIENCE_GUARDIANS, AUDIENCE_CLASS}:
                return False, DENY_SCOPE
            for student_id in self._ward_students(viewer.id):
                student = self.people.get(student_id)
                if student and student.active and view.class_id \
                        and student_id in self.memberships.get(view.class_id, ()):
                    return True, "ok"
            return False, DENY_SCOPE

        # 学生
        if view.audience == AUDIENCE_PERSONAL:
            return False, DENY_SCOPE
        if not view.class_id or viewer.id not in self.memberships.get(view.class_id, ()):
            return False, DENY_SCOPE
        if view.audience == AUDIENCE_GROUP:
            if viewer.id not in self.group_members.get(view.group_id, ()):
                return False, DENY_SCOPE
        return True, "ok"

    def _ward_students(self, guardian_id: str) -> set[str]:
        return {sid for sid, guardians in self.guardianships.items() if guardian_id in guardians}

    @staticmethod
    def _age_of(person: Person, current_year: Optional[int]) -> Optional[int]:
        if not current_year or not person.birth_year:
            return None
        return current_year - person.birth_year

    def visible_viewpoints(self, viewer_id: str, *, work_id: Optional[str] = None,
                           chapter_path: Optional[str] = None,
                           current_year: Optional[int] = None) -> list[Viewpoint]:
        """列出读者当前可见的观点；可按作品、章节路径过滤。"""
        result: list[Viewpoint] = []
        for view in self.viewpoints.values():
            if work_id and view.work_id != work_id:
                continue
            if chapter_path and view.anchor.chapter_path != chapter_path:
                continue
            ok, _reason = self.can_view(view.id, viewer_id, current_year=current_year)
            if ok:
                result.append(view)
        result.sort(key=lambda v: (v.anchor.chapter_path, v.id))
        return result

    # ---------------------------------------------------------------------
    # 查询：时间点复原（教师调路线 / 撤回 / 换教材后仍可还原）
    # ---------------------------------------------------------------------

    def snapshot_at(self, as_of_seq: int) -> "ReadingArchive":
        """取截至某条事件（含）的只读投影，用于复原"过去班级看到的内容"。"""
        as_of_seq = max(0, min(as_of_seq, len(self.store.events)))
        projection = ReadingArchive(EventStore(clock=self.store._clock).seed(
            self.store.events[:as_of_seq]))
        return projection

    def route_history(self, route_id: str) -> list[dict[str, Any]]:
        """路线的每次编排（含调整），按时间排列；任一时点的编排都可还原。"""
        history: list[dict[str, Any]] = []
        route = self.routes.get(route_id)
        if route:
            history.append({"since_seq": route["since_seq"], "name": route["name"],
                            "entries": list(self.route_entries[route_id])})
        return history

    # ---------------------------------------------------------------------
    # 查询：出处追溯
    # ---------------------------------------------------------------------

    def provenance(self, viewpoint_id: str) -> dict[str, Any]:
        """任一观点的完整出处：作者、依据文本版本、证据、发布决定链、编辑史。"""
        view = self._live_viewpoint(viewpoint_id)
        version = self.versions.get(view.anchor.version_id, {})
        work = self.works.get(view.work_id, {})
        author = self.people.get(view.author_id)
        return {
            "viewpoint_id": view.id,
            "author": {"id": author.id, "name": author.name, "role": author.role}
                     if author else None,
            "author_active": author.active if author else False,
            "work": {"id": view.work_id, "title": work.get("title", "")},
            "version": {
                "id": view.anchor.version_id,
                "label": version.get("label", ""),
                "publisher": version.get("publisher", ""),
                "year": version.get("year"),
                "isbn": version.get("isbn", ""),
                "replaces": version.get("replaces"),
            },
            "anchor": view.anchor.to_dict(),
            "evidence": [a.to_dict() for a in view.evidence],
            "audience": view.audience,
            "group_id": view.group_id,
            "class_id": view.class_id,
            "min_age": view.min_age,
            "status": view.status,
            "supersedes": view.supersedes,
            "replaced_by": self._replaced_by(view.id),
            "decisions": list(view.decisions),
            "edit_history": list(view.edits),
        }

    def _replaced_by(self, viewpoint_id: str) -> Optional[str]:
        for view in self.viewpoints.values():
            if view.supersedes == viewpoint_id:
                return view.id
        return None

    # ---------------------------------------------------------------------
    # 学期导出：本班真正读过、引用过、讨论过的材料
    # ---------------------------------------------------------------------

    def export_class_term(self, actor: str, class_id: str, *,
                          current_year: Optional[int] = None,
                          reason: str = "") -> dict[str, Any]:
        """导出一个班的学期阅读档案。

        只包含本班实际接触的材料：
        - 路线编排过的章节与指定版本（真正读过）；
        - 本班观点证据引用过的锚点与版本（真正引用过）；
        - 本班产生的全部观点及其出处与发布决定（真正讨论过）。
        撤回的观点以 withdrawn 状态导出但不输出正文；离校学生的观点保留并标注。
        """
        self._require_staff(actor)
        with self._lock:
            self._require(class_id in self.classes, f"班级不存在：{class_id}", NotFoundError)
            current = self

        route_ids = [rid for rid, route in current.routes.items() if route["class_id"] == class_id]
        read_chapters: dict[str, dict[str, Any]] = {}
        used_versions: set[str] = set()
        for rid in route_ids:
            for seq_index, entry in enumerate(current.route_entries[rid]):
                chapter = current.chapters[entry["chapter_id"]]
                version_id = entry["assign_version_id"] or chapter["version_id"]
                used_versions.add(version_id)
                read_chapters.setdefault(chapter["chapter_path"], {
                    "chapter_id": chapter["chapter_id"],
                    "chapter_path": chapter["chapter_path"],
                    "title": chapter["title"],
                    "version_id": version_id,
                    "routes": [],
                })["routes"].append({"route_id": rid, "order": seq_index})

        class_viewpoints = [v for v in current.viewpoints.values()
                            if not v.deleted and v.class_id == class_id]
        referenced_versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        exported_views: list[dict[str, Any]] = []
        for view in sorted(class_viewpoints, key=lambda v: v.id):
            for anchor in [view.anchor, *view.evidence]:
                used_versions.add(anchor.version_id)
                referenced_versions[anchor.version_id].append({
                    "viewpoint_id": view.id,
                    "chapter_path": anchor.chapter_path,
                    "start": anchor.start,
                    "end": anchor.end,
                    "quote": anchor.quote,
                })
            author = current.people.get(view.author_id)
            exported_views.append({
                "id": view.id,
                "author_id": view.author_id,
                "author_name": author.name if author else None,
                "author_status": "active" if author and author.active else "left",
                "audience": view.audience,
                "group_id": view.group_id,
                "status": view.status,
                "min_age": view.min_age,
                "work_id": view.work_id,
                # 撤回内容不导出正文，只保留事实与决定链
                "body": view.body if view.status != STATUS_WITHDRAWN else None,
                "withdraw_reason": view.withdraw_reason,
                "anchor": view.anchor.to_dict(),
                "evidence": [a.to_dict() for a in view.evidence],
                "supersedes": view.supersedes,
                "replaced_by": current._replaced_by(view.id),
                "provenance": current.provenance(view.id),
            })

        exported_versions = []
        for version_id in sorted(used_versions):
            version = current.versions[version_id]
            exported_versions.append({
                "version_id": version_id,
                "work_id": version["work_id"],
                "label": version["label"],
                "publisher": version["publisher"],
                "year": version["year"],
                "isbn": version["isbn"],
                "replaces": version.get("replaces"),
                "citations": referenced_versions.get(version_id, []),
            })

        adaptations = [rw for rw in current.related.values()
                       if any(v.work_id == rw.source_work_id for v in class_viewpoints)]

        return {
            "class": {"class_id": class_id, "name": current.classes[class_id]["name"],
                      "grade": current.classes[class_id]["grade"]},
            "routes": [{"route_id": rid,
                        "name": current.routes[rid]["name"],
                        "entries": current.route_entries[rid]} for rid in sorted(route_ids)],
            "read_chapters": sorted(read_chapters.values(), key=lambda c: c["chapter_path"]),
            "versions": exported_versions,
            "adaptations": [{"id": rw.id, "title": rw.title, "kind": rw.kind,
                             "relation": rw.relation, "source_work_id": rw.source_work_id,
                             "note": rw.note, "metadata": rw.metadata} for rw in adaptations],
            "viewpoints": exported_views,
            "members": sorted(current.memberships[class_id]),
            "event_seq_range": {"from": 1, "to": len(current.store.events)},
        }

    # ---------------------------------------------------------------------
    # 投影：事件 -> 状态
    # ---------------------------------------------------------------------

    def _apply(self, event: dict[str, Any]) -> None:  # noqa: C901 - 事件分派集中一处
        data = event["data"]
        etype = event["type"]
        ts = event["ts"]

        if etype == "work.registered":
            self.works[data["work_id"]] = {
                "work_id": data["work_id"], "title": data["title"], "author": data.get("author", ""),
            }
        elif etype == "version.registered":
            self.versions[data["version_id"]] = dict(data)
        elif etype == "chapter.registered":
            self.chapters[data["chapter_id"]] = dict(data)
            self.anchor_by_chapter[(data["version_id"], data["chapter_path"])] = data["chapter_id"]

        elif etype == "related.registered":
            rw = RelatedWork(
                id=data["related_id"], title=data["title"], kind=data["kind"],
                relation=data["relation"], source_work_id=data["source_work_id"],
                note=data.get("note", ""), metadata=data.get("metadata", {}),
            )
            self.related[rw.id] = rw

        elif etype == "person.registered":
            self.people[data["person_id"]] = Person(
                id=data["person_id"], name=data["name"], role=data["role"],
                birth_year=data.get("birth_year"), active=True,
            )
        elif etype == "person.left":
            person = self.people[data["person_id"]]
            self.people[data["person_id"]] = Person(
                id=person.id, name=person.name, role=person.role,
                birth_year=person.birth_year, active=False, left_at=ts,
            )

        elif etype == "class.registered":
            self.classes[data["class_id"]] = {
                "class_id": data["class_id"], "name": data["name"], "grade": data.get("grade", ""),
            }
        elif etype == "class.member_added":
            self.memberships[data["class_id"]].add(data["person_id"])
        elif etype == "class.member_removed":
            self.memberships[data["class_id"]].discard(data["person_id"])

        elif etype == "guardian.linked":
            self.guardianships[data["student_id"]].add(data["guardian_id"])

        elif etype == "group.created":
            self.groups[data["group_id"]] = {
                "group_id": data["group_id"], "class_id": data["class_id"], "name": data["name"],
            }
        elif etype == "group.member_added":
            self.group_members[data["group_id"]].add(data["person_id"])

        elif etype == "route.defined":
            self.routes[data["route_id"]] = {
                "route_id": data["route_id"], "class_id": data["class_id"],
                "name": data["name"], "since_seq": event["seq"],
            }
            self.route_entries[data["route_id"]] = [dict(e) for e in data["entries"]]
        elif etype == "route.adjusted":
            if data.get("name"):
                self.routes[data["route_id"]]["name"] = data["name"]
            self.route_entries[data["route_id"]] = [dict(e) for e in data["entries"]]

        elif etype == "viewpoint.created":
            view = Viewpoint(
                id=data["viewpoint_id"],
                author_id=event["actor"],
                audience=data["audience"],
                work_id=data["work_id"],
                anchor=Anchor.from_dict(data["anchor"]),
                body=data.get("body", ""),
                group_id=data.get("group_id"),
                class_id=data.get("class_id"),
                min_age=int(data.get("min_age", 0)),
                status=data.get("status", STATUS_DRAFT),
                created_at=ts,
                published_at=ts if data.get("status") == STATUS_PUBLISHED else None,
                evidence=[Anchor.from_dict(a) for a in data.get("evidence", [])],
                supersedes=data.get("supersedes"),
            )
            view.decisions.append({"seq": event["seq"], "ts": ts, "action": "created",
                                   "by": event["actor"], "reason": event.get("reason", "")})
            self.viewpoints[view.id] = view
        elif etype == "viewpoint.published":
            view = self.viewpoints[data["viewpoint_id"]]
            view.status = STATUS_PUBLISHED
            view.published_at = ts
            if data.get("min_age") is not None:
                view.min_age = int(data["min_age"])
            view.decisions.append({"seq": event["seq"], "ts": ts, "action": "published",
                                   "by": event["actor"], "min_age": view.min_age,
                                   "reason": event.get("reason", "")})
        elif etype == "viewpoint.edited":
            view = self.viewpoints[data["viewpoint_id"]]
            view.edits.append({"seq": event["seq"], "ts": ts, "by": event["actor"],
                               "old_body": view.body, "reason": event.get("reason", "")})
            view.body = data["body"]
        elif etype == "viewpoint.withdrawn":
            view = self.viewpoints[data["viewpoint_id"]]
            view.status = STATUS_WITHDRAWN
            view.withdrawn_at = ts
            view.withdraw_reason = event.get("reason", "")
            view.decisions.append({"seq": event["seq"], "ts": ts, "action": "withdrawn",
                                   "by": event["actor"], "reason": event.get("reason", "")})
        elif etype == "viewpoint.restored":
            view = self.viewpoints[data["viewpoint_id"]]
            view.status = STATUS_PUBLISHED
            view.withdrawn_at = None
            view.withdraw_reason = ""
            view.decisions.append({"seq": event["seq"], "ts": ts, "action": "restored",
                                   "by": event["actor"], "reason": event.get("reason", "")})
        elif etype == "viewpoint.superseded":
            view = self.viewpoints[data["viewpoint_id"]]
            view.decisions.append({"seq": event["seq"], "ts": ts, "action": "superseded",
                                   "by": event["actor"],
                                   "replaced_by": data["replaced_by"],
                                   "reason": event.get("reason", "")})

    # ---------------------------------------------------------------------
    # 便捷序列化
    # ---------------------------------------------------------------------

    @staticmethod
    def viewpoint_to_dict(view: Viewpoint) -> dict[str, Any]:
        return {
            "id": view.id, "author_id": view.author_id, "audience": view.audience,
            "work_id": view.work_id, "anchor": view.anchor.to_dict(), "body": view.body,
            "group_id": view.group_id, "class_id": view.class_id, "min_age": view.min_age,
            "status": view.status, "created_at": view.created_at,
            "published_at": view.published_at, "withdrawn_at": view.withdrawn_at,
            "evidence": [a.to_dict() for a in view.evidence],
            "supersedes": view.supersedes,
        }
