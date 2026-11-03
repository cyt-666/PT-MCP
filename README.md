# PT MCP

PT MCP 是独立运行的 BitTorrent 站点搜索与下载服务。它通过 Streamable HTTP 为 Agent 提供站点、分类、搜索、RSS 和 qBittorrent 操作。

服务使用站点原生分类，不要求电影、电视剧或音乐元数据。Agent 只提交服务生成的 `resource_id`，站点凭据和下载器凭据始终留在服务端。

## 功能

- 使用站点原生分类搜索资源，并按单站点分页。
- 按需读取 RSS；游标续读独立快照，不推进共享站点位置。
- 通过 qBittorrent 添加任务，默认立即开始。
- 通过提交记录和 hash 查询核对重复任务及未知提交结果。
- 仅开放 8 个固定 MCP 工具，不提供任意 URL 或命令执行工具。

## 环境要求

- Python 3.12。
- Conda。
- qBittorrent Web API。
- 可访问的 NexusPHP 站点或已配置的 MTeam API。

Python MCP SDK 固定为 2.3.0，版本来源见[官方发行说明](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)。项目依赖范围定义在 `pyproject.toml` 中。

## 本地启动

```sh
conda create --prefix ./.conda python=3.12 pip -y
conda activate ./.conda
python -m pip install -e '.[dev]'
cp config.example.yaml config.yaml
export PT_MCP_TOKEN='替换为随机长令牌'
export PT_MCP_CONFIG='./config.yaml'
python -m pt_mcp --host 127.0.0.1 --port 8000
```

MCP 地址为 `http://127.0.0.1:8000/mcp`。服务启动时不要求预先配置站点或下载器，因此空配置也能发现固定的 8 个工具；调用需要配置相应能力。

`PT_MCP_TOKEN` 是所有 HTTP 方法共用的 Bearer 凭据。服务默认只绑定回环地址。远程访问应放在 HTTPS 反向代理之后，并显式配置允许的 Host 和 Origin。

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `list_sites` | 列出已配置站点 |
| `list_categories` | 读取站点原生分类 |
| `search_torrents` | 按站点、关键词和分类搜索；页码从 1 开始 |
| `read_rss` | 读取一个站点的 RSS 快照并返回下一游标 |
| `list_downloaders` | 列出已配置下载器 |
| `add_download` | 按 `resource_id` 添加下载，默认立即开始 |
| `get_submission` | 按 `request_id` 查询提交回执和未知状态 |
| `list_downloads` | 按下载器和 hash 查询任务 |

`search_torrents` 的结果引用可传给 `add_download`。添加任务时，客户端为一次意图提供稳定的 `request_id`。网络超时返回未知状态后，应先调用 `get_submission` 或 `list_downloads` 核对，不能换一个请求 ID 重试。

详细合同见 [工具说明](docs/tools.md)。

## 部署

Docker Compose 示例见 [部署说明](docs/deployment.md)。Secret 通过环境变量引用，不要将令牌、Cookie、API Key 或 qBittorrent 密码写入配置示例或版本控制。

`save_profile_id` 指向已配置的保存配置。实际保存目录由 qBittorrent 管理；PT MCP 不会在本地创建该目录。

## 兼容范围

NexusPHP 适配器按站点配置提供通用页面解析能力，不承诺兼容所有模板或反机器人页面。MTeam 使用显式配置的 `api_base_url` 和站点 profile；仓库不假设站点提供未确认的分类接口。自动化测试使用离线 fixture，不访问真实 PT 站点或 qBittorrent。

MTeam 尚未完成真实站点验收。公开部署前，请先核对目标站点的 API 权限、分类映射和访问限制。

## 开发与验证

```sh
python -m pytest
```

测试使用 fixture、fake transport 和本地 ASGI 客户端，不会访问真实 PT 站点或下载器。开发约定见 [开发说明](docs/development.md)。

## 许可证

本项目采用 GNU GPL v3，许可证全文见 [LICENSE](LICENSE)。复用 MoviePilot 代码的位置和来源见 [NOTICE](NOTICE)。
