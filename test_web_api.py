"""HTTP 接口端到端测试：从建版本锚点到发布、撤回、as_of 复原与期末导出。"""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eventlog import EventLog
from archive import ReadingArchive
from service import build_handler

_BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def ts(day, hh=8):
    return (_BASE + timedelta(days=day - 1, hours=hh)).isoformat()


class WebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 业务事件均显式带 at；时钟只用于"未给 as_of 的导出窗口"，
        # 保持真实当前时间即可（测试情境中的学期跨越当下）。
        archive = ReadingArchive(EventLog())
        cls.handler = build_handler(archive)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, payload=None, actor=None, as_of=None):
        url = f"{self.base}{path}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if actor:
            headers["X-Actor"] = actor
        if as_of:
            headers["X-As-Of"] = as_of
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            exc.body = json.load(exc)
            raise

    def expect_error(self, status, method, path, payload=None, actor=None,
                     as_of=None):
        try:
            self.call(method, path, payload, actor, as_of=as_of)
        except HTTPError as exc:
            self.assertEqual(exc.code, status)
            return exc.body
        self.fail(f"期望 {status}，但请求成功")

    def test_01_full_reading_term_journey(self):
        # --- 作品与两版教材（版本登记需教师身份，先建账号） ---
        status, _ = self.call("POST", "/works", {
            "work_id": "xyj", "title": "西游记", "author": "吴承恩",
            "min_age": 12, "at": ts(1)})
        self.assertEqual(status, 201)

        # --- 用户、监护关系 ---
        for uid, name, role, age in (
                ("t_wang", "王老师", "teacher", None),
                ("s1", "张三", "student", 13),
                ("s2", "李四", "student", 13),
                ("g1", "张三家长", "guardian", None)):
            self.call("POST", "/users", {
                "user_id": uid, "name": name, "role": role, "age": age,
                "at": ts(1)})

        self.call("POST", "/editions", {
            "edition_id": "ed2018", "work_id": "xyj",
            "name": "人教版2018节选", "year": 2018,
            "at": ts(1)}, actor="t_wang")
        self.expect_error(403, "POST", "/editions", {
            "edition_id": "edX", "work_id": "xyj", "name": "X",
            "at": ts(1)}, actor="s1")
        self.call("POST", "/editions", {
            "edition_id": "ed2024", "work_id": "xyj",
            "name": "统编版2024七上", "year": 2024, "at": ts(1)},
            actor="t_wang")
        self.call("POST", "/anchors", {
            "anchor_id": "a7", "edition_id": "ed2018",
            "chapter_no": 7, "chapter_title": "八卦炉中逃大圣",
            "locator": "上册第82页", "at": ts(1)}, actor="t_wang")

        self.call("POST", "/guardianships/link",
                  {"student_id": "s1", "guardian_id": "g1", "at": ts(1)})

        # --- 班级与学期、路线 ---
        self.call("POST", "/classes", {
            "class_id": "c1", "name": "初一1班", "grade": "初一",
            "at": ts(1)}, actor="t_wang")
        self.call("POST", "/classes/c1/enrollments",
                  {"user_id": "s1", "at": ts(1)})
        self.call("POST", "/classes/c1/enrollments",
                  {"user_id": "s2", "at": ts(1)})
        self.call("POST", "/classes/c1/terms", {
            "term_id": "term2026autumn", "start_at": ts(1),
            "end_at": ts(120), "at": ts(1)}, actor="t_wang")
        self.call("POST", "/routes", {
            "route_id": "r1", "title": "七上路线",
            "class_id": "c1", "term_id": "term2026autumn",
            "at": ts(2)}, actor="t_wang")
        self.call("POST", "/routes/r1/items", {
            "anchor_id": "a7", "stage_label": "精读", "at": ts(2)},
            actor="t_wang")

        # --- 学生写观点：未发布前他人 404 ---
        self.call("POST", "/viewpoints", {
            "viewpoint_id": "vp1", "author_id": "s1",
            "primary_anchor_id": "a7",
            "summary": "孙悟空反抗等级秩序", "at": ts(5)})
        self.expect_error(404, "GET", "/viewpoints/vp1", actor="s2")
        self.call("POST", "/viewpoints/vp1/evidence", {
            "anchor_id": "a7", "quote": "皇帝轮流做，明年到我家",
            "at": ts(5)}, actor="s1")

        # --- 提交、退回、再提交、发布 ---
        self.call("POST", "/viewpoints/vp1/decisions",
                  {"decision": "submit", "at": ts(6)}, actor="s1")
        self.call("POST", "/viewpoints/vp1/decisions", {
            "decision": "return", "reason": "补一条原文", "at": ts(7)},
            actor="t_wang")
        self.call("POST", "/viewpoints/vp1/evidence", {
            "anchor_id": "a7", "quote": "五行山下定心猿", "at": ts(8)},
            actor="s1")
        self.call("POST", "/viewpoints/vp1/decisions",
                  {"decision": "submit", "at": ts(9)}, actor="s1")
        self.expect_error(403, "POST", "/viewpoints/vp1/decisions",
                          {"decision": "publish", "scopes": ["class"],
                           "at": ts(10)}, actor="s1")
        self.call("POST", "/viewpoints/vp1/decisions", {
            "decision": "publish", "scopes": ["class", "guardians"],
            "at": ts(10)}, actor="t_wang")

        status, vp = self.call("GET", "/viewpoints/vp1", actor="s2")
        self.assertEqual(status, 200)
        self.assertEqual(vp["status"], "published")
        self.assertEqual(len(vp["evidence"]), 2)
        status, vp_g = self.call("GET", "/viewpoints/vp1", actor="g1")
        self.assertEqual(vp_g["author_name"], "张三")

        # --- 同学参与讨论 ---
        self.call("POST", "/viewpoints/vp1/comments", {
            "body": "蟠桃会的民俗细节也支持这点", "at": ts(11)}, actor="s2")

        # --- 撤回批注：学生与家长立刻 404；教师仍可溯源 ---
        self.call("POST", "/viewpoints/vp1/decisions", {
            "decision": "withhold", "reason": "批注尚未准备好对家长公开",
            "at": ts(12)}, actor="t_wang")
        self.expect_error(404, "GET", "/viewpoints/vp1", actor="s2")
        self.expect_error(404, "GET", "/viewpoints/vp1", actor="g1")
        status, prov = self.call("GET", "/viewpoints/vp1/provenance",
                                 actor="t_wang")
        self.assertEqual(prov["status"], "withheld")
        self.assertEqual(prov["decisions"][-1]["by"], "t_wang")

        # --- 重新发布后换教材：旧版本身份仍在证据里 ---
        self.call("POST", "/viewpoints/vp1/decisions", {
            "decision": "publish", "scopes": ["class"], "at": ts(13)},
            actor="t_wang")
        self.call("POST", "/anchors", {
            "anchor_id": "a7n", "edition_id": "ed2024",
            "chapter_no": 21, "chapter_title": "小圣施威降大圣",
            "at": ts(40)}, actor="t_wang")
        self.call("POST", "/editions/ed2018/supersede", {
            "by_edition_id": "ed2024", "reason": "换用新版教材",
            "at": ts(40)}, actor="t_wang")
        status, vp_now = self.call("GET", "/viewpoints/vp1", actor="s2")
        self.assertEqual(
            vp_now["evidence"][0]["anchor"]["edition"]["superseded_by"],
            "ed2024")

        # --- as_of：回到撤回当时，s2 仍不可见；回到发布当时可见 ---
        self.expect_error(404, "GET", "/viewpoints/vp1", actor="s2",
                          as_of=ts(12))
        status, vp_past = self.call("GET", "/viewpoints/vp1", actor="s2",
                                    as_of=ts(10))
        self.assertEqual(vp_past["status"], "published")

        # --- 离校：本人 403，讨论不断链，导出仍含其发言 ---
        self.call("POST", "/users/s1/depart",
                  {"reason": "转学", "at": ts(50)}, actor="t_wang")
        body = self.expect_error(403, "GET", "/viewpoints/vp1", actor="s1")
        self.assertEqual(body["error"], "forbidden")
        status, vp_kept = self.call("GET", "/viewpoints/vp1", actor="s2")
        self.assertEqual(vp_kept["author_id"], "s1")
        self.assertEqual(vp_kept["comments"][0]["author_id"], "s2")

        # --- 改编作品：仅元数据 + 原文关联 ---
        self.call("POST", "/adaptations", {
            "work_id": "wukong", "title": "悟空传", "author": "今何在",
            "source_work_id": "xyj",
            "bibliographic_note": "2001年初版；对照讨论用", "at": ts(60)})
        self.expect_error(400, "POST", "/editions", {
            "edition_id": "edwk", "work_id": "wukong", "name": "X",
            "at": ts(60)}, actor="t_wang")
        self.call("POST", "/adaptation-links", {
            "adaptation_work_id": "wukong", "anchor_id": "a7",
            "adapted_locator": "第三章·天宫", "at": ts(61)},
            actor="t_wang")

        # --- 期末导出：读过/引用过/讨论过 + 每条观点履历 ---
        status, export = self.call(
            "GET", "/classes/c1/terms/term2026autumn/export",
            actor="t_wang")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(export["coverage"]["read"], 1)
        self.assertGreaterEqual(export["coverage"]["cited"], 1)
        vp_export = next(v for v in export["viewpoints"]
                         if v["viewpoint_id"] == "vp1")
        self.assertEqual(vp_export["author_id"], "s1")
        self.assertEqual(vp_export["primary_anchor"]["edition_id"],
                         "ed2018")
        chain = [d["decision"] for d in vp_export["decisions"]]
        self.assertEqual(chain, [
            "submitted", "returned", "submitted", "published",
            "withheld", "published"])
        self.assertEqual(vp_export["comments"][0]["body"],
                         "蟠桃会的民俗细节也支持这点")
        self.assertIn("routes", export)
        self.assertEqual(export["materials"][0]["edition"]["name"],
                         "人教版2018节选")

    def test_health_contract_alongside_business_routes(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "classic-reading")
        self.expect_error(404, "GET", "/no-such-path")

    def test_missing_actor_is_forbidden_on_views(self):
        self.expect_error(403, "GET", "/viewpoints")


if __name__ == "__main__":
    unittest.main()
