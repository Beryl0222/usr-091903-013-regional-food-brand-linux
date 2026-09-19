"""地域餐饮品牌准入的基础运行入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from brand import BrandRegistry

SERVICE_ID = "regional-food-brand"
SERVICE_NAME = "地域餐饮品牌准入"

REGISTRY = BrandRegistry()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def public_summary_payload():
    """返回面向公众的克制摘要：当前有效门店、标准版本与退出原因。"""
    return {"service": SERVICE_ID, "summary": REGISTRY.public_summary()}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与公众摘要，并为领域接口保留清晰入口。"""

    def do_GET(self):
        if self.path == "/health":
            payload = health_payload()
        elif self.path == "/public/summary":
            payload = public_summary_payload()
        else:
            self.send_error(404)
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
        assert health_payload()["service"] == SERVICE_ID
        assert set(public_summary_payload()["summary"]) == {
            "stores",
            "standards",
            "exits",
        }
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
