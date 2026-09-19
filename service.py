"""名著多角度阅读档案——HTTP 入口。

保留稳定的 /health 契约；业务接口均为 JSON：

* 请求头 ``X-Actor`` 标明操作人（用户 id）；
* 查询接口可用 ``X-As-Of``（或导出接口的 ``?as_of=``）回到任意历史时点；
* 404 同时表示"不存在"与"对你不可见"，避免借探测暴露未发布批注。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from eventlog import EventError, EventLog, utc_now
from archive import (
    ReadingArchive,
    NotFoundError, ValidationError, AccessDeniedError,
)

SERVICE_ID = "classic-reading"
SERVICE_NAME = "名著多角度阅读档案"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# ---- 路由表：方法 + 路径模板（<名> 为路径变量） -------------------------

ROUTES = [
    ("GET",    "/health",                              "health"),
    ("GET",    "/works",                               "list_works"),
    ("POST",   "/works",                               "register_work"),
    ("POST",   "/adaptations",                         "register_adaptation"),
    ("POST",   "/adaptation-links",                    "add_adaptation_link"),
    ("POST",   "/editions",                            "add_edition"),
    ("POST",   "/editions/<edition_id>/supersede",     "supersede_edition"),
    ("GET",    "/anchors/<anchor_id>",                 "get_anchor"),
    ("POST",   "/anchors",                             "register_anchor"),
    ("POST",   "/anchors/<anchor_id>/aliases",         "add_anchor_alias"),

    ("POST",   "/users",                               "register_user"),
    ("POST",   "/users/<user_id>/depart",              "depart_user"),
    ("POST",   "/guardianships/link",                  "link_guardian"),
    ("POST",   "/guardianships/unlink",                "unlink_guardian"),

    ("POST",   "/classes",                             "create_class"),
    ("POST",   "/classes/<class_id>/enrollments",      "add_enrollment"),
    ("POST",   "/classes/<class_id>/enrollments/<user_id>/end", "end_enrollment"),
    ("POST",   "/classes/<class_id>/terms",            "open_term"),
    ("POST",   "/classes/<class_id>/terms/<term_id>/close", "close_term"),
    ("GET",    "/classes/<class_id>/terms/<term_id>/export", "export_term"),

    ("POST",   "/groups",                              "create_group"),
    ("POST",   "/groups/<group_id>/members",           "add_group_member"),
    ("POST",   "/groups/<group_id>/members/<user_id>/remove", "remove_group_member"),

    ("POST",   "/routes",                              "create_route"),
    ("GET",    "/routes/<route_id>",                   "get_route"),
    ("POST",   "/routes/<route_id>/items",             "add_route_item"),
    ("POST",   "/routes/<route_id>/items/<item_id>/remove", "remove_route_item"),
    ("POST",   "/routes/<route_id>/items/<item_id>/reorder", "reorder_route_item"),
    ("POST",   "/routes/<route_id>/retire",            "retire_route"),

    ("POST",   "/viewpoints",                          "open_viewpoint"),
    ("GET",    "/viewpoints",                          "list_viewpoints"),
    ("GET",    "/viewpoints/<viewpoint_id>",           "get_viewpoint"),
    ("POST",   "/viewpoints/<viewpoint_id>/evidence",  "add_evidence"),
    ("POST",   "/viewpoints/<viewpoint_id>/evidence/<evidence_id>/remove",
                                                                 "remove_evidence"),
    ("POST",   "/viewpoints/<viewpoint_id>/comments",  "add_comment"),
    ("POST",   "/viewpoints/<viewpoint_id>/comments/<comment_id>/remove",
                                                                 "remove_comment"),
    ("POST",   "/viewpoints/<viewpoint_id>/decisions", "make_decision"),
    ("GET",    "/viewpoints/<viewpoint_id>/provenance", "provenance"),
]


def _match_route(method, path):
    for route_method, template, action in ROUTES:
        if route_method != method:
            continue
        t_parts = [p for p in template.strip("/").split("/") if p]
        p_parts = [p for p in path.strip("/").split("/") if p]
        if len(t_parts) != len(p_parts):
            continue
        kwargs = {}
        for t, p in zip(t_parts, p_parts):
            if t.startswith("<") and t.endswith(">"):
                kwargs[t[1:-1]] = p
            elif t != p:
                break
        else:
            return action, kwargs
    return None, None


class Handler(BaseHTTPRequestHandler):
    """JSON over HTTP；共享一个进程内（或 JSONL 持久化的）阅读档案。"""

    archive = ReadingArchive()

    # ---- 通用收发 -------------------------------------------------------

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _actor(self, body):
        actor = self.headers.get("X-Actor") or body.pop("actor_id", None)
        return actor or None

    def _as_of(self, query=None):
        return self.headers.get("X-As-Of") or (query or {}).get("as_of", [None])[0]

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        action, kwargs = _match_route(method, parsed.path)
        if action is None:
            self._send_json(
                {"error": "not_found", "message": f"未知路由：{parsed.path}"},
                404)
            return
        query = parse_qs(parsed.query)
        try:
            body = self._read_body() if method == "POST" else {}
            getattr(self, f"do_{action}")(body, kwargs, query)
        except (NotFoundError,) as exc:
            self._send_json({"error": "not_found", "message": str(exc)}, 404)
        except AccessDeniedError as exc:
            self._send_json({"error": "forbidden", "message": str(exc)}, 403)
        except ValidationError as exc:
            self._send_json({"error": "validation", "message": str(exc)}, 400)
        except EventError as exc:
            self._send_json({"error": "conflict", "message": str(exc)}, 409)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, *_args):
        return

    # ---- 健康检查 -------------------------------------------------------

    def do_health(self, body, kwargs, query):
        self._send_json(health_payload())

    # ---- 作品 / 改编 ----------------------------------------------------

    def do_list_works(self, body, kwargs, query):
        works = self.archive.list_works(as_of=self._as_of(query))
        self._send_json({"works": works})

    def do_register_work(self, body, kwargs, query):
        work_id = self._req(body, "work_id")
        self.archive.register_work(
            work_id, self._req(body, "title"), self._req(body, "author"),
            kind=body.get("kind", "original"), min_age=body.get("min_age"),
            at=body.get("at"))
        self._send_json({"work_id": work_id}, 201)

    def do_register_adaptation(self, body, kwargs, query):
        work_id = self._req(body, "work_id")
        self.archive.register_adaptation(
            work_id, self._req(body, "title"), self._req(body, "author"),
            self._req(body, "source_work_id"),
            bibliographic_note=body.get("bibliographic_note", ""),
            min_age=body.get("min_age"), at=body.get("at"))
        self._send_json({"work_id": work_id}, 201)

    def do_add_adaptation_link(self, body, kwargs, query):
        link_id = body.get("link_id") or self.archive._new_id("link")
        self.archive.add_adaptation_link(
            link_id, self._req(body, "adaptation_work_id"),
            self._req(body, "anchor_id"),
            adapted_locator=body.get("adapted_locator", ""),
            note=body.get("note", ""), actor_id=self._actor(body),
            at=body.get("at"))
        self._send_json({"link_id": link_id}, 201)

    # ---- 版本 / 锚点 ----------------------------------------------------

    def do_add_edition(self, body, kwargs, query):
        edition_id = self._req(body, "edition_id")
        self.archive.add_edition(
            edition_id, self._req(body, "work_id"),
            self._req(body, "name"), publisher=body.get("publisher", ""),
            year=body.get("year"),
            bibliographic_note=body.get("bibliographic_note", ""),
            external_ref=body.get("external_ref", ""),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"edition_id": edition_id}, 201)

    def do_supersede_edition(self, body, kwargs, query):
        self.archive.supersede_edition(
            kwargs["edition_id"], self._req(body, "by_edition_id"),
            reason=body.get("reason", ""), actor_id=self._actor(body),
            at=body.get("at"))
        self._send_json({"edition_id": kwargs["edition_id"],
                         "superseded_by": body["by_edition_id"]})

    def do_get_anchor(self, body, kwargs, query):
        self._send_json(self.archive.get_anchor(
            kwargs["anchor_id"], as_of=self._as_of(query)))

    def do_register_anchor(self, body, kwargs, query):
        anchor_id = self._req(body, "anchor_id")
        self.archive.register_anchor(
            anchor_id, self._req(body, "edition_id"),
            self._req(body, "chapter_no"), self._req(body, "chapter_title"),
            locator=body.get("locator", ""),
            quote_start=body.get("quote_start", ""),
            quote_end=body.get("quote_end", ""),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"anchor_id": anchor_id}, 201)

    def do_add_anchor_alias(self, body, kwargs, query):
        self.archive.add_anchor_alias(
            kwargs["anchor_id"], self._req(body, "alias_kind"),
            self._req(body, "value"), actor_id=self._actor(body),
            at=body.get("at"))
        self._send_json({"anchor_id": kwargs["anchor_id"]})

    # ---- 用户 / 监护 / 离校 ---------------------------------------------

    def do_register_user(self, body, kwargs, query):
        user_id = self._req(body, "user_id")
        self.archive.register_user(
            user_id, self._req(body, "name"), self._req(body, "role"),
            age=body.get("age"), at=body.get("at"))
        self._send_json({"user_id": user_id}, 201)

    def do_depart_user(self, body, kwargs, query):
        self.archive.depart_user(
            kwargs["user_id"], reason=body.get("reason", ""),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"user_id": kwargs["user_id"], "status": "departed"})

    def do_link_guardian(self, body, kwargs, query):
        self.archive.link_guardian(
            self._req(body, "student_id"), self._req(body, "guardian_id"),
            at=body.get("at"))
        self._send_json({"linked": True})

    def do_unlink_guardian(self, body, kwargs, query):
        self.archive.unlink_guardian(
            self._req(body, "student_id"), self._req(body, "guardian_id"),
            at=body.get("at"))
        self._send_json({"unlinked": True})

    # ---- 班级 / 学期 ----------------------------------------------------

    def do_create_class(self, body, kwargs, query):
        class_id = self._req(body, "class_id")
        self.archive.create_class(
            class_id, self._req(body, "name"), self._req(body, "grade"),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"class_id": class_id}, 201)

    def do_add_enrollment(self, body, kwargs, query):
        self.archive.add_enrollment(
            kwargs["class_id"], self._req(body, "user_id"), at=body.get("at"))
        self._send_json({"enrolled": True}, 201)

    def do_end_enrollment(self, body, kwargs, query):
        self.archive.end_enrollment(
            kwargs["class_id"], kwargs["user_id"],
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"enrollment_ended": True})

    def do_open_term(self, body, kwargs, query):
        term_id = self._req(body, "term_id")
        self.archive.open_term(
            kwargs["class_id"], term_id, self._req(body, "start_at"),
            end_at=body.get("end_at"), actor_id=self._actor(body),
            at=body.get("at"))
        self._send_json({"term_id": term_id}, 201)

    def do_close_term(self, body, kwargs, query):
        self.archive.close_term(
            kwargs["class_id"], kwargs["term_id"],
            end_at=body.get("end_at"), actor_id=self._actor(body),
            at=body.get("at"))
        self._send_json({"term_id": kwargs["term_id"], "closed": True})

    def do_export_term(self, body, kwargs, query):
        result = self.archive.export_term(
            kwargs["class_id"], kwargs["term_id"],
            actor_id=self.headers.get("X-Actor"),
            as_of=self._as_of(query))
        self._send_json(result)

    # ---- 小组 -----------------------------------------------------------

    def do_create_group(self, body, kwargs, query):
        group_id = self._req(body, "group_id")
        self.archive.create_group(
            group_id, self._req(body, "name"), self._req(body, "class_id"),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"group_id": group_id}, 201)

    def do_add_group_member(self, body, kwargs, query):
        self.archive.add_group_member(
            kwargs["group_id"], self._req(body, "user_id"), at=body.get("at"))
        self._send_json({"added": True}, 201)

    def do_remove_group_member(self, body, kwargs, query):
        self.archive.remove_group_member(
            kwargs["group_id"], kwargs["user_id"], at=body.get("at"))
        self._send_json({"removed": True})

    # ---- 阅读路线 -------------------------------------------------------

    def do_create_route(self, body, kwargs, query):
        route_id = self._req(body, "route_id")
        self.archive.create_route(
            route_id, self._req(body, "title"),
            self._req(body, "class_id"), self._req(body, "term_id"),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"route_id": route_id}, 201)

    def do_get_route(self, body, kwargs, query):
        self._send_json(self.archive.get_route(
            kwargs["route_id"], as_of=self._as_of(query)))

    def do_add_route_item(self, body, kwargs, query):
        item_id = self.archive.add_route_item(
            kwargs["route_id"], self._req(body, "anchor_id"),
            order_no=body.get("order_no"),
            stage_label=body.get("stage_label", ""),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"item_id": item_id}, 201)

    def do_remove_route_item(self, body, kwargs, query):
        self.archive.remove_route_item(
            kwargs["route_id"], kwargs["item_id"],
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"item_id": kwargs["item_id"], "removed": True})

    def do_reorder_route_item(self, body, kwargs, query):
        self.archive.reorder_route_item(
            kwargs["route_id"], kwargs["item_id"],
            self._req(body, "order_no"),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"item_id": kwargs["item_id"],
                         "order_no": body["order_no"]})

    def do_retire_route(self, body, kwargs, query):
        self.archive.retire_route(
            kwargs["route_id"], reason=body.get("reason", ""),
            actor_id=self._actor(body), at=body.get("at"))
        self._send_json({"route_id": kwargs["route_id"], "retired": True})

    # ---- 观点 / 证据 / 评论 ---------------------------------------------

    def do_open_viewpoint(self, body, kwargs, query):
        vp_id = self._req(body, "viewpoint_id")
        self.archive.open_viewpoint(
            vp_id, self._req(body, "author_id"),
            self._req(body, "primary_anchor_id"),
            self._req(body, "summary"), group_id=body.get("group_id"),
            at=body.get("at"))
        self._send_json({"viewpoint_id": vp_id}, 201)

    def do_list_viewpoints(self, body, kwargs, query):
        actor = self.headers.get("X-Actor")
        if not actor:
            raise AccessDeniedError("缺少 X-Actor 请求头")
        self._send_json({"viewpoints": self.archive.list_viewpoints(
            actor, class_id=query.get("class_id", [None])[0],
            anchor_id=query.get("anchor_id", [None])[0],
            as_of=self._as_of(query))})

    def do_get_viewpoint(self, body, kwargs, query):
        actor = self.headers.get("X-Actor")
        if not actor:
            raise AccessDeniedError("缺少 X-Actor 请求头")
        self._send_json(self.archive.get_viewpoint(
            kwargs["viewpoint_id"], actor, as_of=self._as_of(query)))

    def do_add_evidence(self, body, kwargs, query):
        evidence_id = self.archive.add_evidence(
            kwargs["viewpoint_id"], self._req(body, "anchor_id"),
            self._req_actor(body), quote=body.get("quote", ""),
            note=body.get("note", ""),
            adaptation_link_id=body.get("adaptation_link_id"),
            at=body.get("at"))
        self._send_json({"evidence_id": evidence_id}, 201)

    def do_remove_evidence(self, body, kwargs, query):
        self.archive.remove_evidence(
            kwargs["viewpoint_id"], kwargs["evidence_id"],
            self._req_actor(body), reason=body.get("reason", ""),
            at=body.get("at"))
        self._send_json({"evidence_id": kwargs["evidence_id"], "removed": True})

    def do_add_comment(self, body, kwargs, query):
        comment_id = self.archive.add_comment(
            kwargs["viewpoint_id"], self._req_actor(body),
            self._req(body, "body"), at=body.get("at"))
        self._send_json({"comment_id": comment_id}, 201)

    def do_remove_comment(self, body, kwargs, query):
        self.archive.remove_comment(
            kwargs["viewpoint_id"], kwargs["comment_id"],
            self._req_actor(body), reason=body.get("reason", ""),
            at=body.get("at"))
        self._send_json({"comment_id": kwargs["comment_id"], "removed": True})

    # ---- 发布决定链 / 溯源 ----------------------------------------------

    def do_make_decision(self, body, kwargs, query):
        decision = self._req(body, "decision")
        vp_id = kwargs["viewpoint_id"]
        actor = self._req_actor(body)
        common = dict(reason=body.get("reason", ""), at=body.get("at"))
        if decision == "submit":
            self.archive.submit(vp_id, actor, at=body.get("at"))
        elif decision == "return":
            self.archive.return_viewpoint(vp_id, actor, **common)
        elif decision == "publish":
            self.archive.publish(vp_id, actor,
                                 self._req(body, "scopes"), at=body.get("at"))
        elif decision == "withhold":
            self.archive.withhold(vp_id, actor, **common)
        elif decision == "withdraw":
            self.archive.withdraw(vp_id, actor, **common)
        else:
            raise ValidationError(
                "decision 必须是 submit/return/publish/withhold/withdraw")
        vp = self.archive.get_viewpoint(
            vp_id, actor) if actor else None
        self._send_json({"viewpoint_id": vp_id,
                         "status": vp["status"] if vp else decision})

    def do_provenance(self, body, kwargs, query):
        self._send_json(self.archive.provenance(
            kwargs["viewpoint_id"], actor_id=self.headers.get("X-Actor"),
            as_of=self._as_of(query)))

    # ---- 小工具 ---------------------------------------------------------

    @staticmethod
    def _req(body, key):
        if key not in body or body[key] in (None, ""):
            raise ValidationError(f"缺少必填字段：{key}")
        return body[key]

    def _req_actor(self, body):
        actor = self._actor(body)
        if not actor:
            raise AccessDeniedError("缺少 X-Actor 请求头")
        return actor


def build_handler(archive):
    """生成绑定指定档案实例的 Handler 类（测试/多租户用）。"""
    return type("BoundHandler", (Handler,), {"archive": archive})


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", default="",
                        help="事件日志 JSONL 路径；提供后崩溃可重放恢复")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert _match_route("GET", "/health")[0] == "health"
        print("基础检查通过")
        return
    if args.data:
        Handler.archive = ReadingArchive(EventLog(path=args.data))
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
