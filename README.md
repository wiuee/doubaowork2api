# DoubaoWork2API

将豆包工作模型接入 OpenAI 兼容客户端的自部署网关，提供账号池和 Web 管理面板。

**v1.0.0** · **Python 3.10+** · **Windows / macOS / Linux**

导入自己的账号后，网关直接请求豆包官方上游，不需要桌面客户端或浏览器常驻。
管理员密码与模型 API Key 独立，账号凭证加密保存在本地。

## 功能

- **协议接入**：Chat Completions、Responses、流式与非流式返回。
- **模型能力**：文本、图片输入、可见思考、函数工具桥接和结构化 JSON 输出校验。
- **多轮对话**：完整历史转换、Responses 回放与压缩、账号粘滞和满足条件时的原生会话复用。
- **账号管理**：扫码、浏览器登录、JSON 批量导入导出；支持搜索、错误码筛选、批量启停和定向测试。
- **访问控制**：多 API Key、账号/模型白名单、Key 并发限制和账号并发设置。
- **用量观察**：官方额度查询、请求耗时与首字、可用的官方 Token 数据或明确标记的估算；30 天历史日志和统计。
- **自动化**：北京时间签到计划、手动执行和官方结果回查，明确展示未确认或需人工处理的状态。

面板包含 **总览、账号、用量、自动化、设置、日志** 六个页面；模型清单位于总览底部，支持实时同步官方目录及逐模型测试。

## 快速开始

从 [Releases](https://github.com/niyouqianma/doubaowork2api/releases) 下载源码包，或克隆仓库。

### Windows

```powershell
git clone https://github.com/niyouqianma/doubaowork2api.git
cd doubaowork2api
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
$env:DOUBAO_WORK_ADMIN_PASSWORD = "your-admin-password"
$env:DOUBAO_WORK_API_KEY = "sk-your-api-key"
python -m doubao_work2api
```

### macOS / Linux

```bash
git clone https://github.com/niyouqianma/doubaowork2api.git
cd doubaowork2api
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
export DOUBAO_WORK_ADMIN_PASSWORD='your-admin-password'
export DOUBAO_WORK_API_KEY='sk-your-api-key'
python -m doubao_work2api
```

打开 `http://127.0.0.1:18789/`，使用管理员密码登录，然后导入账号。
客户端填写 Base URL `http://127.0.0.1:18789/v1`，使用 API Key 调用模型。
首次初始化读取环境变量，后续管理密码、Key 与设置保存在本地；Python 入口不会自动加载 `.env`。

也可下载 Release 中的 wheel，执行 `python -m pip install /path/to/doubaowork2api-1.0.0-py3-none-any.whl`，
设置同样的环境变量后启动。安装依赖需要联网，wheel 不捆绑 Python 运行时。

### Docker

预构建镜像已内置 `curl_cffi 0.17.0`，统一标签支持 `linux/amd64`、`linux/arm64`、`windows/amd64` 和 `windows/arm64`：

```bash
docker pull ghcr.io/wiuee/doubaowork2api:v1.0.0
```

也可使用平台标签 `v1.0.0-linux`、`v1.0.0-windows-amd64` 和 `v1.0.0-windows-arm64`。
Windows 容器需要与镜像架构及基础系统兼容的 Windows 宿主机；Linux 与 Windows 镜像不能跨系统运行。

从源码构建并启动：

```bash
cp .env.example .env
# 编辑 .env，设置自己的管理员密码与 API Key。
docker compose up -d --build
```

Compose 默认只映射宿主机回环端口。服务器部署、目录权限、备份和迁移见 [部署文档](docs/DEPLOYMENT.md)。
推送版本标签会触发 [Docker 发布工作流](.github/workflows/docker-publish.yml)，默认发布到 GHCR；配置 `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN` 后也会发布到 Docker Hub。

## 调用示例

```bash
curl http://127.0.0.1:18789/v1/models \
  -H "Authorization: Bearer sk-your-api-key"

curl http://127.0.0.1:18789/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "person-deepseek-v4-1-flash-261017",
    "messages": [{"role": "user", "content": "只回复 OK"}],
    "reasoning_effort": "low"
  }'
```

Responses 客户端使用 `POST /v1/responses`：

```json
{
  "model": "person-deepseek-v4-1-flash-261017",
  "input": "只回复 OK",
  "reasoning": {"effort": "low"},
  "stream": true
}
```

模型 ID 使用官方原始 `model_item_key`，例如 DeepSeek Flash / Pro、豆包 Lite、Kimi、GLM。
实际目录、倍率、窗口和思考档位以账号当前官方配置为准，不将未知模型静默映射到其他模型。

## 数据与边界

- 内部账号文件使用 AES-256-GCM 加密，备份时需同时保留 `accounts/.account-vault.key`。导出的迁移 JSON 含可读凭证。
- 请求日志保留模型、账号/Key 标识、状态与用量等元数据，不保存提示词、回复正文或 Cookie；API Key 只保存哈希。
- 工具调用由文本桥接实现，工具由客户端执行；JSON Schema 由网关提示并校验，不代表上游具备原生约束解码。
- 上游有标准 Token 字段时采用官方值，否则明确标为估算；未提供的缓存、积分数据保持未知。
- 常见采样、缓存提示、服务档位和摘要偏好参数接收但忽略；不会因此启用相应上游能力。详细兼容规则见 [API 文档](docs/API.md)。
- 不提供 Anthropic Messages、生图、生视频、后台 Responses 或内置网页搜索工具。
- 签到是否可用取决于官方活动及账号状态；`710010702` 会提示在官方客户端处理后回查，本版本不承诺所有账号都能纯 HTTP 自动签到。

## 文档

- [API 与配置](docs/API.md)
- [部署、账号导入与备份](docs/DEPLOYMENT.md)
- [版本说明](CHANGELOG.md)
- [来源与许可](THIRD_PARTY_NOTICES.md)

## 致谢

感谢 [yyydsz/doubao2api](https://github.com/yyydsz/doubao2api) 与
[3294798852/doubao-work-gateway](https://github.com/3294798852/doubao-work-gateway)
的协议研究和功能设计。适配范围及第三方许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本项目是独立第三方工具，与豆包官方及上述项目没有隶属或官方合作关系。
