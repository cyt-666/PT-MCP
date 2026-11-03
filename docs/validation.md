# 验收记录

| 项目 | 结果 | 范围 |
| --- | --- | --- |
| 独立环境 | 通过 | Conda 环境创建成功；`sys.prefix` 为项目 `./.conda`，Python 3.12.16。 |
| 独立依赖 | 通过 | `mcp==2.3.0`、`pytest==9.2.0`、`httpx==0.28.1`、`pydantic==2.14.2`、`starlette==1.10.0`、`qbittorrent-api==2026.6.0`、`pt-mcp==0.1.0`；`pip check` 通过。 |
| 全量测试 | 74 passed | 使用项目 `./.conda` 环境运行全套测试，包含离线 fixture/fake 与 MCP HTTP 安全测试。 |
| MCP SDK 本地验收 | 通过 | 使用 SDK 2.3.0 的 Streamable HTTP client 连接通过 CLI 启动的本地服务；协商协议版本 `2025-11-25`，发现精确 8 个工具，并成功调用 `list_sites`、`list_downloaders`，两者均返回空结果。未配置站点的 `list_categories` 返回安全的 `not_found` 工具错误。 |
| HTTP 安全门禁 | 通过 | 缺失/错误 Bearer 的 GET、POST、DELETE 均返回 401；未列入的 Host 返回 421，未列入的 Origin 返回 403。 |
| 进程收尾 | 通过 | 本地服务绑定 `127.0.0.1`；验收后服务完成 graceful shutdown，Uvicorn 记录 `Application shutdown complete` 和 `Finished server process`。 |
| 真实 PT / qBittorrent | 未执行 | 全程使用空站点、空下载器配置，没有访问真实 PT 站点或 qBittorrent。 |
| wheel 构建与内容 | 通过（此前预检） | 使用已有环境中的 setuptools 构建；wheel 含内置 NexusPHP/MTeam YAML、LICENSE、NOTICE，依赖元数据不含 MoviePilot。 |

本轮本地协议验收使用的配置、状态和服务日志均位于 Git 忽略的 `.acceptance/` 目录，没有将令牌值或真实凭据写入记录。结构化结果见 `.acceptance/run-7cd3qwit/acceptance-summary.json`，对应服务日志见 `.acceptance/run-7cd3qwit/uvicorn.log`；JSON 文件是根据本轮结果整理的结构化摘要，不是原始 stdout。
