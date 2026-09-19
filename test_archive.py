"""领域服务测试：覆盖版本锚点、冲突共存、发布链、可见范围、
离校断链、改编导入、路线复原与学期导出等核心需求。"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from eventlog import EventError, EventLog
from archive import (
    ReadingArchive, NotFoundError, ValidationError, AccessDeniedError,
    WORK_ADAPTATION,
)

_BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def ts(day, hh=8, mm=0):
    """9 月 1 日起第 day 天（允许跨月）的合法 ISO 时间戳。"""
    return (_BASE + timedelta(days=day - 1, hours=hh, minutes=mm)).isoformat()


class ArchiveTestBase(unittest.TestCase):
    def setUp(self):
        self.a = ReadingArchive()
        self._build_world()

    # -- 标准世界：原著两版教材、教师、学生、家长、班级、学期 -----------

    def _build_world(self):
        a = self.a
        a.register_work("xyj", "西游记", "吴承恩", min_age=12, at=ts(1))
        a.register_user("t_wang", "王老师", "teacher", at=ts(1))
        a.register_user("s1", "张三", "student", age=13, at=ts(1))
        a.register_user("s2", "李四", "student", age=13, at=ts(1))
        a.register_user("s3", "赵五", "student", age=11, at=ts(1))
        a.register_user("s4", "钱六", "student", age=14, at=ts(1))
        a.register_user("g1", "张三家长", "guardian", at=ts(1))
        a.register_user("g2", "李四家长", "guardian", at=ts(1))
        a.link_guardian("s1", "g1", at=ts(1))
        a.link_guardian("s2", "g2", at=ts(1))

        # 2018 人教旧版与 2024 统编新版（学生手里纸书不同）
        a.add_edition("ed2018", "xyj", "人教版2018节选", publisher="人教社",
                      year=2018, actor_id="t_wang", at=ts(1))
        a.add_edition("ed2024", "xyj", "统编版2024七上", publisher="人教社",
                      year=2024, bibliographic_note="换用新版教材",
                      actor_id="t_wang", at=ts(1))
        a.register_anchor("a7", "ed2018", 7, "八卦炉中逃大圣",
                          locator="上册第82页", quote_start="众力士推倒",
                          quote_end="依旧提兵", actor_id="t_wang", at=ts(1))
        a.register_anchor("a7n", "ed2024", 21, "小圣施威降大圣",
                          locator="七上第126页", actor_id="t_wang", at=ts(1))
        a.register_anchor("a27", "ed2018", 27, "尸魔三戏唐三藏",
                          locator="中册第31页", actor_id="t_wang", at=ts(1))
        a.add_anchor_alias("a7", "page", "2018印本 p.82",
                           actor_id="t_wang", at=ts(2))

        a.create_class("c1", "初一1班", "初一", actor_id="t_wang", at=ts(1))
        a.create_class("c2", "初一2班", "初一", actor_id="t_wang", at=ts(1))
        a.create_class("c3", "初二1班", "初二", actor_id="t_wang", at=ts(1))
        for uid in ("s1", "s2"):
            a.add_enrollment("c1", uid, at=ts(1))
        a.add_enrollment("c2", "s3", at=ts(1))
        a.add_enrollment("c3", "s4", at=ts(1))
        a.open_term("c1", "term2026autumn", ts(1), ts(120, 18),
                    actor_id="t_wang", at=ts(1))
        a.open_term("c2", "term2026autumn", ts(1), ts(120, 18),
                    actor_id="t_wang", at=ts(1))


class EditionAnchorTest(ArchiveTestBase):
    def test_anchor_carries_exact_edition_identity(self):
        anchor = self.a.get_anchor("a7")
        self.assertEqual(anchor["edition"]["name"], "人教版2018节选")
        self.assertEqual(anchor["edition"]["year"], 2018)
        self.assertEqual(anchor["aliases"][0]["value"], "2018印本 p.82")
        self.assertEqual(anchor["work_id"], "xyj")

    def test_old_anchors_survive_edition_supersession(self):
        self.a.open_viewpoint("vp1", "s1", "a7", "孙悟空反抗权力", at=ts(3))
        self.a.supersede_edition("ed2018", "ed2024",
                                 reason="换用统编新版", actor_id="t_wang",
                                 at=ts(40))
        # 换教材后：旧锚点仍可解析，观点依据的仍是当年那个版本
        vp = self.a.provenance("vp1", actor_id="t_wang")
        self.assertEqual(vp["primary_anchor"]["anchor_id"], "a7")
        self.assertEqual(vp["primary_anchor"]["edition"]["superseded_by"],
                         "ed2024")
        # 旧版不能再挂新锚点（已被取代只是状态，不禁止注册；但版本身份不变）
        anchor_now = self.a.get_anchor("a7")
        self.assertEqual(anchor_now["edition_id"], "ed2018")
        with self.assertRaises(ValidationError):
            self.a.supersede_edition("ed2018", "ed2024",
                                     actor_id="t_wang", at=ts(41))

    def test_cross_work_supersede_rejected(self):
        self.a.register_work("hlm", "红楼梦", "曹雪芹", at=ts(2))
        self.a.add_edition("ed_hlm", "hlm", "人文版", actor_id="t_wang",
                           at=ts(2))
        with self.assertRaises(ValidationError):
            self.a.supersede_edition("ed2018", "ed_hlm",
                                     actor_id="t_wang", at=ts(3))


class ConflictingViewpointsTest(ArchiveTestBase):
    def test_conflicting_interpretations_coexist_with_own_evidence(self):
        a = self.a
        a.open_viewpoint("vp_rebel", "s1", "a7",
                         "孙悟空的反抗体现对等级秩序的否定", at=ts(5))
        a.add_evidence("vp_rebel", "a7", "s1",
                       quote="皇帝轮流做，明年到我家", note="反天庭话语",
                       at=ts(5))
        a.open_viewpoint("vp_order", "s2", "a7",
                         "同一回目恰恰表现秩序对反抗的收编", at=ts(6))
        a.add_evidence("vp_order", "a7", "s2",
                       quote="五行山下定心猿", note="镇压与规训", at=ts(6))

        rebel = a.provenance("vp_rebel", actor_id="t_wang")
        order = a.provenance("vp_order", actor_id="t_wang")
        self.assertEqual(len(rebel["evidence"]), 1)
        self.assertEqual(len(order["evidence"]), 1)
        self.assertNotEqual(rebel["evidence"][0]["evidence_id"],
                            order["evidence"][0]["evidence_id"])
        self.assertEqual(rebel["evidence"][0]["quote"],
                         "皇帝轮流做，明年到我家")

        # 同锚点列出两条，谁也不覆盖谁
        visible = a.list_viewpoints("t_wang", anchor_id="a7")
        self.assertEqual({v["viewpoint_id"] for v in visible},
                         {"vp_rebel", "vp_order"})

        # 撤回其中一条不影响另一条
        a.submit("vp_rebel", "s1", at=ts(7))
        a.publish("vp_rebel", "t_wang", ["class"], at=ts(8))
        a.withdraw("vp_rebel", "s1", reason="观点有变化", at=ts(20))
        order2 = a.get_viewpoint("vp_order", "t_wang")
        self.assertEqual(order2["summary"],
                         "同一回目恰恰表现秩序对反抗的收编")

    def test_removed_evidence_still_traced_for_teacher(self):
        a = self.a
        a.open_viewpoint("vp1", "s1", "a7", "观点", at=ts(5))
        ev = a.add_evidence("vp1", "a7", "s1", quote="引文", at=ts(5))
        a.remove_evidence("vp1", ev, "t_wang", reason="引文核对有误", at=ts(9))
        vp = a.get_viewpoint("vp1", "s1")
        self.assertTrue(vp["evidence"][0]["removed"])
        self.assertIsNone(vp["evidence"][0]["quote"])
        full = a.provenance("vp1", actor_id="t_wang")
        self.assertEqual(full["evidence"][0]["quote"], "引文")
        self.assertEqual(full["evidence"][0]["removed_by"], "t_wang")

    def test_student_cannot_remove_others_evidence(self):
        a = self.a
        a.open_viewpoint("vp1", "s1", "a7", "观点", at=ts(5))
        ev = a.add_evidence("vp1", "a7", "s1", quote="引文", at=ts(5))
        with self.assertRaises(AccessDeniedError):
            a.remove_evidence("vp1", ev, "s2", at=ts(6))


class PublicationChainTest(ArchiveTestBase):
    def _draft_to_published(self, vp="vp1", scopes=("class",)):
        a = self.a
        a.open_viewpoint(vp, "s1", "a7", "初稿观点", at=ts(5))
        a.add_evidence("vp1", "a7", "s1", quote="原文证据", at=ts(5))
        # 草稿/待审对同班同学不可见
        self._invisible("vp1", "s2")
        a.submit("vp1", "s1", at=ts(6))
        self._invisible("vp1", "s2")
        a.return_viewpoint("vp1", "t_wang", reason="需要补一条原文", at=ts(7))
        a.add_evidence("vp1", "a7", "s1", quote="补充证据", at=ts(8))
        a.submit("vp1", "s1", at=ts(9))
        a.publish("vp1", "t_wang", list(scopes), at=ts(10))

    def _invisible(self, vp, viewer):
        with self.assertRaises(NotFoundError):
            self.a.get_viewpoint(vp, viewer)

    def test_full_decision_chain_and_withhold(self):
        self._draft_to_published()
        a = self.a
        vp = a.get_viewpoint("vp1", "s2")  # 同班同学可见
        self.assertEqual(vp["status"], "published")
        decisions = vp["decisions"]
        self.assertEqual([d["decision"] for d in decisions],
                         ["submitted", "returned", "submitted", "published"])
        self.assertEqual(decisions[1]["reason"], "需要补一条原文")
        self.assertTrue(all(d["by"] for d in decisions))

        # 撤回批注：立即对学生/家长不可见，但事件链保留
        a.withhold("vp1", "t_wang", reason="批注尚未准备好对家长公开",
                   at=ts(11))
        self._invisible("vp1", "s2")
        full = a.provenance("vp1", actor_id="t_wang")
        self.assertEqual(full["status"], "withheld")
        self.assertEqual(full["decisions"][-1]["decision"], "withheld")

        # 重新发布
        a.publish("vp1", "t_wang", ["class"], at=ts(12))
        self.assertEqual(a.get_viewpoint("vp1", "s2")["status"], "published")

    def test_state_machine_guards(self):
        a = self.a
        a.open_viewpoint("vp1", "s1", "a7", "初稿", at=ts(5))
        with self.assertRaises(ValidationError):
            a.publish("vp1", "t_wang", ["class"], at=ts(6))
        with self.assertRaises(AccessDeniedError):
            a.submit("vp1", "s2", at=ts(6))
        a.submit("vp1", "s1", at=ts(6))
        with self.assertRaises(ValidationError):
            a.submit("vp1", "s1", at=ts(7))
        a.publish("vp1", "t_wang", ["class"], at=ts(8))
        with self.assertRaises(ValidationError):
            a.publish("vp1", "t_wang", ["class"], at=ts(9))
        a.withdraw("vp1", "s1", at=ts(10))
        with self.assertRaises(ValidationError):
            a.submit("vp1", "s1", at=ts(11))
        with self.assertRaises(NotFoundError):
            a.add_comment("vp1", "s2", "还能讨论吗", at=ts(11))
        with self.assertRaises(ValidationError):
            a.add_evidence("vp1", "a7", "s1", at=ts(11))

    def test_withdraw_before_parents(self):
        self._draft_to_published(scopes=("class", "guardians"))
        a = self.a
        self.assertEqual(a.get_viewpoint("vp1", "g1")["status"], "published")
        a.withhold("vp1", "t_wang", at=ts(15))
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp1", "g1")


class VisibilityScopeTest(ArchiveTestBase):
    def _publish_grade(self):
        a = self.a
        a.open_viewpoint("vp1", "s1", "a7", "年级层面观点", at=ts(5))
        a.submit("vp1", "s1", at=ts(6))
        a.publish("vp1", "t_wang", ["grade"], at=ts(7))

    def test_grade_scope_respects_grade_and_age(self):
        self._publish_grade()
        a = self.a
        # 同年级、达到适读年龄：同班当然可见
        self.assertTrue(a.get_viewpoint("vp1", "s2"))
        # 同年级但年龄不足（11 < 12）：不可见
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp1", "s3")
        # 年龄足够但不同年级：不可见
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp1", "s4")

    def test_guardian_scope_only_own_child_and_active_enrollment(self):
        a = self.a
        a.open_viewpoint("vp_s1", "s1", "a7", "张三观点", at=ts(5))
        a.open_viewpoint("vp_s2", "s2", "a7", "李四观点", at=ts(5))
        for vp in ("vp_s1", "vp_s2"):
            a.submit(vp, {"vp_s1": "s1", "vp_s2": "s2"}[vp], at=ts(6))
        a.publish("vp_s1", "t_wang", ["guardians"], at=ts(7))
        a.publish("vp_s2", "t_wang", ["guardians"], at=ts(7))
        self.assertTrue(a.get_viewpoint("vp_s1", "g1"))
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp_s2", "g1")  # 不是自己的孩子
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp_s1", "g2")
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp_s1", "s2")  # 学生不经监护范围看见
        # 解除监护关系后不可见
        a.unlink_guardian("s1", "g1", at=ts(8))
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp_s1", "g1")

    def test_group_draft_visibility(self):
        a = self.a
        a.create_group("grp1", "民俗小组", "c1", actor_id="t_wang", at=ts(3))
        a.add_group_member("grp1", "s1", at=ts(3))
        a.open_viewpoint("vp_g", "s1", "a7", "组内初稿", group_id="grp1",
                         at=ts(5))
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp_g", "s2")
        a.add_group_member("grp1", "s2", at=ts(6))
        self.assertTrue(a.get_viewpoint("vp_g", "s2"))
        a.remove_group_member("grp1", "s2", at=ts(9))
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp_g", "s2")
        # 移除前的历史时点仍可见
        self.assertTrue(a.get_viewpoint("vp_g", "s2", as_of=ts(8)))

    def test_outsider_class_cannot_see(self):
        a = self.a
        a.open_viewpoint("vp1", "s1", "a7", "班内观点", at=ts(5))
        a.submit("vp1", "s1", at=ts(6))
        a.publish("vp1", "t_wang", ["class"], at=ts(7))
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp1", "s3")  # 同年级他班
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp1", "s4")  # 他年级


class DepartureTest(ArchiveTestBase):
    def test_departed_student_loses_access_but_discussion_stays_linked(self):
        a = self.a
        a.open_viewpoint("vp1", "s1", "a7", "张三的读书观点", at=ts(5))
        a.add_evidence("vp1", "a7", "s1", quote="原文", at=ts(5))
        a.submit("vp1", "s1", at=ts(6))
        a.publish("vp1", "t_wang", ["class", "guardians"], at=ts(7))
        a.add_comment("vp1", "s2", "我同意这处民俗解读", at=ts(8))

        a.depart_user("s1", reason="转学离校", actor_id="t_wang", at=ts(60))

        # 本人立即失去访问权
        with self.assertRaises(AccessDeniedError):
            a.get_viewpoint("vp1", "s1")
        with self.assertRaises(AccessDeniedError):
            a.add_comment("vp1", "s1", "离校后发言", at=ts(61))

        # 历史时点仍可复原其访问权
        self.assertTrue(a.get_viewpoint("vp1", "s1", as_of=ts(59)))

        # 讨论不断链：同学仍可读，作者署名与全部发言保留
        vp = a.get_viewpoint("vp1", "s2")
        self.assertEqual(vp["author_id"], "s1")
        self.assertEqual(vp["author_name"], "张三")
        self.assertEqual(len(vp["comments"]), 1)
        self.assertEqual(vp["comments"][0]["body"], "我同意这处民俗解读")
        self.assertEqual(vp["evidence"][0]["quote"], "原文")

        # 监护可见性随在校状态终止
        with self.assertRaises(NotFoundError):
            a.get_viewpoint("vp1", "g1")

        # 教师溯源与历史记录完整
        prov = a.provenance("vp1", actor_id="t_wang")
        self.assertEqual(prov["decisions"][-1]["by"], "t_wang")

    def test_teacher_only_actions(self):
        with self.assertRaises(AccessDeniedError):
            self.a.create_class("cx", "X班", "初一", actor_id="s1", at=ts(2))
        with self.assertRaises(AccessDeniedError):
            self.a.depart_user("s2", actor_id="s1", at=ts(2))


class AdaptationTest(ArchiveTestBase):
    def test_adaptation_is_metadata_and_original_links_only(self):
        a = self.a
        a.register_adaptation(
            "wukong", "悟空传", "今何在", "xyj",
            bibliographic_note="2001年光明日报出版社；对照讨论用",
            at=ts(4))
        work = next(w for w in a.list_works(kind=WORK_ADAPTATION)
                    if w["work_id"] == "wukong")
        self.assertEqual(work["source_work_id"], "xyj")
        # 改编作品不导入正文、不另立版本锚点
        with self.assertRaises(ValidationError):
            a.add_edition("ed_wk", "wukong", "悟空传初版",
                          actor_id="t_wang", at=ts(4))

        a.add_adaptation_link("link1", "wukong", "a7",
                              adapted_locator="第三章·天宫一节",
                              note="悟空与众神对峙的改写",
                              actor_id="t_wang", at=ts(5))

        a.open_viewpoint("vp_cmp", "s1", "a7", "改编与原文的反抗母题对照",
                         at=ts(6))
        a.add_evidence("vp_cmp", "a7", "s1",
                       quote="皇帝轮流做", adaptation_link_id="link1",
                       note="《悟空传》强化了这一主题", at=ts(6))
        vp = a.provenance("vp_cmp", actor_id="t_wang")
        self.assertEqual(vp["evidence"][0]["adaptation_link_id"], "link1")

        # 关联与证据锚点不一致时拒绝
        a.open_viewpoint("vp2", "s2", "a27", "另一处", at=ts(7))
        with self.assertRaises(ValidationError):
            a.add_evidence("vp2", "a27", "s2",
                           adaptation_link_id="link1", at=ts(7))

        # 普通学生不能建改编关联（教师/馆员职责）
        with self.assertRaises(AccessDeniedError):
            a.add_adaptation_link("linkX", "wukong", "a7",
                                  actor_id="s2", at=ts(8))


class RouteRecoveryTest(ArchiveTestBase):
    def test_route_changes_and_retirement_are_recoverable(self):
        a = self.a
        a.create_route("r1", "七上阅读路线", "c1", "term2026autumn",
                       actor_id="t_wang", at=ts(3))
        i1 = a.add_route_item("r1", "a7", actor_id="t_wang", at=ts(3))
        a.add_route_item("r1", "a27", actor_id="t_wang", at=ts(4))
        a.register_anchor("a50", "ed2018", 50, "情乱性从因爱欲",
                          actor_id="t_wang", at=ts(5))
        a.add_route_item("r1", "a50", actor_id="t_wang", at=ts(6))

        snapshot = ts(10)
        original = a.get_route("r1", as_of=snapshot)
        self.assertEqual([it["anchor"]["anchor_id"] for it in original["items"]],
                         ["a7", "a27", "a50"])

        # 教师调整路线：移除、调序、最终停用换新路线
        a.remove_route_item("r1", i1, actor_id="t_wang", at=ts(30))
        a.reorder_route_item("r1",
                             [it["item_id"] for it in a.get_route("r1")["items"]
                              if it["anchor"]["anchor_id"] == "a50"][0],
                             1, actor_id="t_wang", at=ts(31))
        a.retire_route("r1", reason="换用新版教材路线",
                       actor_id="t_wang", at=ts(40))

        current = a.get_route("r1")
        self.assertEqual([it["anchor"]["anchor_id"] for it in current["items"]],
                         ["a50", "a27"])
        self.assertIsNotNone(current["retired_at"])

        # 过去班级看到的内容仍可复原
        past = a.get_route("r1", as_of=snapshot)
        self.assertEqual([it["anchor"]["anchor_id"] for it in past["items"]],
                         ["a7", "a27", "a50"])
        self.assertIsNone(past["retired_at"])


class TermExportTest(ArchiveTestBase):
    def _prepare_term(self):
        a = self.a
        a.create_route("r1", "七上阅读路线", "c1", "term2026autumn",
                       actor_id="t_wang", at=ts(3))
        a.add_route_item("r1", "a7", actor_id="t_wang", stage_label="精读",
                         at=ts(3))

        a.open_viewpoint("vp1", "s1", "a7", "反抗主题", at=ts(10))
        a.add_evidence("vp1", "a7", "s1", quote="皇帝轮流做", at=ts(10))
        a.submit("vp1", "s1", at=ts(11))
        a.publish("vp1", "t_wang", ["class"], at=ts(12))
        a.add_comment("vp1", "s2", "民俗角度补充：蟠桃会等级森严", at=ts(13))

        a.open_viewpoint("vp2", "s2", "a27", "白骨精故事的叙事重复", at=ts(14))
        a.add_evidence("vp2", "a27", "s2", quote="一行三步一打", at=ts(14))
        a.submit("vp2", "s2", at=ts(15))

        a.open_viewpoint("vp3", "s1", "a7", "已放弃的旧解释", at=ts(16))
        a.submit("vp3", "s1", at=ts(17))
        a.withdraw("vp3", "s1", reason="解释被自己推翻", at=ts(18))

    def test_export_covers_read_cited_discussed_with_provenance(self):
        self._prepare_term()
        a = self.a
        export = a.export_term("c1", "term2026autumn", actor_id="t_wang",
                               as_of=ts(100))
        self.assertEqual(export["coverage"]["read"], 1)
        self.assertEqual(export["coverage"]["cited"], 2)
        self.assertEqual(export["coverage"]["viewpoints"], 3)
        self.assertEqual(export["coverage"]["discussed_viewpoints"], 3)

        materials = {m["anchor_id"]: m for m in export["materials"]}
        self.assertEqual(set(materials), {"a7", "a27"})
        self.assertEqual(materials["a7"]["used_as"],
                         ["read", "cited", "discussed"])
        self.assertEqual(materials["a27"]["used_as"], ["cited", "discussed"])
        # 材料可追溯到具体纸书版本
        self.assertEqual(materials["a7"]["edition"]["name"], "人教版2018节选")

        vp1 = next(v for v in export["viewpoints"]
                   if v["viewpoint_id"] == "vp1")
        self.assertEqual(vp1["author_id"], "s1")
        self.assertEqual(vp1["primary_anchor"]["edition_id"], "ed2018")
        chain = [d["decision"] for d in vp1["decisions"]]
        self.assertEqual(chain, ["submitted", "published"])
        self.assertEqual(vp1["comments"][0]["body"],
                         "民俗角度补充：蟠桃会等级森严")
        # 撤回的观点也在档案里，且标明终态与原因
        vp3 = next(v for v in export["viewpoints"]
                   if v["viewpoint_id"] == "vp3")
        self.assertEqual(vp3["status"], "withdrawn")
        self.assertEqual(vp3["decisions"][-1]["reason"], "解释被自己推翻")

        # 导出动作本身留痕
        snap = a._snapshot()
        self.assertEqual(snap.classes["c1"]["exports"][0]["by"], "t_wang")

    def test_export_excludes_other_class_and_later_work(self):
        self._prepare_term()
        a = self.a
        # 他班学生的观点不进本班导出
        a.open_viewpoint("vp_other", "s3", "a7", "他班观点", at=ts(20))
        early = a.export_term("c1", "term2026autumn", actor_id="t_wang",
                              as_of=ts(13))
        ids = {v["viewpoint_id"] for v in early["viewpoints"]}
        self.assertEqual(ids, {"vp1"})  # 13 号之前只有 vp1 活动
        self.assertNotIn("vp2", ids)

    def test_departed_student_contributions_still_exported(self):
        self._prepare_term()
        a = self.a
        a.depart_user("s1", reason="转学", actor_id="t_wang", at=ts(50))
        export = a.export_term("c1", "term2026autumn", actor_id="t_wang",
                               as_of=ts(100))
        authors = {v["author_id"] for v in export["viewpoints"]}
        self.assertIn("s1", authors)

    def test_export_includes_route_change_history(self):
        self._prepare_term()
        a = self.a
        item = a.get_route("r1")["items"][0]["item_id"]
        a.remove_route_item("r1", item, actor_id="t_wang", at=ts(40))
        export = a.export_term("c1", "term2026autumn", actor_id="t_wang",
                               as_of=ts(100))
        route = export["routes"][0]
        kinds = [h["kind"] for h in route["history"]]
        self.assertIn("RouteItemAdded", kinds)
        self.assertIn("RouteItemRemoved", kinds)


class EventLogPersistenceTest(unittest.TestCase):
    def test_jsonl_roundtrip_and_order_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            log = EventLog(path=path)
            log.append("viewpoint:x", "ViewpointOpened", {"x": 1},
                       at="2026-09-01T08:00:00+00:00")
            log.append("viewpoint:x", "PublicationDecision", {"x": 2},
                       at="2026-09-02T08:00:00+00:00")

            replayed = EventLog(path=path)
            self.assertEqual(replayed.count, 2)
            self.assertEqual([e.kind for e in replayed.read("viewpoint:x")],
                             ["ViewpointOpened", "PublicationDecision"])
            with self.assertRaises(EventError):
                replayed.append("viewpoint:x", "PublicationDecision", {},
                                at="2026-08-31T08:00:00+00:00")

    def test_as_of_replay(self):
        log = EventLog()
        log.append("s", "A", {}, at="2026-09-01T08:00:00+00:00")
        log.append("s", "B", {}, at="2026-09-03T08:00:00+00:00")
        self.assertEqual(
            [e.kind for e in log.all_events(as_of="2026-09-02T00:00:00+00:00")],
            ["A"])


if __name__ == "__main__":
    unittest.main()
