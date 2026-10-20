"""Webhook 入口。

云效两种事件形态完全不同：

  MR 事件    → 带 number / sourceBranch / targetBranch，可以"直评"
  push 事件  → **只带 ref 和 repository，没有 MR 编号**

所以 push 事件只能走兜底：扫这个仓库所有 opened MR，靠版本号去重，
谁的版本变了就评谁。这不是 bug，是**有意设计**——云效在"MR 已建、之后又推提交"
时不一定会再发 MR 事件，只能靠 push 来对账。

⚠️ 因此云效侧 **必须勾上 push 事件**，否则新提交会漏评，却照样能合并。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .codeup import Codeup, mr_version, norm_mr, opened_mrs
from .diffutil import diff_stats, make_diff, split_diff_batches
from .reviewer import merge_batch_results, post_review, render_comment, review_batches
from . import notify

SKIP_ACTIONS = {"merge", "close", "delete", "reject"}


def extract_mr(payload):
    """MR 事件里挖出 MR 信息。字段名各版本不一致，多做兼容。"""
    obj = payload.get("object_attributes") or payload.get("objectAttributes") or {}
    local_id = (obj.get("iid") or obj.get("localId") or obj.get("number")
                or payload.get("number") or payload.get("localId"))
    if not local_id:
        return None
    repo = payload.get("repository") or {}
    return {
        "local_id": local_id,
        "title": obj.get("title") or payload.get("title") or "",
        "source": obj.get("source_branch") or obj.get("sourceBranch") or "",
        "target": obj.get("target_branch") or obj.get("targetBranch") or "",
        "url": obj.get("url") or payload.get("url") or repo.get("git_http_url") or "",
        "repo_name": repo.get("name") or "",
    }


def push_info(payload):
    """push 事件：只有 ref 和 repository，没有 MR 编号。"""
    ref = payload.get("ref") or ""
    after = payload.get("after") or ""
    if not ref.startswith("refs/heads/") or not after or set(after) == {"0"}:
        return None
    return {
        "branch": ref[len("refs/heads/"):],
        "after": after,
        "repo_name": (payload.get("repository") or {}).get("name") or "",
    }


class Handler(BaseHTTPRequestHandler):
    cfg = None
    cu = None
    state = {}

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        cfg = self.cfg
        if cfg.webhook_token:
            got = self.headers.get("X-Webhook-Token") or self.headers.get("X-Codeup-Token")
            if got != cfg.webhook_token:
                return self._reply(401, {"ok": False, "msg": "unauthorized"})

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return self._reply(400, {"ok": False, "msg": "bad json"})

        print("收到事件：%d 字节" % len(raw), flush=True)

        # 先回 200，再异步处理：评审要几分钟，卡住 webhook 会被云效判定超时重推
        self._reply(202, {"ok": True, "msg": "accepted"})

        def work():
            try:
                self.handle_payload(payload)
            except Exception as exc:
                print("处理失败：%r" % (exc,), flush=True)

        threading.Thread(target=work, daemon=True).start()

    def handle_payload(self, payload):
        cfg, cu = self.cfg, self.cu
        action = (payload.get("object_attributes") or {}).get("action") or payload.get("action")
        if action in SKIP_ACTIONS:
            print("跳过动作：%s" % action, flush=True)
            return

        mr = extract_mr(payload)
        if mr:
            repo = self.match_repo(mr.get("repo_name"))
            if repo:
                mr["repo"] = repo
                self.review_mr(mr)
            return

        info = push_info(payload)
        if info:
            repo = self.match_repo(info["repo_name"])
            if not repo:
                return
            # 兜底：扫这个仓库所有 opened MR，靠版本去重
            for m in opened_mrs(cu, cfg, only_repo=repo):
                if m.get("local_id"):
                    self.review_mr(m)

    def match_repo(self, name):
        if not name:
            return None
        for r in self.cfg.repos:
            url = r.get("gitUrl") or ""
            if url.rstrip("/").endswith("/" + name) or url.rstrip("/").endswith("/" + name + ".git"):
                return r
        return None

    def review_mr(self, mr):
        cfg, cu = self.cfg, self.cu
        key = "%s#%s" % (mr["repo"]["id"], mr["local_id"])

        ver = mr_version(cu, mr["repo"], mr["local_id"])
        if ver and self.state.get(key) == ver:
            print("%s 版本未变，跳过" % key, flush=True)
            return
        if ver:
            self.state[key] = ver

        diff = make_diff(cfg, mr["repo"], mr["source"], mr["target"])
        if not diff.strip():
            print("%s diff 为空，跳过" % key, flush=True)
            return

        stats = diff_stats(diff)
        batches, all_files, truncated = split_diff_batches(
            diff, cfg.max_diff_chars, cfg.max_batches)
        print("%s 分 %d 批评审（每批 ≤ %d 字符）"
              % (key, len(batches), cfg.max_diff_chars), flush=True)

        results, metas = review_batches(cfg, mr, batches, all_files)
        merged = merge_batch_results(results)
        text = render_comment(cfg, mr, merged, stats, truncated, metas)

        if cfg.dry_run:
            print(text, flush=True)
            return

        post_review(cu, cfg, mr, text)
        notify.notify_review([], mr, merged)


def serve(cfg, cu):
    Handler.cfg = cfg
    Handler.cu = cu
    Handler.state = {}
    srv = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    print("webhook 监听 0.0.0.0:8080", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    from .config import Config
    c = Config()
    c.require()
    serve(c, Codeup(c))
