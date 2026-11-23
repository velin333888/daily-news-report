# 云端 AI 日报

每天早上在 GitHub 的服务器上跑一次：抓取 AI 新闻源 → 关键词筛选去重 → 模型挑选并写中文解读 → 推送到飞书群机器人。

**完全不需要你的电脑开机。** 手机飞书直接收消息。

## 为什么不用本地定时

WorkBuddy 的自动化调度器是 `LocalAutomationScheduler`，跑在本机进程里。电脑关机时它不存在，任务不会执行（虽然有 24 小时补跑窗口，但你早上不开机就赶不上）。所以这里把整条链路搬到云端。

## 部署（约 10 分钟）

### 1. 建一个 GitHub 仓库

私有仓库即可。把本目录下的内容传到仓库根目录，结构要是这样：

```
你的仓库/
├── .github/workflows/daily.yml
├── daily.py
└── README.md
```

### 2. 拿飞书 Webhook

飞书里建一个只有自己的群 → 群设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook 地址。
安全设置建议选「自定义关键词」填 `AI`，或选「签名校验」并把密钥记下来。

### 3. 配三个 Secrets

仓库 → Settings → Secrets and variables → Actions：

| 类型 | 名称 | 值 |
|---|---|---|
| Secret | `FEISHU_WEBHOOK` | 第 2 步的 Webhook 地址 |
| Secret | `FEISHU_SECRET` | 用签名校验时填，否则留空/不建 |
| Secret | `LLM_API_KEY` | 模型 API Key（见下） |
| Variable | `LLM_BASE_URL` | 例如 `https://api.deepseek.com/v1` |
| Variable | `LLM_MODEL` | 例如 `deepseek-chat` |

模型只要是 OpenAI 兼容接口就行（DeepSeek、通义、Kimi、OpenAI 都可以）。

### 4. 手动跑一次验证

仓库 → Actions → 左侧「AI Daily」→ Run workflow。
看到飞书收到消息即成功。之后每天北京时间 07:00 自动跑。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `FEISHU_WEBHOOK` | 空 | 必填，否则跳过发送 |
| `FEISHU_SECRET` | 空 | 开启签名校验时填 |
| `LLM_API_KEY` | 空 | 不填则降级为「标题速览」模式 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容地址 |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `MAX_ITEMS` | `5` | 每天推送条数 |
| `DEDUP_DAYS` | `10` | 去重记忆保留天数 |
| `DRY_RUN` | 空 | 设 `1` 只打印不发送 |

## 改时间

改 `.github/workflows/daily.yml` 里的 cron。**注意是 UTC 时间**：

| 北京时间 | cron |
|---|---|
| 06:00 | `0 22 * * *` |
| 07:00 | `0 23 * * *` |
| 08:00 | `0 0 * * *` |
| 09:00 | `0 1 * * *` |

## 改新闻源

编辑 `daily.py` 顶部的 `SOURCES`，每项是 `(名称, RSS 地址, 权重)`。
关键词权重在 `WEIGHTS` 里，负数表示降权。

已验证可用的源：IT之家、Google AI Blog、arXiv cs.RO、arXiv cs.SE、MIT Tech Review。
OpenAI / Hacker News / Simon Willison 在国内网络环境不可达，但 GitHub Actions 跑在美国机房，通常能抓。

## 去重怎么工作

每次跑完把当天用到的标题哈希写进 `state/history.json` 并提交回仓库，
保留 `DEDUP_DAYS` 天。所以第二天不会推送重复内容。
这个提交顺带解决了 GitHub 的一个坑：**仓库连续 60 天无活动会自动停用定时任务**。

## 已知限制

- GitHub 定时任务不保证准点，高峰时可能延迟 5-30 分钟
- 公开仓库的 Actions 日志任何人可见，但不包含 Secrets 值；仍建议用私有仓库
- 降级模式（无 LLM Key）只有英文标题和摘要，可读性一般，强烈建议配 Key
