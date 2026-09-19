"""名著多角度阅读档案——领域服务。

设计要点
========
* **版本与锚点**：观点引用的不是"第 X 页"这种会随纸书版本漂移的位置，而是
  注册在具体作品版本（edition）上的章节锚点（anchor）。锚点带章回号、定位
  与起讫文字，可挂跨版本别名；旧教材被新版本取代（supersede）后，历史锚点
  依然有效，引用不会断。
* **冲突解释共存**：每个观点（viewpoint）是独立实体，各自携带证据锚点，
  从不互相覆盖；状态只由"发布决定"事件推进（提交→发布/退回→撤回/撤回发布），
  完整决定链可追溯。
* **可见范围**：由班级成员关系、年龄（作品适读年龄）与监护关系共同决定；
  离校学生身份立即停用，但其观点、评论与证据原样保留，讨论不断链。
* **可复原**：所有状态来自只追加事件，任何查询都可带 as_of 回到历史时点；
  学期导出固化"读过、引用过、讨论过"的材料与每条观点的完整履历。
* **改编作品**：只登记书目元数据及其与原文锚点的关联，不导入正文、不另立锚点。
"""

from eventlog import EventLog, utc_now

# ---- 常量 ---------------------------------------------------------------

ROLE_STUDENT = "student"
ROLE_TEACHER = "teacher"
ROLE_GUARDIAN = "guardian"
ROLES = {ROLE_STUDENT, ROLE_TEACHER, ROLE_GUARDIAN}

WORK_ORIGINAL = "original"
WORK_ADAPTATION = "adaptation"

SCOPE_CLASS = "class"
SCOPE_GRADE = "grade"
SCOPE_GUARDIANS = "guardians"
AUDIENCE_SCOPES = {SCOPE_CLASS, SCOPE_GRADE, SCOPE_GUARDIANS}

# 发布状态机：当前状态 -> 允许的决定
STATE_TRANSITIONS = {
    "draft": {"submitted"},
    "submitted": {"published", "draft", "withdrawn"},
    "published": {"withheld", "withdrawn"},
    "withheld": {"published", "withdrawn"},
    "withdrawn": set(),
}
DECISION_KINDS = {
    "submitted": "submitted",
    "published": "published",
    "draft": "returned",      # 教师退回 -> 回到草稿
    "withheld": "withheld",
    "withdrawn": "withdrawn",
}


# ---- 异常 ---------------------------------------------------------------

class ArchiveError(ValueError):
    """所有领域错误的基类。"""


class NotFoundError(ArchiveError):
    """实体不存在，或观察者无权感知其存在（避免暴露未发布内容）。"""


class ValidationError(ArchiveError):
    """输入或状态机校验失败。"""


class AccessDeniedError(ArchiveError):
    """身份存在但无权执行该操作（如离校、非教师）。"""


# ---- 领域服务 -----------------------------------------------------------

class ReadingArchive:
    """命令（写）与查询（读）两类方法；写入全部落为事件。"""

    def __init__(self, log=None, clock=utc_now):
        self.clock = clock
        self.log = log or EventLog(clock=clock)

    def _append(self, stream, kind, payload, at=None):
        return self.log.append(stream, kind, payload, at=at)

    def _snapshot(self, as_of=None):
        return _replay(self.log, as_of)

    @staticmethod
    def _new_id(prefix):
        import uuid
        return f"{prefix}_{uuid.uuid4().hex[:10]}"

    # ===== 作品、版本与章节锚点 ==========================================

    def register_work(self, work_id, title, author, *,
                      kind=WORK_ORIGINAL, min_age=None, at=None):
        """登记原著作品。kind=adaptation 时请改用 register_adaptation。"""
        st = self._snapshot()
        if work_id in st.works:
            raise ValidationError(f"作品已存在：{work_id}")
        if kind not in (WORK_ORIGINAL, WORK_ADAPTATION):
            raise ValidationError("作品类型只能是 original/adaptation")
        self._append(f"work:{work_id}", "WorkRegistered", {
            "work_id": work_id, "title": title, "author": author,
            "kind": kind, "min_age": min_age,
        }, at=at)
        return work_id

    def register_adaptation(self, work_id, title, author, source_work_id, *,
                            bibliographic_note="", min_age=None, at=None):
        """登记改编作品（如《悟空传》）：仅元数据 + 与原著的关联，无正文。"""
        st = self._snapshot()
        if work_id in st.works:
            raise ValidationError(f"作品已存在：{work_id}")
        source = st.works.get(source_work_id)
        if not source:
            raise NotFoundError(f"原著不存在：{source_work_id}")
        if source["kind"] != WORK_ORIGINAL:
            raise ValidationError("改编关系只能指向原著作品")
        self._append(f"work:{work_id}", "AdaptationRegistered", {
            "work_id": work_id, "title": title, "author": author,
            "kind": WORK_ADAPTATION, "min_age": min_age,
            "source_work_id": source_work_id,
            "bibliographic_note": bibliographic_note,
        }, at=at)
        return work_id

    def add_adaptation_link(self, link_id, adaptation_work_id, anchor_id, *,
                            adapted_locator="", note="", actor_id=None, at=None):
        """登记改编作品某处与原文章节锚点的关联（对照讨论的依据）。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        work = st.works.get(adaptation_work_id)
        if not work or work["kind"] != WORK_ADAPTATION:
            raise NotFoundError("改编作品不存在")
        anchor = st.anchors.get(anchor_id)
        if not anchor:
            raise NotFoundError(f"原文锚点不存在：{anchor_id}")
        anchor_work = st.works.get(anchor["work_id"])
        if not anchor_work or anchor_work["kind"] != WORK_ORIGINAL:
            raise ValidationError("改编关联只能指向原著作品的章节锚点")
        if link_id in st.adaptation_links:
            raise ValidationError(f"关联已存在：{link_id}")
        self._append(f"link:{link_id}", "AdaptationLinkAdded", {
            "link_id": link_id,
            "adaptation_work_id": adaptation_work_id,
            "anchor_id": anchor_id,
            "adapted_locator": adapted_locator,
            "note": note, "created_by": actor_id,
        }, at=at)
        return link_id

    def add_edition(self, edition_id, work_id, name, *, publisher="", year=None,
                    bibliographic_note="", external_ref="", actor_id=None, at=None):
        """登记纸书/教材版本（不同学生手中的不同印本各自成版本）。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        if edition_id in st.editions:
            raise ValidationError(f"版本已存在：{edition_id}")
        if work_id not in st.works:
            raise NotFoundError(f"作品不存在：{work_id}")
        if st.works[work_id]["kind"] == WORK_ADAPTATION:
            raise ValidationError(
                "改编作品只登记元数据与原文关联，不另立版本和章节锚点")
        self._append(f"edition:{edition_id}", "EditionAdded", {
            "edition_id": edition_id, "work_id": work_id, "name": name,
            "publisher": publisher, "year": year,
            "bibliographic_note": bibliographic_note,
            "external_ref": external_ref,
            "created_by": actor_id,
        }, at=at)
        return edition_id

    def supersede_edition(self, edition_id, by_edition_id, *, reason="",
                          actor_id=None, at=None):
        """换用新版教材：旧版标记被取代，但事件与锚点全部保留，历史可复原。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        old = st.editions.get(edition_id)
        new = st.editions.get(by_edition_id)
        if not old or not new:
            raise NotFoundError("版本不存在")
        if old["work_id"] != new["work_id"]:
            raise ValidationError("新旧版本必须属于同一作品")
        if old.get("superseded_by"):
            raise ValidationError("该版本已被取代")
        self._append(f"edition:{edition_id}", "EditionSuperseded", {
            "edition_id": edition_id, "by_edition_id": by_edition_id,
            "reason": reason, "by": actor_id,
        }, at=at)

    def register_anchor(self, anchor_id, edition_id, chapter_no, chapter_title, *,
                        locator="", quote_start="", quote_end="",
                        actor_id=None, at=None):
        """在具体版本上注册章节锚点——全系统引用位置的稳定着落点。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        edition = st.editions.get(edition_id)
        if not edition:
            raise NotFoundError(f"版本不存在：{edition_id}")
        if anchor_id in st.anchors:
            raise ValidationError(f"锚点已存在：{anchor_id}")
        self._append(f"anchor:{anchor_id}", "ChapterAnchorRegistered", {
            "anchor_id": anchor_id, "edition_id": edition_id,
            "work_id": edition["work_id"],
            "chapter_no": chapter_no, "chapter_title": chapter_title,
            "locator": locator,
            "quote_start": quote_start, "quote_end": quote_end,
            "created_by": actor_id,
        }, at=at)
        return anchor_id

    def add_anchor_alias(self, anchor_id, alias_kind, value, *,
                         actor_id=None, at=None):
        """为锚点挂跨版本别名，如 volume/page（不同纸书的卷次、页码）。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        if anchor_id not in st.anchors:
            raise NotFoundError(f"锚点不存在：{anchor_id}")
        if alias_kind not in {"volume", "chapter", "page", "custom"}:
            raise ValidationError("别名类型只能是 volume/chapter/page/custom")
        self._append(f"anchor:{anchor_id}", "AnchorAliasAdded", {
            "anchor_id": anchor_id, "alias_kind": alias_kind,
            "value": value, "by": actor_id,
        }, at=at)

    # ===== 用户、监护关系与离校 ==========================================

    def register_user(self, user_id, name, role, *, age=None, at=None):
        st = self._snapshot()
        if user_id in st.users:
            raise ValidationError(f"用户已存在：{user_id}")
        if role not in ROLES:
            raise ValidationError(f"角色必须是 {sorted(ROLES)}")
        self._append(f"user:{user_id}", "UserRegistered", {
            "user_id": user_id, "name": name, "role": role, "age": age,
        }, at=at)
        return user_id

    def link_guardian(self, student_id, guardian_id, *, at=None):
        """建立监护关系（决定家长可见范围）。"""
        st = self._snapshot()
        student = st.users.get(student_id)
        guardian = st.users.get(guardian_id)
        if not student or not guardian:
            raise NotFoundError("用户不存在")
        if student["role"] != ROLE_STUDENT:
            raise ValidationError("监护关系只能指向学生")
        if guardian["role"] != ROLE_GUARDIAN:
            raise ValidationError("监护人账号角色必须是 guardian")
        links = st.guardians.get(student_id, {})
        link = links.get(guardian_id)
        if link and not link.get("unlinked_at"):
            raise ValidationError("监护关系已存在")
        self._append(f"user:{student_id}", "GuardianLinked", {
            "student_id": student_id, "guardian_id": guardian_id,
        }, at=at)

    def unlink_guardian(self, student_id, guardian_id, *, at=None):
        st = self._snapshot()
        links = st.guardians.get(student_id, {})
        link = links.get(guardian_id)
        if not link or link.get("unlinked_at"):
            raise NotFoundError("有效监护关系不存在")
        self._append(f"user:{student_id}", "GuardianUnlinked", {
            "student_id": student_id, "guardian_id": guardian_id,
        }, at=at)

    def depart_user(self, user_id, *, reason="", actor_id=None, at=None):
        """学生离校：身份立即停用，不能再访问内部材料；其发言全部保留。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        user = st.users.get(user_id)
        if not user:
            raise NotFoundError("用户不存在")
        if user.get("departed_at"):
            raise ValidationError("该用户已离校")
        self._append(f"user:{user_id}", "UserDeparted", {
            "user_id": user_id, "reason": reason, "by": actor_id,
        }, at=at)

    # ===== 班级、成员与学期 ==============================================

    def create_class(self, class_id, name, grade, *, actor_id=None, at=None):
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        if class_id in st.classes:
            raise ValidationError(f"班级已存在：{class_id}")
        self._append(f"class:{class_id}", "ClassCreated", {
            "class_id": class_id, "name": name, "grade": grade,
            "created_by": actor_id,
        }, at=at)
        return class_id

    def add_enrollment(self, class_id, user_id, *, at=None):
        st = self._snapshot()
        cls = st.classes.get(class_id)
        user = st.users.get(user_id)
        if not cls or not user:
            raise NotFoundError("班级或用户不存在")
        member = cls["members"].get(user_id)
        if member and not member.get("left_at"):
            raise ValidationError("已在该班")
        self._append(f"class:{class_id}", "ClassEnrollmentAdded", {
            "class_id": class_id, "user_id": user_id,
        }, at=at)

    def end_enrollment(self, class_id, user_id, *, actor_id=None, at=None):
        """退班/毕业：成员资格结束，历史成员记录与其讨论保留。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        cls = st.classes.get(class_id)
        if not cls:
            raise NotFoundError("班级不存在")
        member = cls["members"].get(user_id)
        if not member or member.get("left_at"):
            raise NotFoundError("有效在班记录不存在")
        self._append(f"class:{class_id}", "ClassEnrollmentEnded", {
            "class_id": class_id, "user_id": user_id, "by": actor_id,
        }, at=at)

    def open_term(self, class_id, term_id, start_at, end_at=None, *,
                  actor_id=None, at=None):
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        cls = st.classes.get(class_id)
        if not cls:
            raise NotFoundError("班级不存在")
        if term_id in cls["terms"]:
            raise ValidationError("学期已存在")
        self._append(f"class:{class_id}", "ClassTermBound", {
            "class_id": class_id, "term_id": term_id,
            "start_at": start_at, "end_at": end_at, "by": actor_id,
        }, at=at)

    def close_term(self, class_id, term_id, *, end_at=None, actor_id=None, at=None):
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        cls = st.classes.get(class_id)
        term = cls["terms"].get(term_id) if cls else None
        if not term:
            raise NotFoundError("学期不存在")
        if term.get("closed"):
            raise ValidationError("学期已结束")
        self._append(f"class:{class_id}", "ClassTermClosed", {
            "class_id": class_id, "term_id": term_id,
            "end_at": end_at, "by": actor_id,
        }, at=at)

    # ===== 小组 ==========================================================

    def create_group(self, group_id, name, class_id, *, actor_id=None, at=None):
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        if class_id not in st.classes:
            raise NotFoundError("班级不存在")
        if group_id in st.groups:
            raise ValidationError("小组已存在")
        self._append(f"group:{group_id}", "GroupCreated", {
            "group_id": group_id, "name": name, "class_id": class_id,
            "created_by": actor_id,
        }, at=at)

    def add_group_member(self, group_id, user_id, *, at=None):
        st = self._snapshot()
        group = st.groups.get(group_id)
        if not group or user_id not in st.users:
            raise NotFoundError("小组或用户不存在")
        member = group["members"].get(user_id)
        if member and not member.get("removed_at"):
            raise ValidationError("已在该组")
        self._append(f"group:{group_id}", "GroupMemberAdded", {
            "group_id": group_id, "user_id": user_id,
        }, at=at)

    def remove_group_member(self, group_id, user_id, *, at=None):
        st = self._snapshot()
        group = st.groups.get(group_id)
        member = group["members"].get(user_id) if group else None
        if not member or member.get("removed_at"):
            raise NotFoundError("有效小组成员记录不存在")
        self._append(f"group:{group_id}", "GroupMemberRemoved", {
            "group_id": group_id, "user_id": user_id,
        }, at=at)

    # ===== 阅读路线 ======================================================

    def create_route(self, route_id, title, class_id, term_id, *,
                     actor_id=None, at=None):
        """教师为某班某学期创建阅读路线（后续可增删调序，履历全部保留）。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        cls = st.classes.get(class_id)
        if not cls:
            raise NotFoundError("班级不存在")
        if term_id not in cls["terms"]:
            raise NotFoundError("学期不存在")
        if route_id in st.routes:
            raise ValidationError("路线已存在")
        self._append(f"route:{route_id}", "RouteCreated", {
            "route_id": route_id, "title": title,
            "class_id": class_id, "term_id": term_id,
            "created_by": actor_id,
        }, at=at)
        return route_id

    def add_route_item(self, route_id, anchor_id, *, order_no=None,
                       stage_label="", actor_id=None, at=None):
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        route = st.routes.get(route_id)
        if not route:
            raise NotFoundError("路线不存在")
        if anchor_id not in st.anchors:
            raise NotFoundError(f"锚点不存在：{anchor_id}")
        existing = [it for it in route["items"].values()
                    if it["anchor_id"] == anchor_id and not it.get("removed_at")]
        if existing:
            raise ValidationError("该锚点已在路线中")
        if order_no is None:
            order_no = 1 + max(
                (it["order_no"] for it in route["items"].values()
                 if not it.get("removed_at")), default=0)
        item_id = self._new_id("ritem")
        self._append(f"route:{route_id}", "RouteItemAdded", {
            "route_id": route_id, "item_id": item_id,
            "anchor_id": anchor_id, "order_no": order_no,
            "stage_label": stage_label, "by": actor_id,
        }, at=at)
        return item_id

    def remove_route_item(self, route_id, item_id, *, actor_id=None, at=None):
        """调整路线：移除不等于删除，事件保留，as_of 可复原。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        route = st.routes.get(route_id)
        item = route["items"].get(item_id) if route else None
        if not item or item.get("removed_at"):
            raise NotFoundError("路线条目不存在")
        self._append(f"route:{route_id}", "RouteItemRemoved", {
            "route_id": route_id, "item_id": item_id, "by": actor_id,
        }, at=at)

    def reorder_route_item(self, route_id, item_id, order_no, *,
                           actor_id=None, at=None):
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        route = st.routes.get(route_id)
        item = route["items"].get(item_id) if route else None
        if not item or item.get("removed_at"):
            raise NotFoundError("路线条目不存在")
        self._append(f"route:{route_id}", "RouteItemReordered", {
            "route_id": route_id, "item_id": item_id,
            "order_no": order_no, "by": actor_id,
        }, at=at)

    def retire_route(self, route_id, *, reason="", actor_id=None, at=None):
        """停用路线（换路线）；历史班级所见仍可按 as_of 复原。"""
        st = self._snapshot()
        self._require_teacher(st, actor_id)
        route = st.routes.get(route_id)
        if not route:
            raise NotFoundError("路线不存在")
        if route.get("retired_at"):
            raise ValidationError("路线已停用")
        self._append(f"route:{route_id}", "RouteRetired", {
            "route_id": route_id, "reason": reason, "by": actor_id,
        }, at=at)

    # ===== 观点与证据 ====================================================

    def open_viewpoint(self, viewpoint_id, author_id, primary_anchor_id, summary,
                       *, group_id=None, at=None):
        """提出观点：必须锚定一个版本锚点，作者身份被永久记录。"""
        st = self._snapshot()
        author = self._require_active_user(st, author_id)
        if author["role"] == ROLE_GUARDIAN:
            raise AccessDeniedError("监护人不能提出观点")
        if primary_anchor_id not in st.anchors:
            raise NotFoundError(f"锚点不存在：{primary_anchor_id}")
        if viewpoint_id in st.viewpoints:
            raise ValidationError("观点已存在")
        if group_id is not None:
            group = st.groups.get(group_id)
            if not group:
                raise NotFoundError("小组不存在")
            if author["role"] != ROLE_TEACHER and not self._is_group_member(
                    st, group_id, author_id):
                raise AccessDeniedError("不是该小组成员")
        self._append(f"viewpoint:{viewpoint_id}", "ViewpointOpened", {
            "viewpoint_id": viewpoint_id, "author_id": author_id,
            "primary_anchor_id": primary_anchor_id,
            "group_id": group_id, "summary": summary,
        }, at=at)
        return viewpoint_id

    def add_evidence(self, viewpoint_id, anchor_id, actor_id, *,
                     quote="", note="", adaptation_link_id=None, at=None):
        """为观点追加证据：证据锚定具体版本，冲突观点各自持有各自的证据。"""
        st = self._snapshot()
        vp = self._require_viewpoint(st, viewpoint_id)
        self._require_contributor(st, vp, actor_id)
        if vp["status"] == "withdrawn":
            raise ValidationError("观点已撤回，不能再补充证据")
        if anchor_id not in st.anchors:
            raise NotFoundError(f"锚点不存在：{anchor_id}")
        if adaptation_link_id is not None:
            if adaptation_link_id not in st.adaptation_links:
                raise NotFoundError("改编关联不存在")
            link = st.adaptation_links[adaptation_link_id]
            if link["anchor_id"] != anchor_id:
                raise ValidationError("改编关联与证据锚点不一致")
        evidence_id = self._new_id("ev")
        self._append(f"viewpoint:{viewpoint_id}", "EvidenceAdded", {
            "viewpoint_id": viewpoint_id, "evidence_id": evidence_id,
            "anchor_id": anchor_id, "quote": quote, "note": note,
            "adaptation_link_id": adaptation_link_id, "by": actor_id,
        }, at=at)
        return evidence_id

    def remove_evidence(self, viewpoint_id, evidence_id, actor_id, *,
                        reason="", at=None):
        st = self._snapshot()
        vp = self._require_viewpoint(st, viewpoint_id)
        ev = vp["evidence"].get(evidence_id)
        if not ev or ev.get("removed_at"):
            raise NotFoundError("证据不存在")
        if not self._can_moderate(st, vp, actor_id) and ev["by"] != actor_id:
            raise AccessDeniedError("只能移除自己的证据，或由教师移除")
        self._append(f"viewpoint:{viewpoint_id}", "EvidenceRemoved", {
            "viewpoint_id": viewpoint_id, "evidence_id": evidence_id,
            "reason": reason, "by": actor_id,
        }, at=at)

    def add_comment(self, viewpoint_id, actor_id, body, *, at=None):
        st = self._snapshot()
        vp = self._require_viewpoint(st, viewpoint_id)
        viewer = self._require_active_user(st, actor_id)
        if viewer["role"] == ROLE_GUARDIAN:
            raise AccessDeniedError("监护人不能参与内部讨论")
        visible = self._can_see(st, vp, actor_id)
        if not visible:
            raise NotFoundError("观点不存在")
        if vp["status"] == "withdrawn":
            raise ValidationError("观点已撤回，讨论已冻结")
        if not self._is_collaborator(st, vp, actor_id):
            # 非作者/组员/教师：只允许对已发布观点讨论，且须与作者同班
            if vp["status"] != "published" or not self._shared_class(
                    st, vp["author_id"], actor_id):
                raise AccessDeniedError("只能讨论本班已发布的观点")
        comment_id = self._new_id("cmt")
        self._append(f"viewpoint:{viewpoint_id}", "CommentAdded", {
            "viewpoint_id": viewpoint_id, "comment_id": comment_id,
            "author_id": actor_id, "body": body,
        }, at=at)
        return comment_id

    def remove_comment(self, viewpoint_id, comment_id, actor_id, *,
                       reason="", at=None):
        st = self._snapshot()
        vp = self._require_viewpoint(st, viewpoint_id)
        comment = vp["comments"].get(comment_id)
        if not comment or comment.get("removed_at"):
            raise NotFoundError("评论不存在")
        if not self._can_moderate(st, vp, actor_id) and \
                comment["author_id"] != actor_id:
            raise AccessDeniedError("只能删除自己的评论，或由教师删除")
        self._append(f"viewpoint:{viewpoint_id}", "CommentRemoved", {
            "viewpoint_id": viewpoint_id, "comment_id": comment_id,
            "reason": reason, "by": actor_id,
        }, at=at)

    # ===== 发布决定链 ====================================================

    def submit(self, viewpoint_id, actor_id, *, at=None):
        """作者提交审校：draft -> submitted。"""
        self._decide(viewpoint_id, actor_id, "submitted", at=at,
                     actor_must="author")

    def return_viewpoint(self, viewpoint_id, actor_id, *, reason="", at=None):
        """教师退回：submitted -> draft。"""
        self._decide(viewpoint_id, actor_id, "returned", at=at,
                     reason=reason, actor_must="teacher")

    def publish(self, viewpoint_id, actor_id, scopes, *, at=None):
        """教师发布给受众（class/grade/guardians 可组合）。"""
        scopes = list(scopes)
        if not scopes or any(s not in AUDIENCE_SCOPES for s in scopes):
            raise ValidationError(
                f"发布范围必须是 {sorted(AUDIENCE_SCOPES)} 的非空组合")
        if len(scopes) != len(set(scopes)):
            raise ValidationError("发布范围重复")
        self._decide(viewpoint_id, actor_id, "published", at=at,
                     actor_must="teacher", extra={"scopes": scopes})

    def withhold(self, viewpoint_id, actor_id, *, reason="", at=None):
        """教师撤回发布：published -> withheld，家长/学生立即不可见，事件保留。"""
        self._decide(viewpoint_id, actor_id, "withheld", at=at,
                     reason=reason, actor_must="teacher")

    def withdraw(self, viewpoint_id, actor_id, *, reason="", at=None):
        """作者撤回（或教师代为撤回）：进入终态 withdrawn。"""
        self._decide(viewpoint_id, actor_id, "withdrawn", at=at,
                     reason=reason, actor_must="author_or_teacher")

    def _decide(self, viewpoint_id, actor_id, decision, *, at=None,
                reason="", actor_must="teacher", extra=None):
        st = self._snapshot()
        vp = self._require_viewpoint(st, viewpoint_id)
        actor = self._require_active_user(st, actor_id)
        if actor_must == "teacher":
            self._require_teacher(st, actor_id)
        elif actor_must == "author":
            if actor["role"] != ROLE_TEACHER and vp["author_id"] != actor_id:
                raise AccessDeniedError("只有作者本人可以提交")
        elif actor_must == "author_or_teacher":
            if actor["role"] != ROLE_TEACHER and vp["author_id"] != actor_id:
                raise AccessDeniedError("只有作者本人或教师可以撤回")
        target_state = {"submitted": "submitted", "published": "published",
                        "returned": "draft", "withheld": "withheld",
                        "withdrawn": "withdrawn"}[decision]
        allowed = STATE_TRANSITIONS[vp["status"]]
        if target_state not in allowed:
            raise ValidationError(
                f"当前状态 {vp['status']} 不允许决定 {decision}")
        payload = {"viewpoint_id": viewpoint_id, "decision": decision,
                   "target_state": target_state,
                   "reason": reason, "by": actor_id}
        if extra:
            payload.update(extra)
        self._append(f"viewpoint:{viewpoint_id}", "PublicationDecision", payload,
                     at=at)

    # ===== 查询 ==========================================================

    def list_works(self, *, kind=None, as_of=None):
        st = self._snapshot(as_of)
        works = sorted(st.works.values(), key=lambda w: w["work_id"])
        if kind is not None:
            works = [w for w in works if w["kind"] == kind]
        return [self._work_dict(w) for w in works]

    def get_viewpoint(self, viewpoint_id, viewer_id, *, as_of=None):
        """按观察者身份与 as_of 时点返回观点；无权感知时抛 NotFoundError。"""
        st = self._snapshot(as_of)
        viewer = st.users.get(viewer_id)
        if viewer is None:
            raise NotFoundError("用户不存在")
        if viewer.get("departed_at"):
            # 已认证但身份已停用（如离校）与"无权感知存在"明确区分
            raise AccessDeniedError("该身份已停用，不能访问内部材料")
        vp = st.viewpoints.get(viewpoint_id)
        if not vp:
            raise NotFoundError("观点不存在")
        if not self._can_see(st, vp, viewer_id):
            raise NotFoundError("观点不存在或尚未对你可见")
        return self._viewpoint_dict(st, vp, viewer_id)

    def list_viewpoints(self, viewer_id, *, class_id=None, anchor_id=None,
                        as_of=None):
        """列出观察者在 as_of 时点可见的观点，可按班级/锚点过滤。"""
        st = self._snapshot(as_of)
        result = []
        for vp in st.viewpoints.values():
            if anchor_id is not None and not self._references_anchor(vp, anchor_id):
                continue
            if class_id is not None and not self._viewpoint_in_class(st, vp, class_id):
                continue
            if self._can_see(st, vp, viewer_id):
                result.append(self._viewpoint_dict(st, vp, viewer_id))
        result.sort(key=lambda v: (v["created_at"], v["viewpoint_id"]))
        return result

    def get_anchor(self, anchor_id, *, as_of=None):
        st = self._snapshot(as_of)
        anchor = st.anchors.get(anchor_id)
        if not anchor:
            raise NotFoundError("锚点不存在")
        return self._anchor_dict(st, anchor)

    def get_route(self, route_id, *, as_of=None):
        st = self._snapshot(as_of)
        route = st.routes.get(route_id)
        if not route:
            raise NotFoundError("路线不存在")
        return self._route_dict(st, route, include_history=False)

    def provenance(self, viewpoint_id, *, actor_id=None, as_of=None):
        """完整溯源：出自谁、依据哪些文本版本、经过哪些发布决定。

        仅教师可调用（普通观察者请用 get_viewpoint）。
        """
        st = self._snapshot(as_of)
        self._require_teacher(st, actor_id)
        vp = st.viewpoints.get(viewpoint_id)
        if not vp:
            raise NotFoundError("观点不存在")
        return self._viewpoint_dict(st, vp, actor_id, full=True)

    # ===== 学期导出 ======================================================

    def export_term(self, class_id, term_id, *, actor_id, as_of=None):
        """导出本班本学期真正读过、引用过、讨论过的全部材料与观点履历。"""
        st = self._snapshot(as_of)
        self._require_teacher(st, actor_id)
        cls = st.classes.get(class_id)
        term = cls["terms"].get(term_id) if cls else None
        if not term:
            raise NotFoundError("班级或学期不存在")
        window_end = as_of or self.clock()

        # 本学期为本班服务过的路线（含已停用、已调整的完整履历）
        routes = [r for r in st.routes.values()
                  if r["class_id"] == class_id and r["term_id"] == term_id]
        route_anchor_ids = set()
        route_dicts = []
        for route in routes:
            for item in route["items"].values():
                if not item.get("removed_at"):
                    route_anchor_ids.add(item["anchor_id"])
            route_dicts.append(
                self._route_dict(st, route, include_history=True))

        # 学期内曾在班的成员
        member_ids = {
            uid for uid, m in cls["members"].items()
            if _intervals_overlap(m.get("joined_at"), m.get("left_at"),
                                  term["start_at"],
                                  term.get("end_at") or window_end)
        }

        cited_anchor_ids = set()
        discussed = []
        exported_vps = []
        for vp in st.viewpoints.values():
            if vp["author_id"] not in member_ids:
                continue
            anchors = {vp["primary_anchor_id"]}
            anchors.update(e["anchor_id"] for e in vp["evidence"].values())
            active_in_term = self._viewpoint_active_in_term(vp, term, window_end)
            touched_route = bool(anchors & route_anchor_ids)
            has_discussion = bool(vp["comments"]) or any(
                d["decision"] in ("submitted", "published", "withheld", "withdrawn")
                for d in vp["decisions"])
            if not (active_in_term or touched_route):
                continue
            cited_anchor_ids.update(
                e["anchor_id"] for e in vp["evidence"].values())
            if has_discussion:
                discussed.append(vp["viewpoint_id"])
            exported_vps.append(
                self._viewpoint_dict(st, vp, actor_id, full=True))

        material_anchor_ids = route_anchor_ids | cited_anchor_ids
        materials = [
            self._anchor_dict(st, st.anchors[aid])
            for aid in sorted(material_anchor_ids) if aid in st.anchors
        ]
        discussed_anchors = set()
        for vp in exported_vps:
            if vp["viewpoint_id"] in discussed:
                if vp.get("primary_anchor"):
                    discussed_anchors.add(vp["primary_anchor"]["anchor_id"])
                discussed_anchors.update(
                    e["anchor"]["anchor_id"] for e in vp["evidence"]
                    if e.get("anchor"))
        for m in materials:
            used_as = []
            if m["anchor_id"] in route_anchor_ids:
                used_as.append("read")
            if m["anchor_id"] in cited_anchor_ids:
                used_as.append("cited")
            if m["anchor_id"] in discussed_anchors:
                used_as.append("discussed")
            m["used_as"] = used_as

        edition_ids = {m["edition_id"] for m in materials}
        work_ids = {m["work_id"] for m in materials}
        link_ids = {e.get("adaptation_link_id") for vp in exported_vps
                    for e in vp["evidence"] if e.get("adaptation_link_id")}
        link_ids.discard(None)

        import uuid
        export_id = f"export_{uuid.uuid4().hex[:10]}"
        payload = {
            "export_id": export_id, "class_id": class_id, "term_id": term_id,
            "as_of": window_end, "by": actor_id,
            "counts": {
                "routes": len(routes), "materials": len(materials),
                "viewpoints": len(exported_vps),
                "discussed_viewpoints": len(discussed),
            },
        }
        self._append(f"class:{class_id}", "TermExported", payload, at=as_of)

        return {
            "export_id": export_id,
            "class_id": class_id,
            "term_id": term_id,
            "term": {"start_at": term["start_at"], "end_at": term.get("end_at")},
            "generated_as_of": window_end,
            "generated_by": actor_id,
            "works": [self._work_dict(st.works[wid]) for wid in sorted(work_ids)],
            "editions": [self._edition_dict(st.editions[eid])
                         for eid in sorted(edition_ids)],
            "materials": materials,
            "adaptation_links": [self._link_dict(st.adaptation_links[lid])
                                 for lid in sorted(link_ids)],
            "routes": route_dicts,
            "viewpoints": exported_vps,
            "coverage": {
                "read": len(route_anchor_ids),
                "cited": len(cited_anchor_ids),
                "discussed_viewpoints": len(discussed),
                "viewpoints": len(exported_vps),
            },
        }

    # ===== 权限与投影辅助 ================================================

    @staticmethod
    def _require_teacher(st, actor_id):
        if actor_id is None:
            raise AccessDeniedError("需要教师身份")
        actor = st.users.get(actor_id)
        if not actor:
            raise NotFoundError("用户不存在")
        if actor.get("departed_at"):
            raise AccessDeniedError("该身份已停用")
        if actor["role"] != ROLE_TEACHER:
            raise AccessDeniedError("该操作仅教师可执行")
        return actor

    @staticmethod
    def _require_active_user(st, user_id):
        user = st.users.get(user_id)
        if not user:
            raise NotFoundError("用户不存在")
        if user.get("departed_at"):
            raise AccessDeniedError("该用户已离校，身份已停用")
        return user

    @staticmethod
    def _require_viewpoint(st, viewpoint_id):
        vp = st.viewpoints.get(viewpoint_id)
        if not vp:
            raise NotFoundError("观点不存在")
        return vp

    @staticmethod
    def _is_member(st, class_id, user_id):
        cls = st.classes.get(class_id)
        if not cls:
            return False
        m = cls["members"].get(user_id)
        return bool(m and not m.get("left_at"))

    @staticmethod
    def _is_group_member(st, group_id, user_id):
        group = st.groups.get(group_id)
        if not group:
            return False
        m = group["members"].get(user_id)
        return bool(m and not m.get("removed_at"))

    def _is_collaborator(self, st, vp, user_id):
        user = st.users.get(user_id)
        if not user:
            return False
        if user["role"] == ROLE_TEACHER:
            return True
        if vp["author_id"] == user_id:
            return True
        if vp["group_id"] and self._is_group_member(
                st, vp["group_id"], user_id):
            return True
        return False

    def _require_contributor(self, st, vp, user_id):
        user = self._require_active_user(st, user_id)
        if not self._is_collaborator(st, vp, user_id):
            raise AccessDeniedError("只有作者、同组成员或教师可以补充证据")
        return user

    def _can_moderate(self, st, vp, user_id):
        user = st.users.get(user_id)
        return bool(user and user["role"] == ROLE_TEACHER
                    and not user.get("departed_at"))

    def _shared_class(self, st, a, b):
        return any(self._is_member(st, cid, a) and self._is_member(st, cid, b)
                   for cid in st.classes)

    def _can_see(self, st, vp, viewer_id):
        """核心可见性判定：身份状态 + 协作关系 + 发布范围 + 年龄 + 监护。"""
        viewer = st.users.get(viewer_id)
        if not viewer or viewer.get("departed_at"):
            return False  # 离校（含 as_of 时点已离校）即不再访问内部材料
        if viewer["role"] == ROLE_TEACHER:
            return True  # 教师审校需要看到全部状态
        if vp["author_id"] == viewer_id:
            return True
        if vp["group_id"] and self._is_group_member(
                st, vp["group_id"], viewer_id):
            return True
        if vp["status"] != "published":
            return False
        scopes = vp["audience_scopes"]
        author_class_ids = [
            cid for cid, cls in st.classes.items()
            if self._is_member(st, cid, vp["author_id"])]
        for scope in scopes:
            if scope == SCOPE_CLASS:
                if viewer["role"] == ROLE_STUDENT and any(
                        self._is_member(st, cid, viewer_id)
                        for cid in author_class_ids):
                    return True
            elif scope == SCOPE_GRADE:
                if viewer["role"] != ROLE_STUDENT:
                    continue
                anchor = st.anchors.get(vp["primary_anchor_id"])
                work = st.works.get(anchor["work_id"]) if anchor else None
                if not self._age_allows(work, viewer):
                    continue
                viewer_grades = {st.classes[cid]["grade"]
                                 for cid in st.classes
                                 if self._is_member(st, cid, viewer_id)}
                author_grades = {st.classes[cid]["grade"]
                                 for cid in author_class_ids}
                if viewer_grades & author_grades:
                    return True
            elif scope == SCOPE_GUARDIANS:
                # 作者必须仍在校；学生离校后其家长不再可见
                author = st.users.get(vp["author_id"])
                if not author or author.get("departed_at"):
                    continue
                links = st.guardians.get(vp["author_id"], {})
                link = links.get(viewer_id)
                if link and not link.get("unlinked_at"):
                    return True
        return False

    @staticmethod
    def _age_allows(work, viewer):
        min_age = work.get("min_age") if work else None
        if min_age is None or viewer.get("age") is None:
            return True
        return viewer["age"] >= min_age

    @staticmethod
    def _references_anchor(vp, anchor_id):
        if vp["primary_anchor_id"] == anchor_id:
            return True
        return any(e["anchor_id"] == anchor_id
                   for e in vp["evidence"].values())

    def _viewpoint_in_class(self, st, vp, class_id):
        if self._is_member(st, class_id, vp["author_id"]):
            return True
        group = st.groups.get(vp["group_id"]) if vp["group_id"] else None
        return bool(group and group["class_id"] == class_id)

    @staticmethod
    def _viewpoint_active_in_term(vp, term, window_end):
        start, end = term["start_at"], term.get("end_at") or window_end
        stamps = [vp["created_at"]]
        stamps.extend(e["at"] for e in vp["evidence"].values())
        stamps.extend(c["created_at"] for c in vp["comments"].values())
        stamps.extend(d["at"] for d in vp["decisions"])
        return any(start <= t <= end for t in stamps)

    # ---- 字典投影 -------------------------------------------------------

    @staticmethod
    def _work_dict(work):
        data = {
            "work_id": work["work_id"], "title": work["title"],
            "author": work["author"], "kind": work["kind"],
            "min_age": work.get("min_age"),
        }
        if work["kind"] == WORK_ADAPTATION:
            data["source_work_id"] = work.get("source_work_id")
            data["bibliographic_note"] = work.get("bibliographic_note", "")
        return data

    def _edition_dict(self, edition):
        return {
            "edition_id": edition["edition_id"],
            "work_id": edition["work_id"],
            "name": edition["name"],
            "publisher": edition.get("publisher", ""),
            "year": edition.get("year"),
            "bibliographic_note": edition.get("bibliographic_note", ""),
            "superseded_by": edition.get("superseded_by"),
        }

    def _anchor_dict(self, st, anchor):
        edition = st.editions.get(anchor["edition_id"], {})
        return {
            "anchor_id": anchor["anchor_id"],
            "edition_id": anchor["edition_id"],
            "work_id": anchor["work_id"],
            "chapter_no": anchor["chapter_no"],
            "chapter_title": anchor["chapter_title"],
            "locator": anchor.get("locator", ""),
            "quote_start": anchor.get("quote_start", ""),
            "quote_end": anchor.get("quote_end", ""),
            "aliases": [dict(a) for a in anchor.get("aliases", ())],
            "edition": self._edition_dict(edition) if edition else None,
        }

    @staticmethod
    def _link_dict(link):
        return dict(link)

    def _route_dict(self, st, route, include_history=False):
        items = sorted(
            (it for it in route["items"].values() if not it.get("removed_at")),
            key=lambda it: (it["order_no"], it["item_id"]))
        data = {
            "route_id": route["route_id"],
            "title": route["title"],
            "class_id": route["class_id"],
            "term_id": route["term_id"],
            "created_at": route["created_at"],
            "retired_at": route.get("retired_at"),
            "items": [{
                "item_id": it["item_id"],
                "anchor": self._anchor_dict(st, st.anchors[it["anchor_id"]]),
                "order_no": it["order_no"],
                "stage_label": it.get("stage_label", ""),
            } for it in items if it["anchor_id"] in st.anchors],
        }
        if include_history:
            data["history"] = [
                {"at": e.at, "kind": e.kind, **e.payload}
                for e in self.log.read(f"route:{route['route_id']}")
            ]
        return data

    def _evidence_dict(self, st, ev, full):
        data = {
            "evidence_id": ev["evidence_id"],
            "anchor": self._anchor_dict(st, st.anchors[ev["anchor_id"]])
                    if ev["anchor_id"] in st.anchors else None,
            "added_by": ev["by"], "at": ev["at"],
            "adaptation_link_id": ev.get("adaptation_link_id"),
            "removed_at": ev.get("removed_at"),
            "removed_by": ev.get("removed_by"),
        }
        if full or not ev.get("removed_at"):
            data["quote"] = ev.get("quote", "")
            data["note"] = ev.get("note", "")
        else:
            data["quote"] = None
            data["note"] = None
            data["removed"] = True
        return data

    def _comment_dict(self, cmt, full):
        data = {
            "comment_id": cmt["comment_id"],
            "author_id": cmt["author_id"],
            "created_at": cmt["created_at"],
            "removed_at": cmt.get("removed_at"),
            "removed_by": cmt.get("removed_by"),
        }
        if full or not cmt.get("removed_at"):
            data["body"] = cmt.get("body", "")
        else:
            data["body"] = None
            data["removed"] = True
        return data

    def _viewpoint_dict(self, st, vp, viewer_id, full=False):
        anchor = st.anchors.get(vp["primary_anchor_id"])
        return {
            "viewpoint_id": vp["viewpoint_id"],
            "summary": vp["summary"],
            "author_id": vp["author_id"],
            "author_name": st.users.get(vp["author_id"], {}).get("name"),
            "group_id": vp.get("group_id"),
            "primary_anchor": self._anchor_dict(st, anchor) if anchor else None,
            "status": vp["status"],
            "audience_scopes": list(vp["audience_scopes"]),
            "created_at": vp["created_at"],
            "evidence": [self._evidence_dict(st, ev, full)
                         for ev in sorted(vp["evidence"].values(),
                                          key=lambda e: e["at"])],
            "comments": [self._comment_dict(c, full)
                         for c in sorted(vp["comments"].values(),
                                         key=lambda c: c["created_at"])],
            "decisions": [dict(d) for d in vp["decisions"]],
        }


# ---- 事件重放（投影） ----------------------------------------------------

def _replay(log, as_of=None):
    st = _State()
    for e in log.all_events(as_of):
        _apply(st, e)
    return st


class _State:
    def __init__(self):
        self.works = {}
        self.editions = {}
        self.anchors = {}
        self.adaptation_links = {}
        self.users = {}
        self.guardians = {}   # student_id -> {guardian_id: {linked_at, unlinked_at}}
        self.classes = {}
        self.groups = {}
        self.routes = {}
        self.viewpoints = {}


def _apply(st, e):
    k, p = e.kind, e.payload

    if k in ("WorkRegistered", "AdaptationRegistered"):
        st.works[p["work_id"]] = {
            "work_id": p["work_id"], "title": p["title"],
            "author": p["author"], "kind": p["kind"],
            "min_age": p.get("min_age"),
            "source_work_id": p.get("source_work_id"),
            "bibliographic_note": p.get("bibliographic_note", ""),
        }
    elif k == "AdaptationLinkAdded":
        st.adaptation_links[p["link_id"]] = dict(p)
    elif k == "EditionAdded":
        st.editions[p["edition_id"]] = {
            "edition_id": p["edition_id"], "work_id": p["work_id"],
            "name": p["name"], "publisher": p.get("publisher", ""),
            "year": p.get("year"),
            "bibliographic_note": p.get("bibliographic_note", ""),
            "external_ref": p.get("external_ref", ""),
        }
    elif k == "EditionSuperseded":
        st.editions[p["edition_id"]]["superseded_by"] = p["by_edition_id"]
        st.editions[p["edition_id"]]["superseded_reason"] = p.get("reason", "")
    elif k == "ChapterAnchorRegistered":
        st.anchors[p["anchor_id"]] = {
            "anchor_id": p["anchor_id"], "edition_id": p["edition_id"],
            "work_id": p["work_id"], "chapter_no": p["chapter_no"],
            "chapter_title": p["chapter_title"],
            "locator": p.get("locator", ""),
            "quote_start": p.get("quote_start", ""),
            "quote_end": p.get("quote_end", ""),
            "aliases": [],
        }
    elif k == "AnchorAliasAdded":
        st.anchors[p["anchor_id"]]["aliases"].append(
            {"alias_kind": p["alias_kind"], "value": p["value"]})

    elif k == "UserRegistered":
        st.users[p["user_id"]] = {
            "user_id": p["user_id"], "name": p["name"], "role": p["role"],
            "age": p.get("age"), "departed_at": None,
        }
    elif k == "GuardianLinked":
        st.guardians.setdefault(p["student_id"], {})[p["guardian_id"]] = {
            "linked_at": e.at, "unlinked_at": None}
    elif k == "GuardianUnlinked":
        st.guardians[p["student_id"]][p["guardian_id"]]["unlinked_at"] = e.at
    elif k == "UserDeparted":
        st.users[p["user_id"]]["departed_at"] = e.at

    elif k == "ClassCreated":
        st.classes[p["class_id"]] = {
            "class_id": p["class_id"], "name": p["name"],
            "grade": p["grade"], "members": {}, "terms": {}, "exports": [],
        }
    elif k == "ClassEnrollmentAdded":
        st.classes[p["class_id"]]["members"][p["user_id"]] = {
            "joined_at": e.at, "left_at": None}
    elif k == "ClassEnrollmentEnded":
        st.classes[p["class_id"]]["members"][p["user_id"]]["left_at"] = e.at
    elif k == "ClassTermBound":
        st.classes[p["class_id"]]["terms"][p["term_id"]] = {
            "start_at": p["start_at"], "end_at": p.get("end_at"),
            "closed": False}
    elif k == "ClassTermClosed":
        term = st.classes[p["class_id"]]["terms"][p["term_id"]]
        term["closed"] = True
        term["end_at"] = p.get("end_at") or e.at
    elif k == "TermExported":
        st.classes[p["class_id"]]["exports"].append(dict(p))

    elif k == "GroupCreated":
        st.groups[p["group_id"]] = {
            "group_id": p["group_id"], "name": p["name"],
            "class_id": p["class_id"], "members": {}}
    elif k == "GroupMemberAdded":
        st.groups[p["group_id"]]["members"][p["user_id"]] = {
            "added_at": e.at, "removed_at": None}
    elif k == "GroupMemberRemoved":
        st.groups[p["group_id"]]["members"][p["user_id"]]["removed_at"] = e.at

    elif k == "RouteCreated":
        st.routes[p["route_id"]] = {
            "route_id": p["route_id"], "title": p["title"],
            "class_id": p["class_id"], "term_id": p["term_id"],
            "created_at": e.at, "items": {},
            "retired_at": None,
        }
    elif k == "RouteItemAdded":
        st.routes[p["route_id"]]["items"][p["item_id"]] = {
            "item_id": p["item_id"], "anchor_id": p["anchor_id"],
            "order_no": p["order_no"], "stage_label": p.get("stage_label", ""),
            "at": e.at, "removed_at": None}
    elif k == "RouteItemRemoved":
        st.routes[p["route_id"]]["items"][p["item_id"]]["removed_at"] = e.at
    elif k == "RouteItemReordered":
        st.routes[p["route_id"]]["items"][p["item_id"]]["order_no"] = \
            p["order_no"]
    elif k == "RouteRetired":
        st.routes[p["route_id"]]["retired_at"] = e.at
        st.routes[p["route_id"]]["retire_reason"] = p.get("reason", "")

    elif k == "ViewpointOpened":
        st.viewpoints[p["viewpoint_id"]] = {
            "viewpoint_id": p["viewpoint_id"],
            "author_id": p["author_id"],
            "primary_anchor_id": p["primary_anchor_id"],
            "group_id": p.get("group_id"),
            "summary": p["summary"],
            "created_at": e.at, "evidence": {}, "comments": {},
            "decisions": [], "status": "draft", "audience_scopes": [],
        }
    elif k == "EvidenceAdded":
        vp = st.viewpoints[p["viewpoint_id"]]
        vp["evidence"][p["evidence_id"]] = {
            "evidence_id": p["evidence_id"], "anchor_id": p["anchor_id"],
            "quote": p.get("quote", ""), "note": p.get("note", ""),
            "adaptation_link_id": p.get("adaptation_link_id"),
            "by": p["by"], "at": e.at,
            "removed_at": None, "removed_by": None}
    elif k == "EvidenceRemoved":
        ev = st.viewpoints[p["viewpoint_id"]]["evidence"][p["evidence_id"]]
        ev["removed_at"] = e.at
        ev["removed_by"] = p["by"]
        ev["remove_reason"] = p.get("reason", "")
    elif k == "CommentAdded":
        vp = st.viewpoints[p["viewpoint_id"]]
        vp["comments"][p["comment_id"]] = {
            "comment_id": p["comment_id"], "author_id": p["author_id"],
            "body": p["body"], "created_at": e.at,
            "removed_at": None, "removed_by": None}
    elif k == "CommentRemoved":
        cmt = st.viewpoints[p["viewpoint_id"]]["comments"][p["comment_id"]]
        cmt["removed_at"] = e.at
        cmt["removed_by"] = p["by"]
        cmt["remove_reason"] = p.get("reason", "")
    elif k == "PublicationDecision":
        vp = st.viewpoints[p["viewpoint_id"]]
        vp["decisions"].append({
            "decision": p["decision"], "target_state": p["target_state"],
            "by": p["by"], "at": e.at, "reason": p.get("reason", ""),
            "scopes": p.get("scopes"),
        })
        vp["status"] = p["target_state"]
        if p["decision"] == "published":
            vp["audience_scopes"] = list(p["scopes"])
        elif p["decision"] in ("withheld", "withdrawn", "returned"):
            # 撤回/退回后保留"最近一次发布范围"的履历价值，但当前不生效
            vp["audience_scopes"] = list(vp["audience_scopes"])


def _intervals_overlap(start1, end1, start2, end2):
    """[start1,end1) 与 [start2,end2) 是否相交（None 端点表示开放）。"""
    if start1 and end2 and start1 > end2:
        return False
    if start2 and end1 and start2 > end1:
        return False
    return True
