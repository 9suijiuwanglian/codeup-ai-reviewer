"""配置：全部来自环境变量 / .env，不读任何本地私密文件。"""
import os
import re

# 模型接口的硬上限（实测报错原文：max_tokens 合法范围 [1, 393216]）
MAX_OUTPUT_TOKENS = 393216

DEFAULT_CROSS_PATTERNS = (
    r"(?i)(filter|interceptor|security|auth|transaction|config|common|aspect)"
)


def env(name, default=None):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def env_bool(name, default=False):
    v = env(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def load_dotenv(path):
    """极简 .env 解析：KEY=VALUE，# 开头为注释。不覆盖已存在的环境变量。"""
    if not path or not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def parse_repos(raw, single_id=None, single_url=None):
    """CODEUP_REPOS 形如 id=gitUrl,id=gitUrl → [{id, gitUrl}]"""
    repos = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            rid, _, url = item.partition("=")
            repos.append({"id": rid.strip(), "gitUrl": url.strip()})
        else:
            repos.append({"id": item, "gitUrl": ""})
    if not repos and single_id and single_url:
        repos.append({"id": single_id, "gitUrl": single_url})
    return repos


def repo_label(repo):
    """日志里只用仓库名，不打印完整路径和相关 ID。"""
    url = repo.get("gitUrl") or ""
    name = url.rstrip("/").split("/")[-1] if url else ""
    if name.endswith(".git"):
        name = name[:-4]
    return name or ("repo#%s" % repo.get("id"))


class Config(object):
    def __init__(self, dotenv=True):
        if dotenv:
            load_dotenv(env("MRR_ENV_FILE", ".env"))

        # ---- 云效 ----
        self.token = env("CODEUP_TOKEN")
        self.org_id = env("CODEUP_ORG_ID")
        self.domain = (env("CODEUP_DOMAIN", "https://openapi-rdc.aliyuncs.com") or "").rstrip("/")
        self.repos = parse_repos(env("CODEUP_REPOS"), env("CODEUP_REPO"), env("GIT_URL"))
        self.git_user = env("CODEUP_GIT_USER") or "oauth2"

        # ---- 模型 ----
        self.llm_key = env("LLM_API_KEY") or env("DEEPSEEK_API_KEY")
        self.llm_base = (env("LLM_BASE_URL", "https://api.deepseek.com") or "").rstrip("/")
        self.llm_model = env("LLM_MODEL", "deepseek-chat")
        self.llm_max_tokens = int(env("LLM_MAX_TOKENS", "32000"))
        self.llm_max_tokens = max(0, min(self.llm_max_tokens, MAX_OUTPUT_TOKENS))
        self.llm_temperature = float(env("LLM_TEMPERATURE", "0.2"))

        # ---- 分批 ----
        # 注意：这个值决定"分几批"，不决定"能看多少"。调小不会更安全，
        # 只会把文件切碎、让模型看到半个文件、更容易误判。
        self.max_diff_chars = int(env("MAX_DIFF_CHARS", "400000"))
        self.batch_review = env_bool("BATCH_REVIEW", True)
        self.max_batches = int(env("MAX_BATCHES", "8"))
        self.max_split_depth = int(env("MAX_SPLIT_DEPTH", "3"))

        # ---- 全局通道 ----
        self.global_review = env_bool("GLOBAL_REVIEW", True)
        self.cross_max_files = int(env("CROSS_MAX_FILES", "20"))
        self.cross_max_chars = int(env("CROSS_MAX_CHARS", "120000"))
        self.cross_patterns = env("CROSS_PATTERNS") or DEFAULT_CROSS_PATTERNS
        # 故意只读源码，不读 yml/properties：配置里常有口令和密钥
        self.cross_exts = tuple(
            "." + e.strip().lstrip(".")
            for e in (env("CROSS_EXTS", "java,kt") or "java,kt").split(",")
            if e.strip()
        )

        # ---- 输出 ----
        self.comment_max_chars = int(env("COMMENT_MAX_CHARS", "60000"))  # 云效硬上限 65535
        self.post_inline = env_bool("POST_INLINE", False)
        self.inline_limit = int(env("INLINE_LIMIT", "5"))

        # ---- 提交状态回写 ----
        # 把评审结论写成云效「自动化检查」里的一个状态（就是那个绿勾）。
        # 不需要流水线 / Runner / CI 插件，只是一个外部 HTTP 客户端在调 REST 接口。
        # 代价：云效不校验真实性，这个勾可不可信完全由我们自己保证。
        self.commit_status = env_bool("COMMIT_STATUS", True)
        self.check_context = env("CHECK_CONTEXT", "ai-bug-scan")

        # ---- 运行时 ----
        self.repo_dir = env("REPO_DIR", "./work/repo")
        self.state_file = env("STATE_FILE", "./work/state.json")
        self.dry_run = env_bool("DRY_RUN", False)
        self.webhook_token = env("WEBHOOK_TOKEN")
        self.timeout = int(env("HTTP_TIMEOUT", "60"))
        self.ignore_paths = [
            p.strip()
            for p in (env("IGNORE_PATH_PATTERNS", "") or "").split(",")
            if p.strip()
        ]

    def require(self):
        """启动时快速失败，别让配置问题拖到第一次评审才暴露。"""
        missing = []
        if not self.token:
            missing.append("CODEUP_TOKEN")
        if not self.org_id:
            missing.append("CODEUP_ORG_ID")
        if not self.repos:
            missing.append("CODEUP_REPOS")
        if not self.llm_key:
            missing.append("LLM_API_KEY")
        if missing:
            raise SystemExit("缺少必填配置: %s" % ", ".join(missing))
