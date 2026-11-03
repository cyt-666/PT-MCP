# 开发说明

项目使用 Python 3.12 和[官方 MCP Python SDK 2.3.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)。服务只实现 Streamable HTTP。依赖版本范围定义在 `pyproject.toml` 中；仓库未提供锁定文件。

## 安装与测试

```sh
conda create --prefix ./.conda python=3.12 pip -y
conda activate ./.conda
python -m pip install -e '.[dev]'
python -m pytest
```

测试只使用离线 fixture、fake 站点/下载器和本地 ASGI 客户端。测试不得读取用户的真实配置或访问真实 PT 站点、qBittorrent。

## MCP 服务约定

- 固定注册 8 个工具；即使配置中没有站点或下载器，也保持工具目录不变。
- 保留站点原生分类 ID，不把分类转换成 MoviePilot 媒体类型。
- `resource_id`、RSS 游标和提交回执使用 typed structured results 表达。
- 所有异常在服务边界转换为稳定错误码和脱敏消息，不返回原始 URL、请求头或异常文本。
- Bearer 检查包裹整个 MCP ASGI 应用，覆盖所有 HTTP 方法，并透明转发 ASGI lifespan。
- 测试 HTTP 客户端连接 ASGITransport，不通过 TCP，也不设置真实出网 transport。

## 站点适配器

通用 NexusPHP 适配器只覆盖已实现并有 fixture 的页面结构。新增兼容形态时，应增加脱敏 fixture，验证分类、分页、hash 和下载 locator 解析。

MTeam adapter 使用明确配置的 `api_base_url` 和 profile 分类映射。不得猜测未确认的 API endpoint。真实站点验收必须单独授权执行，常规测试不得触达真实站点。

## MoviePilot 来源与许可

项目按 GNU GPL v3 发布。移植或改写 MoviePilot 代码时，必须更新 `NOTICE`，记录来源仓库、commit、源文件路径和本仓对应文件。纯算法行为应通过本地 fixture 测试锁定。
