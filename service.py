"""名著多角度阅读档案的基础运行入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import reading_archive

SERVICE_ID = "classic-reading"
SERVICE_NAME = "名著多角度阅读档案"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def check():
    """基础配置检查：服务身份与领域模块均可加载。"""
    assert health_payload()["service"] == SERVICE_ID
    assert reading_archive.ReadingArchive(reading_archive.EventStore()) is not None
    print("基础检查通过")


class Handler(BaseHTTPRequestHandler):
    """提供健康检查，保留后续业务接口的明确入口。"""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check()
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

