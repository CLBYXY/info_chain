# EventLens · 投资事件证据链

把散落在公告、董秘回复里的信息整理成一条按时间发展的事件链：最早从哪来、后来怎么变、哪里有冲突、现在处于什么状态、和关注的股票有什么关系。用户问过的话题有新进展时，会主动提醒。

产品设计见 [docs/产品设计.md](docs/产品设计.md)。本文件说明当前实现和运行方式；提交用的完整 README（目标用户、AI 角色、数据使用、边界）待补全。

测试覆盖、网页异常与合规测试步骤、实际测试结果及已知边界见 [测试说明](docs/测试说明.md)。

## 运行

## Render 部署

在 Render 新建 Web Service，选择 GitHub 仓库 `CLBYXY/info_chain`；也可通过 Blueprint 导入根目录的 `render.yaml`。

- Runtime：Python 3，Python 版本 3.12.10。
- Build Command：`pip install -r requirements.txt`。
- Start Command：`uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`。
- Health Check Path：`/api/meta`。
- 在 Environment 中填写 `HITHINK_FINANCE_API_KEY` 和 `DEEPSEEK_API_KEY`，其他模型配置见下表。密钥不要提交到 GitHub。
- 免费实例的 SQLite 会话和缓存位于临时文件系统，重新部署或实例重启后可能丢失；需要保留数据时，使用支持持久磁盘的付费实例，将磁盘挂载到 `/var/data` 并设置 `EVENTLENS_DATA_DIR=/var/data/eventlens`。当前内存任务与缓存要求单 worker、单实例。
- 当前管理端未设置登录保护，适合受控演示；正式面向公众运营前应增加身份验证和调用限额。

部署成功后，使用 Render 控制台显示的 `https://…onrender.com` 地址访问，具体域名以实际分配为准。

## 本地运行

```bash
python -m venv .venv
.venv/Scripts/python -m pip install fastapi==0.115.6 "uvicorn[standard]==0.32.1" anthropic pypdf==5.1.0 httpx==0.28.1 pydantic pytest==8.3.4
```

环境变量（可写在项目根目录的 `.env` 里，该文件已加入 `.gitignore`）：

| 变量 | 必需 | 作用 |
|---|---|---|
| `HITHINK_FINANCE_API_KEY` | 是 | 扶摇（同花顺金融数据）：代码检索、行情、估值 |
| `EVENTLENS_LLM_PROVIDER` | 否 | `deepseek`（默认）或 `anthropic` |
| `DEEPSEEK_API_KEY` | 否 | 启用 DeepSeek，默认模型 `deepseek-flash` |
| `ANTHROPIC_API_KEY` | 否 | 供应商为 `anthropic` 时启用 Claude，默认模型 `claude-opus-5-5` |
| `EVENTLENS_MODEL` | 否 | 覆盖默认模型，如 `deepseek-v4-pro` |
| `EVENTLENS_LLM=off` | 否 | 强制关闭 LLM，所有环节按规则降级运行 |
| `EVENTLENS_REFRESH_MINUTES` | 否 | 定时刷新所有话题的间隔（分钟），默认关闭 |

启动服务：

```bash
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

- 对话页：http://localhost:8000
- 话题后台：http://localhost:8000/admin

运行测试（不访问网络、不调用 LLM）：

```bash
.venv/Scripts/python -m pytest -q tests
```

## 代码结构

| 文件 | 内容 |
|---|---|
| `app/layers/understand.py` | ① 理解层：意图识别、实体链接、时间解析、H 类兜底 |
| `app/layers/fetch.py` | ② 取数层：巨潮公告和互动易；命中筛选、转载去重、记录四种时间、接口异常记为缺口 |
| `app/layers/evidence.py`、`rules.py` | ③ 证据层：声明抽取并回查原文；硬规则圈定可选类型，LLM 判 3 次做软判定；判定事件阶段 |
| `app/layers/event.py` | ④ 事件层：同一事件识别、按披露时间插入、节点归并、四类连线、红黄绿定级、状态单调推导 |
| `app/layers/relate.py` | ⑤ 关联层：关联标的、t0 规则、[t−5, t+5] 累计超额收益 |
| `app/layers/generate.py` | ⑥ 生成层：结论句、引用回查、问题命中检查、违禁词与越界定性拦截 |
| `app/llm.py` | DeepSeek / Claude 调用；输出按 schema 校验，截断时重试，按内容缓存 |
| `app/pipeline.py` | 串联六层，记录过程数据，回调逐层进度 |
| `app/store.py` | SQLite：会话、消息、话题、话题关注者、更新通知 |
| `app/updates.py` | 话题刷新与新旧比对：结论变化、受影响链路、新旧两棵树 |
| `app/main.py` | 接口：会话、后台提问任务、进度查询、更新、后台刷新与回退 |
| `web/` | 对话界面（会话列表、事件链、证据分页、原文片段、红点、弹窗、新旧对比）和话题后台 |

## 当前状态

- **真实事件验证**：用行云科技（300209）“算力卡被卡在海关”传闻，在 LLM 模式（DeepSeek）和规则降级模式下都跑通。事件链为：08-11 传闻 → 冲突 · 被否认 → 08-12 澄清公告 → 作证 → 09-08 进展公告；状态为“公司已否认”。
- **更新推送验证**：把话题回退到 08-20 再刷新，连续 3 次都正确识别出“新增 1 个节点”（09-08 进展公告）。会话出现红点、右下角弹出提醒，新旧对比按三部分展示。
- **耗时**：首次查询 40–120 秒，界面会逐层显示进度；有缓存后同一话题只需几秒。
- **尚未接入**：新闻和研报（iFinD）尚未接入，传闻的最早来源只能追溯到公告里的转述。
- **用户身份**：目前用浏览器本地生成的匿名 ID，还没有接入账号体系。
