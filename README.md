<div align="center">

# Codeup AI Reviewer

云效 Codeup 的 AI 代码评审

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Helm](https://img.shields.io/badge/helm-3.x-326CE5.svg)](https://helm.sh/)

</div>

---

推代码的时候跑一次初筛，提合并请求的时候跑一次完整评审。结果写成云效的评论，同时回写「自动化检查」的状态，有高危的再推到企业微信。

服务跑在自己的集群里，仓库里加一条 webhook 就行。不需要流水线，也不用装 Runner。

云效自带代码检查，但不带 AI 评审。开源方案像 PR-Agent 基本是对着 GitHub 和 GitLab 写的，换到云效要改接口和事件格式。这个项目是这么凑出来的。

代码大部分是让 AI 写的，我负责跑通、踩坑、改逻辑。踩到的 18 个坑记在 [docs/PITFALLS.md](docs/PITFALLS.md)。

两个触发点做的事不一样：

- **push**：只看这次改了什么，一般 15~30 秒
- **合并请求**：分批看完整个变更，还会去读鉴权、事务这类要整个文件才判得出来的地方，3~8 分钟

## 效果

<!-- 截图放好之后把这里放开，文件名见 docs/images/README.md

![MR 评论](docs/images/mr-comment.png)

![自动化检查](docs/images/commit-status.png)

-->

评论正文大概长这样：

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

Python 3.8 以上，只用标准库，不用装第三方包。部署要 K8s 和 Helm 3。模型接口得兼容 OpenAI 格式，默认接 DeepSeek。云效那边要一个个人访问令牌，权限勾上仓库读、合并请求读写、提交读写。

## 用法

### 1. 云效拿令牌

头像 → 个人设置 → 访问令牌 → 新建。

### 2. 找仓库 ID

仓库页 → 设置 → 基本信息，有一串数字的仓库 ID。下一步要用。

### 3. 本地先跑一次

```bash
git clone https://github.com/9suijiuwanglian/codeup-ai-reviewer.git
cd codeup-ai-reviewer
cp .env.example .env
```

`.env` 里填四个：

```bash
CODEUP_TOKEN=<访问令牌>
CODEUP_ORG_ID=<组织 ID>
CODEUP_REPOS=1234567=https://codeup.aliyun.com/<组织>/<组>/<仓库>.git
LLM_API_KEY=<模型 key>
```

`CODEUP_REPOS` 是 `仓库ID=仓库地址`，多个仓库用逗号隔开。组织 ID 在组织设置里。

看它能列出什么：

```bash
python -m src.cli list
```

```
#12  version=3  修复下单接口空指针
#9   version=1  新增站点查询
```

挑一个试评。`--mr` 填合并请求编号，界面上 `!12` 里那个 12，得是打开状态：

```bash
python -m src.cli once --mr 12 --dry-run
```

`--dry-run` 只把评论打到终端，不发出去。

### 4. 构建镜像

仓库里没有预构建镜像，得自己打：

```bash
docker build -t <镜像仓库>/codeup-ai-reviewer:0.1.0 .
docker push <镜像仓库>/codeup-ai-reviewer:0.1.0
```

### 5. 部署

```bash
cp examples/values-local.yaml values-local.yaml
```

里面有 6 个标了 `★` 的地方：镜像地址、仓库 ID 和地址、组织 ID、令牌、模型 key、模型地址。改这一个文件，别改 `values.yaml`。

```bash
helm upgrade --install mr-review charts/codeup-ai-reviewer \
  -n mr-review --create-namespace \
  -f values-local.yaml
```

装完默认是演练模式（`options.dryRun: "1"`），只把评论打到日志，不往云效发：

```bash
kubectl logs -n mr-review deploy/mr-review-codeup-ai-reviewer -f
```

日志里能看到完整正文。确认对了改成 `"0"` 再 upgrade 一次。不改的话，Pod 是 Running、日志有输出、webhook 也通，但云效里一条评论都没有。

### 6. 配云效 webhook

仓库设置 → Webhook → 新建。URL 填 `http://<域名或 IP>/webhook`，Token 和 `webhookToken` 一致，事件勾上「合并请求」的创建和更新。

配完点测试，或者在 MR 上随便改点什么，看日志有没有收到。

### push 事件要不要勾

云效的 push 事件里没有合并请求编号，所以服务收到 push 之后是反过来查：扫一遍当前打开的合并请求，谁的版本变了评谁。

它只影响一件事，「MR 已经建好、之后又推了新提交」能不能及时发现。这个我没验出确定结论，有的环境会补发 MR 更新事件，有的不会。想自己验就勾上 push 推一次，看日志收到的是 push 还是 MR 更新。不想验就两个都勾，重复的会被版本去重挡掉。

## 配置

环境变量和 Helm values 一一对应。

| 环境变量 | values | 说明 |
|---|---|---|
| `CODEUP_TOKEN` | `codeup.token` | 云效访问令牌 |
| `CODEUP_ORG_ID` | `codeup.orgId` | 组织 ID |
| `CODEUP_REPOS` | `codeup.repos` | `仓库ID=git地址`，逗号分隔 |
| `CODEUP_DOMAIN` | `codeup.domain` | 默认 `https://openapi-rdc.aliyuncs.com` |
| `LLM_API_KEY` | `llm.apiKey` | 模型 key |
| `LLM_BASE_URL` | `llm.baseUrl` | 默认 DeepSeek |
| `LLM_MODEL` | `llm.model` | 默认 `deepseek-chat` |
| `LLM_MAX_TOKENS` | `llm.maxTokens` | 单次输出上限，给小了 JSON 会断在半路 |
| `MAX_DIFF_CHARS` | `options.maxDiffChars` | 每批 diff 的字符上限，决定分几批 |
| `MAX_BATCHES` | `options.maxBatches` | 最多分几批，超出的不评，评论里会写 |
| `GLOBAL_REVIEW` | `options.globalReview` | 是否读鉴权、事务这类横切文件 |
| `MAX_ISSUES` | `options.maxIssues` | 评论里最多列几条 |
| `COMMIT_STATUS` | `options.commitStatus` | 是否回写「自动化检查」状态 |
| `CHECK_CONTEXT` | `options.checkContext` | 检查项名字，默认 `ai-bug-scan` |
| `WECOM_WEBHOOKS` | `wecom.webhooks` | 企微机器人地址，空则不推群 |
| `NOTIFY_LEVELS` | `wecom.notifyLevels` | 哪些级别推群，默认只有 `high` |
| `DRY_RUN` | `options.dryRun` | `1` 只打印，`0` 真发 |
| `WEBHOOK_TOKEN` | `webhookToken` | webhook 校验头 |
| `STATE_FILE` | — | 去重状态存哪，默认 `./work/state.json` |

## 命令行

```bash
python -m src.cli serve              # 起 webhook 服务
python -m src.cli list               # 列出打开的 MR 和版本号
python -m src.cli once --mr 12       # 评一个，加 --dry-run 只打印
python -m src.cli poll               # 扫一遍打开的 MR，cronjob 用这个
python -m src.cli check              # 检查配置和连通性
```

## 装完没反应

先看 `dryRun` 是不是还是 `1`。其次看日志有没有收到事件，云效 webhook 的推送记录里应该有 200。

改了 values 不生效、进容器 `echo $LLM_MAX_TOKENS` 还是旧值，是因为用了 `existingSecret` 之后 chart 不渲染 Secret，参数只写在 Secret 里等于没下发。这个 chart 已经把调参项都挪到 `env:` 里，自己加参数时照抄那个写法。

评论发出来了但内容是上一版代码，是版本去重取错了 patchset。云效的 patchset 列表里有一项是 Base（目标分支），它不随源分支变。只取源分支那一项，过程在 [docs/PITFALLS.md](docs/PITFALLS.md)。

评审跑好几分钟、中途什么都没有，是因为分批串行。现在会先写一个 `pending` 状态到「自动化检查」，界面能看到在跑，但评论要等全跑完才发。

换模型改 `llm.baseUrl` 和 `llm.model`，兼容 OpenAI 的 `/chat/completions` 就行。

## 已知问题

- 跨文件的结构性问题会漏。分批之后模型看不到全貌。
- 同一份代码两次可能给出不同结论，所以别配成阻断合并。
- 完整评审 3~8 分钟，中途只写状态，不出中间结果。
- 内置的规则是按 Java/Spring 写的，换 Go、Python、前端要重写提示词，见 [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md)。
- 去重状态存在本地 JSON，所以只能单副本。PVC 丢了会把所有打开的 MR 重评一遍。
- 没有验收集，说不清误报率。这是最大的短板。
- 代码会发给模型供应商，不能外发的别用。

它挡的是空指针、资源没关、事务漏加、异常被吞、SQL 拼接、接口改了调用方没改这类有明确对错的问题。设计上的问题判不了，最终还是要人看。

更多在 [docs/LIMITATIONS.md](docs/LIMITATIONS.md)。

## 目录

```
src/                     全部逻辑，只用标准库
  config.py              读环境变量，启动即校验
  codeup.py              云效 OpenAPI 封装
  diffutil.py            diff 生成、按文件切批
  llm.py                 模型调用
  reviewer.py            流程和提示词
  notify.py              企业微信通知
  server.py              webhook 入口
  cli.py                 命令行
charts/                  Helm chart
docs/
  DEPLOY.md              部署全流程、云效侧配置、验证方法
  PITFALLS.md            18 个坑
  LIMITATIONS.md         缺点和不该用的场景
  CUSTOMIZE.md           怎么改成你们团队的规则
examples/
  values-local.yaml      配置覆盖模板
  review-rules.md        评审规则模板
```

## License

MIT
