"""diff 的生成、统计、切批，以及评论正文的分条。

这里的每个函数都对应一个踩过的坑，注释里写清了"为什么不能那么写"。
"""
import os
import subprocess


def git(args, cwd=None, check=True):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError("git %s 失败: %s" % (" ".join(args), (p.stderr or "")[:300]))
    return p.returncode, p.stdout, p.stderr


def ensure_repo(cfg, repo):
    """把仓库拉到本地（已有则 fetch）。用 token 走 HTTPS。"""
    url = repo["gitUrl"]
    if not url:
        raise RuntimeError("仓库 %s 缺少 gitUrl" % repo.get("id"))
    auth = url.replace("https://", "https://%s:%s@" % (cfg.git_user, cfg.token))
    name = url.rstrip("/").split("/")[-1].replace(".git", "")
    workdir = os.path.join(cfg.repo_dir, name)
    if not os.path.isdir(os.path.join(workdir, ".git")):
        os.makedirs(cfg.repo_dir, exist_ok=True)
        p = subprocess.run(["git", "clone", "--filter=blob:none", auth, workdir],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("clone 失败: %s" % (p.stderr or "")[:300])
    else:
        git(["remote", "set-url", "origin", auth], cwd=workdir, check=False)
    return workdir


def fetch_branches(cfg, repo, branches, workdir):
    args = ["fetch", "origin", "--prune"]
    for b in branches:
        if b:
            args.append("+refs/heads/%s:refs/remotes/origin/%s" % (b, b))
    code, _, err = git(args, cwd=workdir, check=False)
    return code == 0


def make_diff(cfg, repo, source, target):
    """三点比较（target...source），语义等于"这个 MR 相对目标分支改了什么"。

    不要用两点比较（before..after）：那只能看到本次提交，
    会漏掉分支上前几次提交累积的问题。
    """
    workdir = ensure_repo(cfg, repo)
    fetch_branches(cfg, repo, [source, target], workdir)
    excludes = [":(exclude)%s" % pat for pat in cfg.ignore_paths]
    base_args = ["diff", "--no-color", "--no-ext-diff", "--unified=3"]
    tail = (["--"] + excludes) if excludes else []

    code, out, err = git(base_args + ["origin/%s...origin/%s" % (target, source)] + tail,
                         cwd=workdir, check=False)
    if code != 0 or not out:
        # 三点比较在某些分支状态下会失败（没有共同祖先），退化成两点
        code2, out2, err2 = git(base_args + ["origin/%s" % target, "origin/%s" % source] + tail,
                                cwd=workdir, check=False)
        if out2:
            out, code, err = out2, code2, err2 or err
    if code != 0:
        raise RuntimeError("生成 diff 失败: %s" % (err or "未知原因")[:300])
    return out


def diff_stats(diff):
    files = plus = minus = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return {"files": files, "plus": plus, "minus": minus}


def split_by_file(diff):
    """按 `diff --git` 切块。**绝不按字符切**——切碎文件会让模型误判。"""
    blocks, cur = [], []
    for line in diff.splitlines(True):
        if line.startswith("diff --git ") and cur:
            blocks.append("".join(cur))
            cur = []
        cur.append(line)
    if cur:
        blocks.append("".join(cur))
    return blocks


def split_diff_batches(diff, limit, max_batches=8):
    """把按文件的块打包成若干批，每批不超过 limit 字符。

    返回 (batches, all_files)：
      batches   = [{"text": ..., "files": [路径...]}]
      all_files = 全部文件路径（给 prompt 里声明"本批只有其中几个"用）
    """
    blocks = split_by_file(diff)
    batches, cur, cur_files, cur_len = [], [], [], 0
    all_files = []

    for blk in blocks:
        path = _diff_file_path(blk)
        if path:
            all_files.append(path)
        n = len(blk)
        if cur and cur_len + n > limit:
            batches.append({"text": "".join(cur), "files": cur_files})
            cur, cur_files, cur_len = [], [], 0
        # 单文件就超限：只能放进去（不能切碎），让模型自己处理
        cur.append(blk)
        cur_files.append(path)
        cur_len += n

    if cur:
        batches.append({"text": "".join(cur), "files": cur_files})

    truncated = False
    if max_batches and len(batches) > max_batches:
        batches = batches[:max_batches]
        truncated = True
    return batches, all_files, truncated


def _diff_file_path(block):
    first = block.splitlines()[0] if block else ""
    if not first.startswith("diff --git "):
        return ""
    parts = first.split(" b/")
    return parts[-1].strip() if len(parts) > 1 else ""


def split_comment(text, limit, max_parts=8):
    """按行切成多条评论，绝不切碎一行。

    云效 content 硬上限 65535 字符，超出会**整条发不出去**（不是截断，是失败）。
    所以宁可拆成多条，也不要让评审结果丢失。
    """
    if len(text) <= limit:
        return [text]
    parts, cur, cur_len = [], [], 0
    for line in text.split("\n"):
        n = len(line) + 1
        if n > limit:
            if cur:
                parts.append("\n".join(cur))
                cur, cur_len = [], 0
            parts.append(line[:limit])
            continue
        if cur and cur_len + n > limit:
            parts.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += n
    if cur:
        parts.append("\n".join(cur))

    if len(parts) > max_parts:
        dropped = parts[max_parts:]
        parts = parts[:max_parts]
        parts[-1] += "\n\n> 内容过长，后续 %d 段未发布。" % len(dropped)
    return parts
