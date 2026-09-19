"""阅读档案领域测试：覆盖版本锚点、观点共存、发布决定、可见性、
时间点复原、离校断链、改编作品最小导入与学期导出。"""

import json
import tempfile
import unittest
from pathlib import Path

from reading_archive import (
    Anchor,
    ArchiveError,
    DENY_AGE,
    DENY_LEFT,
    DENY_SCOPE,
    DENY_WITHDRAWN,
    EventStore,
    ReadingArchive,
    STATUS_PUBLISHED,
    STATUS_WITHDRAWN,
)


class Clock:
    """按调用次数产生稳定时间戳，便于按事件序号做时间点复原断言。"""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"2026-09-{self.n:02d}T08:00:00Z"


class ReadingArchiveTest(unittest.TestCase):
    def setUp(self):
        self.archive = ReadingArchive(EventStore(clock=Clock()))
        self._build_school()

    # ----- 夹具 -----------------------------------------------------------

    def _build_school(self):
        a = self.archive
        t = "t-wang"  # 王老师

        # 首位教师先登记（引导），其后所有教务操作都由该教师授权
        a.register_person(t, t, "王老师", "teacher")

        # 原著与三种纸书/教材版本
        a.register_work(t, "w-xyj", "西游记", author="吴承恩")
        a.register_version(t, "v-renmin", "w-xyj", "人文社1980年版",
                           publisher="人民文学出版社", year=1980, isbn="R-1980",
                           char_count=800_000)
        a.register_version(t, "v-jc2024", "w-xyj", "语文教材2024版节选",
                           publisher="某教育出版社", year=2024, isbn="JC-2024",
                           char_count=50_000)
        a.register_version(t, "v-jc2026", "w-xyj", "语文教材2026版节选",
                           publisher="某教育出版社", year=2026, isbn="JC-2026",
                           char_count=52_000, replaces="v-jc2024",
                           reason="新学期换用新版教材")
        # 同一逻辑章节在不同版本里各自登记，锚点不串版本
        for version_id, offset in [("v-renmin", 120000), ("v-jc2024", 3000),
                                   ("v-jc2026", 3200)]:
            a.register_chapter(t, f"ch-27-{version_id}", version_id, "第027回",
                               "尸魔三戏唐三藏", order=27)
            self.chapter_27_offset = {version_id: offset}
        a.register_chapter(t, "ch-28-jc2026", "v-jc2026", "第028回",
                           "花果山群妖聚义", order=28)

        # 改编作品《悟空传》：只登记关联与元数据
        a.register_related_work(
            t, "rw-wukong", "悟空传", "长篇小说", "w-xyj",
            note="学生用于对照讨论的改编作品，不收录正文",
            metadata={"author": "今何在", "year": 2000})

        # 班级与人员
        a.register_class(t, "c-7y1", "七年级一班", grade="七年级")
        a.register_class(t, "c-7y2", "七年级二班", grade="七年级")
        a.register_person(t, "s-ba", "巴同学", "student", birth_year=2013)   # 2026 年 13 岁
        a.register_person(t, "s-lin", "林同学", "student", birth_year=2013)
        a.register_person(t, "s-meng", "孟同学", "student", birth_year=2015)  # 11 岁
        a.register_person(t, "g-ba", "巴家长", "guardian")
        a.register_person(t, "g-other", "二班家长", "guardian")
        a.register_person(t, "s-other", "二班学生", "student", birth_year=2013)
        for sid in ("s-ba", "s-lin", "s-meng"):
            a.add_class_member(t, "c-7y1", sid)
        a.add_class_member(t, "c-7y1", t)
        a.add_class_member(t, "c-7y2", "s-other")
        a.link_guardian(t, "g-ba", "s-ba")
        a.link_guardian(t, "g-other", "s-other")

        # 小组
        a.create_group(t, "g-team1", "c-7y1", "第一讨论小组")
        a.add_group_member(t, "g-team1", "s-ba")
        a.add_group_member(t, "g-team1", "s-lin")

        # 初始阅读路线基于 2024 版教材
        a.define_route(t, "r-7y1", "c-7y1", "七上《西游记》路线",
                       [{"chapter_id": "ch-27-v-jc2024",
                         "assign_version_id": "v-jc2024"}],
                       reason="学期初排定")

    def anchor_27(self, version_id="v-jc2024", start=100, end=160, quote="那怪物……"):
        return {"version_id": version_id, "chapter_path": "第027回",
                "start": start, "end": end, "quote": quote}

    # ----- 版本与锚点 -----------------------------------------------------

    def test_anchor_must_belong_to_registered_version_chapter(self):
        with self.assertRaises(ArchiveError):
            self.archive.create_viewpoint(
                "s-ba", "vp-bad", "personal",
                {"version_id": "v-jc2026", "chapter_path": "第099回"}, "正文")
        with self.assertRaises(ArchiveError):
            self.archive.create_viewpoint(
                "s-ba", "vp-bad2", "personal",
                {"version_id": "v-renmin", "chapter_path": "第027回",
                 "start": 900000, "end": 900001}, "正文")  # 超出版本范围

    def test_same_chapter_different_versions_are_distinct_anchors(self):
        a = self.archive
        a.create_viewpoint("s-ba", "vp-paper", "personal",
                           self.anchor_27("v-renmin", 120100, 120200, "纸书原文"),
                           "我读的是家里人文社纸书")
        a.create_viewpoint("s-lin", "vp-textbook", "personal",
                           self.anchor_27("v-jc2024", 100, 200, "教材原文"),
                           "我读的是教材节选", publish=True)
        paper = a.viewpoints["vp-paper"].anchor
        book = a.viewpoints["vp-textbook"].anchor
        self.assertEqual(paper.chapter_path, book.chapter_path)
        self.assertNotEqual(paper.version_id, book.version_id)
        self.assertNotEqual((paper.start, paper.end), (book.start, book.end))

    # ----- 冲突解释共存 ---------------------------------------------------

    def test_conflicting_interpretations_coexist_with_own_evidence(self):
        a = self.archive
        a.create_viewpoint(
            "s-ba", "vp-c1", "group", self.anchor_27(),
            "唐僧人妖不辨，逐走悟空是糊涂。", group_id="g-team1", publish=True)
        a.create_viewpoint(
            "s-lin", "vp-c2", "group",
            self.anchor_27("v-jc2024", 300, 420, "出家人扫地恐伤蝼蚁命"),
            "唐僧持戒不杀，他的迟疑可以理解。", group_id="g-team1", publish=True)
        visible = {v.id for v in a.visible_viewpoints("s-ba", work_id="w-xyj")}
        self.assertIn("vp-c1", visible)
        self.assertIn("vp-c2", visible)  # 冲突观点并存，而非互相覆盖
        prov = a.provenance("vp-c2")
        self.assertEqual(prov["author"]["id"], "s-lin")
        self.assertEqual(prov["version"]["id"], "v-jc2024")
        self.assertEqual([d["action"] for d in prov["decisions"]],
                         ["created", "published"])

    # ----- 可见范围：草稿、班级、小组、家长、年龄 --------------------------

    def test_drafts_not_exposed_to_students_or_guardians(self):
        a = self.archive
        a.create_viewpoint("s-ba", "vp-draft", "class", self.anchor_27(),
                           "还没写完的猜想", class_id="c-7y1")  # 草稿
        self.assertFalse(a.can_view("vp-draft", "s-lin")[0])
        self.assertFalse(a.can_view("vp-draft", "g-ba")[0])
        # 作者本人和本班教师可以看到草稿
        self.assertTrue(a.can_view("vp-draft", "s-ba")[0])
        self.assertTrue(a.can_view("vp-draft", "t-wang")[0])

    def test_guardian_scope_requires_ward_in_class_and_guardian_audience(self):
        a = self.archive
        a.create_viewpoint("t-wang", "vp-class", "class", self.anchor_27(),
                           "本周共读要点", class_id="c-7y1", publish=True)
        a.create_viewpoint("t-wang", "vp-home", "guardians", self.anchor_27(),
                           "请家长关注孩子的阅读进度", class_id="c-7y1", publish=True)
        a.create_viewpoint("s-ba", "vp-team", "group", self.anchor_27(),
                           "组内意见", group_id="g-team1", publish=True)
        self.assertTrue(a.can_view("vp-class", "g-ba")[0])
        self.assertTrue(a.can_view("vp-home", "g-ba")[0])
        self.assertFalse(a.can_view("vp-team", "g-ba")[0])            # 小组内容不对家长
        self.assertFalse(a.can_view("vp-home", "g-other")[0])        # 非本班家长
        self.assertFalse(a.can_view("vp-class", "g-other")[0])

    def test_student_cannot_publish_to_guardians(self):
        with self.assertRaises(ArchiveError):
            self.archive.create_viewpoint(
                "s-ba", "vp-x", "guardians", self.anchor_27(), "给家长的话",
                class_id="c-7y1", publish=True)

    def test_age_gate_blocks_younger_students_only(self):
        a = self.archive
        a.create_viewpoint("t-wang", "vp-mature", "class", self.anchor_27(),
                           "涉及死亡主题的延伸讨论", class_id="c-7y1",
                           min_age=12, publish=True)
        ok, reason = a.can_view("vp-mature", "s-meng", current_year=2026)  # 11 岁
        self.assertFalse(ok)
        self.assertEqual(reason, DENY_AGE)
        self.assertTrue(a.can_view("vp-mature", "s-ba", current_year=2026)[0])  # 13 岁
        self.assertTrue(a.can_view("vp-mature", "g-ba", current_year=2026)[0])  # 家长不受限

    def test_personal_notes_remain_private(self):
        a = self.archive
        a.create_viewpoint("s-ba", "vp-diary", "personal", self.anchor_27(),
                           "我个人的语言民俗笔记")
        self.assertTrue(a.can_view("vp-diary", "s-ba")[0])
        self.assertFalse(a.can_view("vp-diary", "s-lin")[0])
        self.assertFalse(a.can_view("vp-diary", "g-ba")[0])

    # ----- 撤回与发布决定链 -----------------------------------------------

    def test_withdraw_hides_content_but_keeps_decisions_and_allows_restore(self):
        a = self.archive
        a.create_viewpoint("t-wang", "vp-note", "class", self.anchor_27(),
                           "一条原定公开的批注", class_id="c-7y1", publish=True)
        self.assertTrue(a.can_view("vp-note", "g-ba")[0])
        event = a.withdraw_viewpoint("t-wang", "vp-note", reason="批注尚未定稿，撤回")
        withdraw_seq = event["seq"]
        ok, reason = a.can_view("vp-note", "g-ba")
        self.assertFalse(ok)
        self.assertEqual(reason, DENY_WITHDRAWN)
        # 撤回前一刻的投影：家长仍可见——过去的可见状态可复原
        past = a.snapshot_at(withdraw_seq - 1)
        self.assertTrue(past.can_view("vp-note", "g-ba"))

        prov = a.provenance("vp-note")
        self.assertEqual(prov["status"], STATUS_WITHDRAWN)
        self.assertEqual([d["action"] for d in prov["decisions"]][-1], "withdrawn")
        self.assertEqual(prov["decisions"][-1]["reason"], "批注尚未定稿，撤回")

        a.restore_viewpoint("t-wang", "vp-note", reason="定稿后恢复")
        self.assertTrue(a.can_view("vp-note", "g-ba")[0])
        self.assertEqual(a.provenance("vp-note")["decisions"][-1]["action"], "restored")

    # ----- 路线调整与换教材：时间点复原 -----------------------------------

    def test_route_adjustment_and_textbook_replacement_are_reconstructable(self):
        a = self.archive
        a.create_viewpoint("s-ba", "vp-old", "class",
                           self.anchor_27("v-jc2024"), "基于旧教材第27回的发言",
                           class_id="c-7y1", publish=True)
        before_change = len(a.store.events)

        # 新学期换教材：路线改到 2026 版，旧观点由教师用新锚点接替
        a.adjust_route("t-wang", "r-7y1",
                       [{"chapter_id": "ch-27-v-jc2026", "assign_version_id": "v-jc2026"},
                        {"chapter_id": "ch-28-jc2026", "assign_version_id": "v-jc2026"}],
                       name="七上《西游记》路线（新教材）", reason="换用2026版教材")
        a.supersede_viewpoint(
            "t-wang", "vp-old", "vp-new",
            self.anchor_27("v-jc2026", 120, 240, "新教材第27回原文"),
            "同一讨论在新版教材上的重新落位", reason="换版后重新锚定")

        # 当前状态：新路线、新旧观点都保留并互相关联
        self.assertEqual([e["chapter_id"] for e in a.route_entries["r-7y1"]],
                         ["ch-27-v-jc2026", "ch-28-jc2026"])
        self.assertEqual(a.provenance("vp-old")["replaced_by"], "vp-new")
        self.assertEqual(a.provenance("vp-new")["supersedes"], "vp-old")
        self.assertEqual(a.provenance("vp-new")["version"]["replaces"], "v-jc2024")

        # 换版前的投影：旧路线、旧观点仍"发布中"且无接替标记
        past = a.snapshot_at(before_change)
        self.assertEqual([e["chapter_id"] for e in past.route_entries["r-7y1"]],
                         ["ch-27-v-jc2024"])
        self.assertEqual(past.viewpoints["vp-old"].status, STATUS_PUBLISHED)
        self.assertIsNone(past.provenance("vp-old")["replaced_by"])
        # 过去班级的导出也可完整复原
        old_export = past.export_class_term("t-wang", "c-7y1")
        self.assertEqual({v["version_id"] for v in old_export["versions"]},
                         {"v-jc2024"})

    # ----- 离校：访问失效但不断链 -----------------------------------------

    def test_left_student_loses_access_but_discussions_remain_linked(self):
        a = self.archive
        a.create_viewpoint("s-lin", "vp-lin", "group",
                           self.anchor_27("v-jc2024", 50, 90, "原文"),
                           "林同学的组内观点", group_id="g-team1", publish=True)
        a.mark_person_left("t-wang", "s-lin", reason="转学离校")

        ok, reason = a.can_view("vp-lin", "s-lin")  # 本人凭证也已失效
        self.assertFalse(ok)
        self.assertEqual(reason, DENY_LEFT)
        with self.assertRaises(ArchiveError):
            a.create_viewpoint("s-lin", "vp-x", "personal", self.anchor_27(), "离校后发言")

        # 其他人仍能看到林同学已发布的观点，出处仍指向本人
        self.assertTrue(a.can_view("vp-lin", "s-ba")[0])
        prov = a.provenance("vp-lin")
        self.assertEqual(prov["author"]["name"], "林同学")
        self.assertFalse(prov["author_active"])
        # 移出班级不影响历史档案
        a.remove_class_member("t-wang", "c-7y1", "s-lin")
        self.assertTrue(a.can_view("vp-lin", "s-ba")[0])

    # ----- 改编作品最小导入 -----------------------------------------------

    def test_adaptation_imports_only_link_and_metadata(self):
        a = self.archive
        rw = a.related["rw-wukong"]
        self.assertEqual(rw.relation, "adaptation")
        self.assertEqual(rw.source_work_id, "w-xyj")
        self.assertEqual(rw.metadata["author"], "今何在")
        # 改编作品没有版本/正文，无法在其上落锚点
        with self.assertRaises(ArchiveError):
            a.create_viewpoint("s-ba", "vp-adapt", "personal",
                               {"version_id": "rw-wukong", "chapter_path": "第一章"},
                               "试图引用改编作品正文")

    # ----- 学期导出 -------------------------------------------------------

    def test_term_export_covers_read_cited_discussed_with_provenance(self):
        a = self.archive
        a.create_viewpoint("s-ba", "vp-personal", "personal",
                           self.anchor_27("v-renmin", 120100, 120180, "纸书原文"),
                           "我注意到方言民俗词")
        a.create_viewpoint("s-lin", "vp-group", "group", self.anchor_27(),
                           "组内对孙悟空形象的讨论", group_id="g-team1", publish=True,
                           evidence=[self.anchor_27("v-renmin", 120500, 120560,
                                                    "又只见那行者……")])
        a.create_viewpoint("t-wang", "vp-teacher", "class", self.anchor_27(),
                           "教师总结", class_id="c-7y1", publish=True)
        a.withdraw_viewpoint("t-wang", "vp-teacher", reason="误发，撤回")
        a.mark_person_left("t-wang", "s-lin", reason="学期末转学")

        export = a.export_class_term("t-wang", "c-7y1", current_year=2026)

        # 读过：路线编排过的章节与版本
        read_paths = {c["chapter_path"] for c in export["read_chapters"]}
        self.assertEqual(read_paths, {"第027回"})

        # 引用过：路线版本 + 个人纸书版本 + 证据引用版本
        version_index = {v["version_id"]: v for v in export["versions"]}
        self.assertEqual(set(version_index), {"v-jc2024", "v-renmin"})
        citations = {c["viewpoint_id"] for v in version_index.values() for c in v["citations"]}
        self.assertIn("vp-group", citations)

        # 讨论过：观点齐全，撤回者不导出正文但保留决定链；离校学生标注 left
        views = {v["id"]: v for v in export["viewpoints"]}
        self.assertEqual(set(views), {"vp-personal", "vp-group", "vp-teacher"})
        self.assertIsNone(views["vp-teacher"]["body"])
        self.assertEqual(views["vp-teacher"]["status"], STATUS_WITHDRAWN)
        self.assertEqual(views["vp-group"]["author_status"], "left")
        # 任一观点都能说明出自谁、依据哪个版本、经过哪些发布决定
        self.assertEqual(views["vp-group"]["provenance"]["author"]["id"], "s-lin")
        actions = [d["action"] for d in views["vp-group"]["provenance"]["decisions"]]
        self.assertEqual(actions, ["created", "published"])
        # 与改编作品的关联随档导出，正文不在库中
        self.assertEqual([r["id"] for r in export["adaptations"]], ["rw-wukong"])
        self.assertNotIn("body", export["adaptations"][0])

    def test_export_requires_staff(self):
        with self.assertRaises(ArchiveError):
            self.archive.export_class_term("s-ba", "c-7y1")

    def test_export_json_is_serializable(self):
        self.archive.create_viewpoint("s-ba", "vp-1", "class", self.anchor_27(),
                                      "发言", class_id="c-7y1", publish=True)
        blob = json.dumps(self.archive.export_class_term("t-wang", "c-7y1"),
                          ensure_ascii=False)
        self.assertIn("西游记", blob)

    # ----- 权限与持久化 ---------------------------------------------------

    def test_student_cannot_register_curriculum(self):
        with self.assertRaises(ArchiveError):
            self.archive.register_work("s-ba", "w-x", "水浒")

    def test_event_log_persistence_roundtrip(self):
        a = self.archive
        a.create_viewpoint("s-ba", "vp-keep", "class", self.anchor_27(),
                           "需要跨进程保留的发言", class_id="c-7y1", publish=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            # 模拟进程重启：把已有事件日志落盘，从同一份 JSONL 重放后继续追加
            path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in a.store.events) + "\n",
                encoding="utf-8")
            reloaded = ReadingArchive(EventStore(path, clock=Clock()))
            self.assertTrue(reloaded.can_view("vp-keep", "s-ba"))
            reloaded.create_viewpoint("s-lin", "vp-keep2", "group",
                                      self.anchor_27(), "第二条",
                                      group_id="g-team1", publish=True)
            seq_count = len(reloaded.store.events)

            reloaded_again = ReadingArchive(EventStore(path, clock=Clock()))
            self.assertEqual(len(reloaded_again.store.events), seq_count)
            self.assertTrue(reloaded_again.can_view("vp-keep", "s-ba"))
            self.assertTrue(reloaded_again.can_view("vp-keep2", "s-ba"))
            self.assertEqual(reloaded_again.provenance("vp-keep")["version"]["isbn"],
                             "JC-2024")
            # JSONL 每行一条事件
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
            self.assertEqual(len(lines), seq_count)
            for line in lines:
                json.loads(line)


if __name__ == "__main__":
    unittest.main()
