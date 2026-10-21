<div align="center">

# Codeup AI Reviewer

给阿里云云效（Codeup）用的 AI 代码评审机器人

推代码时做一次快速筛查，提合并请求时做完整评审；结果写回云效的评论和「自动化检查」，高危问题另推企业微信。

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![K8s](https://img.shields.io/badge/kubernetes-helm%203.x-326CE5.svg)](https://helm.sh/)

</div>

---

## 这是什么

云效自带代码检查，但不带 AI 评审。市面上的开源方案（PR-Agent 等）基本只对接 GitHub 和 GitLab，换成云效要自己改一遍接口和事件格式。

这个项目就是补这一块：一个跑在你自己集群里的小服务，接云效的 webhook，把评审结果写回云效。

它不需要流水线，也不需要装 Runner。云效仓库设置里加一条 webhook 就够了。

两个触发点，做的事不一样：

| 触发 | 时机 | 跑什么 | 耗时 |
|---|---|---|---|
| **推送（push）** | 每次提交代码 | 增量初筛，只看这次改了什么、有没有明显的错 | 15~30 秒 |
| **合并请求（MR）** | 创建 / 更新 MR | 完整评审，分批看完整个变更，读鉴权等横切文件 | 3~8 分钟 |

第一次接触建议先用 webhook 跑通 MR 评审，push 初筛可以之后再开。

## 它能做什么，不能做什么

**能做**：找出空指针、资源没关、事务漏加、异常被吞、SQL 拼接、接口改了调用方没改这类**有明确对错**的问题，并给出文件行号。

**不能做**：

- **不能当合入门禁。** 同一份代码两次可能给出不同结论，而且它只看了 diff，看不到业务背景。配成阻断合并会经常误伤。
- **判不了跨文件的结构问题。** 分批之后模型看不到全貌，这类问题会漏，或者只能说「需结合上下文确认」。
- **不替代人。** 它挡的是低级错误，不是设计问题。评审的最终判断还是在人这边。

如果你要的是一个能拦住所有问题的东西，这个项目不合适。

## 效果

<!--
截图放好之后，把下面两行的注释去掉（图在 docs/images/，文件名见那里的 README）：

![MR 评论效果](docs/images/mr-comment.png)

云效「自动化检查」里的状态——这个勾是服务自己调接口打的，不是流水线：

![自动化检查](docs/images/commit-status.png)
-->

评论正文长这样：

```markdown
## AI 代码评审 · 发现高危问题，建议修改后再合并

**结论**：本次改动新增了订单查询接口，但未对 stationId 做归属校验，存在越权读取风险。

变更规模：3 个文件，+128 / -14 行

### 1. [高] 越权：stationId 未校验归属
`OrderController.java:57`

直接从请求参数取 stationId 查询订单，没有校验该站点是否属于当前登录用户。
攻击者改一个参数就能读到别的站点的订单。

> 建议：查询前用当前用户可访问的站点列表做一次校验，或改用带归属条件的查询方法。

### 2. [中] 事务边界：两处写操作未在同一事务内
`OrderService.java:112`
...
```

## 环境要求

- Python 3.8 以上（只用标准库，不需要装第三方包）
- 部署的话：Kubernetes 集群 + Helm 3.x
- 一个 OpenAI 兼容格式的模型接口（默认 DeepSeek，换别的模型改 `llm.baseUrl` 和 `llm.model`）
- 云效的个人访问令牌，需要仓库读、合并请求读写、提交读写的权限

## 快速开始

### 1. 拿到云效令牌

云效右上角头像 → 个人设置 → 访问令牌 → 新建。权限勾上和仓库、合并请求、提交相关的那几项。

### 2. 找到仓库 ID

云效仓库页 → 设置 → 基本信息，能看到一串数字的**仓库 ID**。记下来，下一步要用。

### 3. 本地先跑一次，不发任何评论

```bash
git clone https://github.com/9suijiuwanglian/codeup-ai-reviewer.git
cd codeup-ai-reviewer
cp .env.example .env
```

编辑 `.env`，这四个必填：

```bash
CODEUP_TOKEN=<你的访问令牌>
CODEUP_ORG_ID=<组织 ID>
CODEUP_REPOS=1234567=https://codeup.aliyun.com/<组织>/<组>/<仓库>.git
LLM_API_KEY=<模型 key>
```

`CODEUP_REPOS` 的格式是 `仓库ID=仓库地址`，多个仓库用逗号隔开。仓库 ID 就是第 2 步那个数字。

组织 ID 在云效的「组织设置 → 基本信息」里。

先在终端看看它能列出什么：

```bash
python -m src.cli list
```

正常会输出当前打开的合并请求和版本号：

```
#12  version=3  修复下单接口空指针
#9   version=1  新增站点查询
```

然后挑一个试评。`--mr` 填的是合并请求编号，就是界面上 `!12` 里的那个 12（必须是打开状态）：

```bash
python -m src.cli once --mr 12 --dry-run
```

`--dry-run` 只把评论内容打到终端，不会发出去。看着内容像样了再往下走。

### 4. 构建镜像

仓库里没有预构建的镜像，需要自己打一个推到你的镜像仓库：

```bash
docker build -t <你的镜像仓库>/codeup-ai-reviewer:0.1.0 .
docker push <你的镜像仓库>/codeup-ai-reviewer:0.1.0
```

### 5. 部署

复制一份配置覆盖文件，只改这一份，不要改 `values.yaml`：

```bash
cp examples/values-local.yaml values-local.yaml
```

里面有 6 个地方要填：镜像地址、仓库 ID 和地址、组织 ID、令牌、模型 key、模型地址。

```bash
helm upgrade --install mr-review charts/codeup-ai-reviewer \
  -n mr-review --create-namespace \
  -f values-local.yaml
```

装完先别急着上线，**默认是演练模式**（`options.dryRun: "1"`），只把评论打到日志，不往云效发。确认内容对：

```bash
kubectl logs -n mr-review deploy/mr-review-codeup-ai-reviewer -f
```

日志里能看到完整的评论正文。确认没问题，再把它改成 `"0"` 重新 upgrade。

> 这一步最容易忘。忘了的话，Pod 是 Running、日志有输出、webhook 也通了，但云效里一条评论都没有。

### 6. 配置云效 webhook

仓库设置 → Webhook → 新建：

- **URL**：`http://<你的域名或 IP>/webhook`
- **Token**：和你填的 `webhookToken` 一致
- **事件**：勾上「合并请求」的创建和更新。push 事件要不要一起勾，见下面

配完点「测试」，或者在 MR 上随便改点什么触发一次，看服务日志有没有收到。

## 关于 push 事件

云效的 push 事件里没有合并请求编号，所以服务收到 push 之后，做的是**对账**：扫一遍当前打开的合并请求，看谁的版本变了就评谁。

因此 push 事件只影响一件事——「MR 已经建好、之后又推了新提交」这种情况能不能及时发现。

我没做出确定结论：有的云效环境在 MR 建好后再推提交，仍然会补发一次 MR 更新事件；有的不会。**你可以自己验**：勾上 push，推一次代码，看日志里收到的是 push 还是 MR 更新。

两种都勾不会有副作用，重复的会被版本去重挡住。**不想验证就两个都勾。**

## 配置项

环境变量（本地跑）和 Helm values（部署）是一一对应的。

| 环境变量 | values 路径 | 说明 |
|---|---|---|
| `CODEUP_TOKEN` | `codeup.token` | 云效访问令牌 |
| `CODEUP_ORG_ID` | `codeup.orgId` | 组织 ID |
| `CODEUP_REPOS` | `codeup.repos` | `仓库ID=git地址`，多个用逗号分隔 |
| `CODEUP_DOMAIN` | `codeup.domain` | 默认 `https://openapi-rdc.aliyuncs.com`，一般不用改 |
| `LLM_API_KEY` | `llm.apiKey` | 模型 key |
| `LLM_BASE_URL` | `llm.baseUrl` | 默认 DeepSeek |
| `LLM_MODEL` | `llm.model` | 默认 `deepseek-chat` |
| `LLM_MAX_TOKENS` | `llm.maxTokens` | 单次输出上限，给小了 JSON 会断在半路 |
| `MAX_DIFF_CHARS` | `options.maxDiffChars` | 每批 diff 的字符上限，决定分几批 |
| `MAX_BATCHES` | `options.maxBatches` | 最多分几批，超出的不评（评论里会写明） |
| `GLOBAL_REVIEW` | `options.globalReview` | 是否开启横切文件通道（鉴权、事务等） |
| `MAX_ISSUES` | `options.maxIssues` | 评论里最多列几条问题 |
| `COMMIT_STATUS` | `options.commitStatus` | 是否回写「自动化检查」状态 |
| `CHECK_CONTEXT` | `options.checkContext` | 检查项名字，默认 `ai-bug-scan` |
| `WECOM_WEBHOOKS` | `wecom.webhooks` | 企业微信机器人地址，空则不推群 |
| `NOTIFY_LEVELS` | `wecom.notifyLevels` | 哪些级别推群，默认只有 `high` |
| `DRY_RUN` | `options.dryRun` | `1` 只打印不发，`0` 真发 |
| `WEBHOOK_TOKEN` | `webhookToken` | webhook 校验头 |
| `STATE_FILE` | — | 去重状态存哪，默认 `./work/state.json` |

## 命令行

```bash
python -m src.cli serve              # 起 webhook 服务
python -m src.cli list               # 列出打开的 MR 和版本号
python -m src.cli once --mr 12       # 评一个 MR，加 --dry-run 只打印
python -m src.cli poll               # 把所有打开的 MR 扫一遍（cronjob 用这个）
python -m src.cli check              # 检查配置和连通性
```

## 常见问题

**装好了，云效里一条评论都没有**

先看 `options.dryRun` 是不是还是 `"1"`。这是最常见的。其次看日志有没有收到事件，云效 webhook 的推送记录里应该有 200。

**改了 values 不生效，进容器 `echo $LLM_MAX_TOKENS` 还是旧值**

如果用了 `existingSecret`，chart 就不渲染 Secret，而参数只写在 Secret 里就等于没下发。本 chart 已经把调参项都挪到 `env:` 里显式下发了；你自己加新参数时照抄这个写法。

**评论内容是对的，但评的是上一版代码**

版本去重取错了 patchset。云效的 patchset 列表里有一项是 Base（目标分支），它的版本号不随源分支变化。必须只取源分支那一项。这个坑的完整过程在 [docs/PITFALLS.md](docs/PITFALLS.md)。

**评审要跑好几分钟，中途什么都没有**

分批是串行的。现在会先写一个 `pending` 状态到「自动化检查」，界面上能看到它在跑，但评论要等全部跑完才发。看不到进度不等于挂了。

**能换成自己的模型吗**

改 `llm.baseUrl` 和 `llm.model` 就行，只要兼容 OpenAI 的 `/chat/completions`。国内可选 qwen、kimi、智谱等。

## 已知限制

- 跨文件的结构性问题会漏。分批之后模型看不到全貌，prompt 只能缓解。
- 结果不稳定，同一份代码两次可能不同，所以不能当门禁。
- 一次完整评审 3~8 分钟，中途只写状态，不出中间结果。
- 内置的横切规则是按 Java / Spring 写的。换 Go、Python、前端需要重写提示词，见 [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md)。
- 去重状态存在本地 JSON 文件里，所以只能单副本。PVC 丢了会把所有打开的 MR 重评一遍。
- **没有验收集**，说不清误报率。这是目前最大的短板。
- 代码会发给模型供应商。不能外发的代码不要用。

更详细的在 [docs/LIMITATIONS.md](docs/LIMITATIONS.md)。

## 目录结构

```
src/                     全部逻辑，只用 Python 标准库
  config.py              读环境变量，启动即校验
  codeup.py              云效 OpenAPI 封装
  diffutil.py            diff 生成、按文件切批
  llm.py                 模型调用
  reviewer.py            评审流程和提示词
  notify.py              企业微信通知
  server.py              webhook 入口
  cli.py                 命令行
charts/                  Helm chart
docs/
  DEPLOY.md              部署全流程、云效侧逐步配置、验证方法
  PITFALLS.md            18 个坑，按症状 → 根因 → 修法写
  LIMITATIONS.md         缺点和不该用的场景
  CUSTOMIZE.md           怎么改成你们团队的规则
examples/
  values-local.yaml      配置覆盖模板
  review-rules.md        评审规则模板
```

## 背景

这套东西是在一个真实仓库上跑起来的，不是玩具项目。用的过程中撞了 18 个坑，大部分在云效的接口行为和事件格式上，文档里查不到，只能试出来。都记在 [docs/PITFALLS.md](docs/PITFALLS.md) 里了。

代码大部分是让 AI 生成的，我负责跑通、踩坑和改逻辑。所以代码风格一般，但线上确实在跑。

## License

[MIT](LICENSE)
