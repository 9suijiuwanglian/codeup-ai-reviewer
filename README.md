# codeup-ai-reviewer

云效 Codeup 的 AI 代码评审。推代码或提 MR 的时候触发一次，结果写回云效的评论里，有高危的再推到企业微信群。

代码大部分是让 AI 生成的，我在真实仓库上把它调通，顺手把踩的坑记下来。清单在 [docs/PITFALLS.md](docs/PITFALLS.md)，一共 18 条，其中 3 条属于"不报错、看着正常、但结果是错的"。

市面上的 AI 评审方案基本都是 GitHub / GitLab 的，云效这边我没找到能直接拿来用的，就自己凑了一个。

## 它怎么跑的

```
云效 push / MR 事件
  → webhook 服务（集群里的一个 Pod）
  → 拉 diff，按文件切批，调模型
  → 写回云效：提交评论 + 自动化检查的状态
  → 企业微信群（只有高危才推）
```

没有用流水线，也没装 Runner。云效「自动化检查」里那个 `ai-bug-scan` 的勾，是服务自己调接口打上去的：

```python
POST /oapi/v1/codeup/organizations/{org}/repositories/{repo}/commits/{sha}/statuses
```

好处是模型 key 不用配到云效上，也不用管云效的构建资源。代价是这个勾是自己给自己发的，云效不校验我们到底跑没跑，所以**别把它配成阻断合并**。

## 几个具体做法

- **diff 和鉴权分开看。** 越权、事务边界、接口契约这些靠 diff 判不出来，得去读鉴权 filter、拦截器、事务注解的全文。所以拆了两条通道：一条按文件切 diff，一条单独读横切文件。两边要查的清单不能重叠，否则同一个问题报两遍。
- **让模型知道自己没看全。** 分批之后它看不到别的文件，但不会承认，会很肯定地说"没有权限问题"。所以在 prompt 里写清楚这批包含哪几个文件、缺了哪些。这个改动比换模型管用。
- **评论要限条数。** 一个 MR 刷四十条，开发第二次就不看了。按严重程度分级，只推前几条，其余归档到评论里。
- **push 事件只能对账。** 云效的 push 事件里没有 MR 编号，所以只能扫一遍打开的 MR，谁的版本变了评谁。

## 上手

本地先跑一次，不碰集群：

```bash
git clone https://github.com/9suijiuwanglian/codeup-ai-reviewer.git
cd codeup-ai-reviewer
pip install -r requirements.txt
cp .env.example .env
```

`.env` 里填 `CODEUP_TOKEN`、`CODEUP_ORG_ID`、`CODEUP_REPOS`、`LLM_API_KEY` 这四个，其余有默认值。

```bash
python -m src.cli once --mr 1 --dry-run
```

`--dry-run` 只把评论内容打到终端，不会发出去。看着没问题了再去掉。

装到集群：

```bash
helm upgrade --install mr-review charts/codeup-ai-reviewer -n mr-review --create-namespace \
  -f values-local.yaml
```

参数和部署细节在 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 云效那边要做的

1. 建个个人访问令牌，勾代码管理相关权限（仓库读、合并请求读写、提交读写）
2. 仓库设置里加 webhook，地址填你服务的域名 + `/webhook`
3. **事件一定要勾上 push**。只勾合并请求的话，MR 建好之后的新提交会全部漏评，而且照样能合并

## 踩过的几个坑

当时找了一圈没人写过这些，记在下面。详细的都在 [docs/PITFALLS.md](docs/PITFALLS.md)。

### 它评了一周的旧代码

评论照常发，群里通知也正常，内容却是上一版的。

原因是去重取了 `patchsets` 列表的最后一项，而这一项是 **Base**（`relatedMergeItemType=MERGE_TARGET`），不是最新版本。目标分支不动，Base 就不变，所以从第二个版本开始，每次都被判定"已经评过了"，直接跳过。云效合并时用的是源分支的当前内容，所以那段时间"AI 说没问题"和"真的合进去的代码"完全不是一回事。

修法是只取 `MERGE_SOURCE`，按 `versionNo` 排序取最后一个。代码在 `src/codeup.py`。

### 改了 values 不生效

`helm upgrade` 成功，Pod 也是新的，进容器里 `echo $LLM_MAX_TOKENS` 还是旧值。

因为 chart 里设了 `existingSecret`，就不渲染 `secret.yaml` 了，而调参项恰好只写在那个文件里。改了 `values.yaml` 等于没改。

现在所有调参项都在 `deployment.yaml` 的 `env:` 里显式下发。以后加新的也照这个来，不然同一个坑会再来一遍。

### "没动静"其实是还在跑

分批是串行的，4 批要七到八分钟，中间不打任何东西出来。我看到群里没消息，以为挂了，重启了三次，每次都从头再来。

所以现在先挂一个 `pending` 状态，至少界面上能看出它在跑。

## 已知问题

- 跨文件的问题会漏。分批之后模型看不全，靠 prompt 只能缓解。
- 同一份代码两次可能给出不同结论，所以不能当合入门禁，只能当提示。
- 一次评审七到八分钟，中间没有进度输出。
- 全局通道的规则是按 Spring 写的，换 Go 或前端要重写。
- 状态存在 `state.json` 里，没上数据库，所以只能单副本。PVC 丢了会重评所有 MR。
- 没有验收集，现在说不清它的误报率。

## 目录

```
src/         全部逻辑，只用 Python 标准库
charts/      Helm chart
docs/        DEPLOY / PITFALLS / LIMITATIONS / CUSTOMIZE
examples/    评审规则的模板
```

## License

MIT
