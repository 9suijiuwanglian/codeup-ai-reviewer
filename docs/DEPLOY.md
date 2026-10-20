# 部署指南

从零到跑通。**建议顺序不要跳**：本地先 dry-run 跑通，再进集群。

> 文中 `【待截图：xxx】` 是给你补充配图的位置，见文末清单。

---

## 0. 先确认三件事

在动手之前，确认这三条都成立，否则后面全是白费：

| 检查项 | 为什么 |
|---|---|
| 公司允许把源码发给模型 API | **这是硬约束**。不允许就换私有部署模型，或别做 |
| 有一个能访问云效和模型 API 的集群 | 很多内网集群出不去，先把网络打通（或配代理） |
| 团队里有人愿意维护它 | 没人的话，三个月后它就是个僵尸 Pod |

---

## 1. 云效侧准备

### 1.1 个人访问令牌

云效 → 右上角头像 → 个人设置 → 个人访问令牌 → 新建

需要勾的权限：

| 权限组 | 权限 |
|---|---|
| 代码管理 | 仓库：读 |
| 代码管理 | 合并请求：读写 |
| 代码管理 | 提交：读写 |
| 代码管理 | 分支：读 |

`【待截图：个人访问令牌的权限勾选界面】`

> ⚠️ 令牌只显示一次，复制后立刻存好。
> ⚠️ 如果后面用到「提交状态/检查运行」，还要额外勾"提交状态：读写"，否则会返回 `403 Current token has no permission to api.`

### 1.2 组织 ID 和仓库 ID

- **组织 ID**：云效地址栏 `/o/<这一段>/`，或者调 API 查
- **仓库 ID**（repositoryId）：仓库 → 设置 → 基本信息里能看到

> ⚠️ **repositoryId ≠ push 事件里的 project_id**。这是两个不同的号，用错不会报错，只会 404/500。
> 拿不准就用仓库全路径代替（`org/group/repo` 的形式），代码会自动 URL 编码。

### 1.3 配置 Webhook

仓库 → 设置 → Webhook → 新建

| 项 | 填什么 |
|---|---|
| URL | `https://<你的域名>/webhook` |
| 密钥/Token | 与你 `WEBHOOK_TOKEN` 一致（会放在 `X-Webhook-Token` 头里） |
| 触发事件 | **合并请求**（创建/更新/重新打开）+ **Push** |

**必须勾 Push**，原因见 [PITFALLS.md 坑 3](PITFALLS.md)：云效在"MR 已建、之后又推提交"时不一定再发 MR 事件，只能靠 push 来对账。**只勾合并请求，会导致新提交漏评，但 MR 照样能合并。**

`【待截图：Webhook 新建页面，勾选事件的界面】`

`【待截图：Webhook 创建成功后的列表，能看到最近推送记录】`

---

## 2. 本地先跑通（强烈建议，不花钱试错）

```bash
cp .env.example .env
# 编辑 .env：CODEUP_TOKEN / CODEUP_ORG_ID / CODEUP_REPOS / LLM_API_KEY
# DRY_RUN=1 保持开启

# 看有哪些 opened MR
python -m src.cli list

# 自查配置和版本
python -m src.cli check

# 评一个 MR，只打印不发送
python -m src.cli once --mr 1 --dry-run
```

`--dry-run` 会把完整的评论正文打到终端。**这一步能提前发现 80% 的问题**：token 权限不对、diff 生成不出来、模型返回解析不了。

确认评论内容像样了，再往下走。

---

## 3. 构建镜像

```bash
docker build -t <你的仓库>/codeup-ai-reviewer:0.1.0 .
docker push <你的仓库>/codeup-ai-reviewer:0.1.0
```

`【待截图：镜像仓库里能看到这个 tag】`

> 受限网络环境（拉不到 docker.io 基础镜像）的处理，见 [PITFALLS.md 坑 16](PITFALLS.md)。

---

## 4. 部署到 K8s

```bash
cp charts/codeup-ai-reviewer/values.yaml values-local.yaml
# 编辑 values-local.yaml：镜像地址、orgId、repos、域名、密钥

helm lint charts/codeup-ai-reviewer -f values-local.yaml

helm upgrade --install mr-review charts/codeup-ai-reviewer \
  -n mr-review --create-namespace -f values-local.yaml

kubectl rollout status deploy/mr-review-codeup-ai-reviewer -n mr-review --timeout=180s
```

**先用 `options.dryRun: "1"` 部署**，跑一两天看日志里输出的评论内容，确认没问题再改成 `"0"` 真正发评论。这个习惯能避免第一次上线就在群里刷屏。

---

## 5. 逐层验证

**不要跳层**，哪层断了修哪层。这套顺序能把问题定位到一个具体的点上。

| 层 | 命令 | 期望 |
|---|---|---|
| Pod | `kubectl get pod -n mr-review` | `2/2 Running`（或 `1/1`） |
| Pod 内配置 | `kubectl exec -n mr-review deploy/mr-review-codeup-ai-reviewer -- sh -c 'echo "tokens=$LLM_MAX_TOKENS diff=$MAX_DIFF_CHARS"'` | 与 values 一致（**见坑 2**） |
| 入口可达 | `curl -i https://<域名>/webhook` | 非 502（405/401 都算通） |
| 鉴权 | `curl -s -X POST -H 'X-Webhook-Token: <token>' -H 'Content-Type: application/json' -d '{}' https://<域名>/webhook` | `{"ok":true,"msg":"accepted"}` |
| 鉴权反向 | 同上，去掉 Token 头 | `401` |
| token 有效性 | `python -m src.cli check` | 能列出 opened MR 和版本号 |
| 端到端 | 在云效里随便改一行提交到 MR | 日志出现 `收到事件` → `分 N 批评审` → `第 i/N 批` |

**如果日志里连 `收到事件` 都没有**，问题在云效侧（URL 不通、webhook 没启用、事件没勾），不在代码。

`【待截图：云效 webhook 的推送记录/响应结果页面，证明 200 通了】`

`【待截图：MR 里 AI 评论的实际效果（这是最有说服力的一张图，README 里要用）】`

`【待截图：企微群收到的通知消息】`

---

## 6. 上线初期怎么用（很关键）

工具上线失败最常见的原因不是技术，是**一上来就全量开、然后被噪音淹没**。

建议按这个节奏：

| 阶段 | 做法 |
|---|---|
| 第 1 周 | `dryRun=1`，只自己看日志，不发评论 |
| 第 2 周 | 只对**一个仓库**开，`max_issues` 设小（3 条），只推 high |
| 第 3~4 周 | 收集开发的反驳意见，攒"不该说什么"清单（见 `examples/review-rules.md`） |
| 第 2 个月 | 规则稳定了，再扩到更多仓库，允许推 medium |

**判断能不能扩量的标准**：一周内没有人说"这条不对"。有人说过，就先把那条加进排除清单。

---

## 7. 日常运维

```bash
# 看最近日志
kubectl logs -n mr-review deploy/mr-review-codeup-ai-reviewer --tail=100 -f

# 改了配置 → 重启（Secret 类改动必须重启，见坑 13）
helm upgrade --install mr-review charts/codeup-ai-reviewer -n mr-review -f values-local.yaml
kubectl rollout restart deploy/mr-review-codeup-ai-reviewer -n mr-review

# 查版本是否对齐（防止坑 1 复发）
kubectl exec -n mr-review deploy/mr-review-codeup-ai-reviewer -- python -m src.cli check
```

---

## 需要你补的截图清单

按优先级排，前两张最重要（README 里要用，决定别人会不会点进来）：

1. **MR 里的 AI 评论效果**（把业务代码和仓库名打码）— README 首图
2. **企微群通知效果**（群名和成员打码）
3. 云效 Webhook 新建页面（勾选事件的地方）
4. 个人访问令牌的权限勾选界面
5. Webhook 推送记录（证明 200 通）
6. 镜像仓库里的 tag

截图注意：**打码要打掉仓库路径、组织名、真实人名、内网域名、token**。特别是第一张，很容易漏掉右上角的仓库路径。
