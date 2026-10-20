"""云效 Codeup OpenAPI 客户端。

只用标准库。所有请求带 x-yunxiao-token 头。
注意 /oapi/v1/codeup/organizations/<orgId>/... 里的 <orgId> 是组织 ID，
而 repositories/<repo> 里的 <repo> 是**仓库全路径 URL 编码**，不是项目 ID——
这两者混用不会报错，只会 404/500，排查起来很费时间。
"""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request


class CodeupError(Exception):
    pass


def http_json(method, url, headers=None, body=None, timeout=60):
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, _maybe_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read().decode("utf-8", "replace"))
    except Exception as exc:
        return 0, {"error": repr(exc)}


def _maybe_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def as_list(data):
    """云效不同接口的返回结构不统一，这里统一抹平。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "result", "list", "items", "records", "changeRequests"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


class Codeup(object):
    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def encode_repo(repo):
        """仓库全路径必须 URL 编码，且用 '=' 之外的 %2F 形式。"""
        return repo.replace("/", "%2F") if "/" in repo else repo

    def _call(self, method, path, params=None, body=None):
        url = self.cfg.domain + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return http_json(
            method, url,
            headers={"x-yunxiao-token": self.cfg.token, "Accept": "application/json"},
            body=body, timeout=self.cfg.timeout,
        )

    # ---------------- 合并请求 ----------------
    def list_change_requests(self, repo, state="opened", page=1, per_page=100):
        path = "/oapi/v1/codeup/organizations/%s/changeRequests" % self.cfg.org_id
        return self._call("GET", path, params={
            "projectIds": repo, "state": state, "page": page, "perPage": per_page,
        })

    def list_patchsets(self, repo, local_id):
        path = ("/oapi/v1/codeup/organizations/%s/repositories/%s"
                "/changeRequests/%s/diffs/patches"
                % (self.cfg.org_id, self.encode_repo(repo), local_id))
        return self._call("GET", path)

    def create_comment(self, repo, local_id, content,
                       comment_type="GLOBAL_COMMENT", file_path=None,
                       line_number=None, patchset_biz_id=None):
        """发评论。content 硬上限 65535 字符，超了整条失败。"""
        path = ("/oapi/v1/codeup/organizations/%s/repositories/%s"
                "/changeRequests/%s/comments"
                % (self.cfg.org_id, self.encode_repo(repo), local_id))
        body = {"comment_type": comment_type, "content": content, "resolved": False}
        if file_path:
            body["file_path"] = file_path
        if line_number is not None:
            body["line_number"] = line_number
        if patchset_biz_id:
            body["patchset_biz_id"] = patchset_biz_id
        return self._call("POST", path, body=body)

    def create_commit_comment(self, repo, sha, content):
        """分支评审的落点。注意：提交评论**不能行内**，没有 file_path 概念。"""
        path = ("/oapi/v1/codeup/organizations/%s/repositories/%s/commits/%s/comments"
                % (self.cfg.org_id, self.encode_repo(repo), sha))
        return self._call("POST", path, body={"content": content})

    def list_branches(self, repo):
        path = ("/oapi/v1/codeup/organizations/%s/repositories/%s/branches"
                % (self.cfg.org_id, self.encode_repo(repo)))
        return self._call("GET", path)


def opened_mrs(cu, cfg, only_repo=None):
    repos = [only_repo] if only_repo else cfg.repos
    out = []
    for r in repos:
        status, data = cu.list_change_requests(r["id"], state="opened")
        if status != 200:
            continue
        for raw in as_list(data):
            out.append(norm_mr(raw, r))
    return out


def norm_mr(raw, repo):
    """把云效的 changeRequest 抹平成内部结构。字段名各家文档不一致，全部做兼容。"""
    return {
        "repo": repo,
        "local_id": raw.get("localId") or raw.get("local_id") or raw.get("iid"),
        "title": raw.get("title") or "",
        "source": raw.get("sourceBranch") or raw.get("source_branch") or "",
        "target": raw.get("targetBranch") or raw.get("target_branch") or "",
        "author": (raw.get("author") or {}).get("name", ""),
        "url": raw.get("webUrl") or raw.get("web_url") or "",
        "raw": raw,
    }


def mr_version(cu, repo, local_id):
    """取当前最新版本号。

    ★ 核心坑：patchsets 列表里最后一项是 Base（relatedMergeItemType=MERGE_TARGET），
      而 Base 在目标分支不动时**恒定不变**。如果取 plist[-1]，会一直判定"已评审过"，
      导致后续所有新提交被**静默跳过**——评论看起来正常，但评的是旧代码。

    正确做法：只取 MERGE_SOURCE，按 versionNo 升序，最后一个才是最新。
    """
    status, data = cu.list_patchsets(repo["id"], local_id)
    if status != 200:
        return None
    items = []
    for p in as_list(data):
        kind = str(p.get("relatedMergeItemType") or "").upper()
        if kind and kind != "MERGE_SOURCE":
            continue                      # 跳过 Base / MERGE_TARGET
        vno = p.get("versionNo") or p.get("version_no") or 0
        try:
            vno = int(vno)
        except Exception:
            vno = 0
        items.append((vno, p.get("patchSetBizId") or p.get("patchSetBizID") or ""))
    if not items:
        return None
    items.sort(key=lambda x: x[0])
    return items[-1][1]
