# 部署说明

本服务默认监听 `127.0.0.1:8000`，通过 `/mcp` 提供 Streamable HTTP。所有请求都需要 `PT_MCP_TOKEN`。

## 本地运行

创建并激活项目专用 Conda Python 3.12 环境后，在仓库根目录执行：

```sh
conda create --prefix ./.conda python=3.12 pip -y
conda activate ./.conda
python -m pip install -e .
cp config.example.yaml config.yaml
export PT_MCP_TOKEN='替换为随机长令牌'
export PT_MCP_CONFIG='./config.yaml'
python -m pt_mcp --host 127.0.0.1 --port 8000
```

服务不会自动探测站点或下载器。空的 `sites`、`downloaders` 配置可用于连接和发现工具；实际调用前需完成站点、下载器和保存配置。

## Docker Compose

从配置示例复制本地配置，并在未纳入版本控制的 `.env` 中设置令牌：

```sh
cp config.example.yaml config.yaml
printf 'PT_MCP_TOKEN=%s\n' '替换为随机长令牌' > .env
docker compose up -d --build
```

示例中的站点和 qBittorrent 凭据环境变量也需要写入 `.env`。Compose 会逐项转发示例配置列出的变量；若在 `config.yaml` 中改用其他环境变量名，也要同步更新 `compose.yaml` 的 `environment` 映射。

服务端口默认只绑定宿主机的 `127.0.0.1:8000`。需要远程访问时，将反向代理配置为 HTTPS，并在服务配置中列出代理使用的 Host 和 Origin。不要直接将服务端口映射到公网。

Docker 数据卷保存 SQLite 文件。qBittorrent 的 `save_profile_id` 指向 qBittorrent 服务器自己的保存路径；PT MCP 不会在容器内创建该目录。

## 配置管理

站点、分类映射、下载器和保存配置都写入 `config.yaml`。`config.example.yaml` 只包含结构示例和假值。

站点凭据可使用配置支持的环境变量引用。部署时应通过环境文件或容器 Secret 注入。不要把 `.env`、实际 `config.yaml`、站点 Cookie、API Key 或 qBittorrent 密码提交到 Git。

## 运行限制

- 默认单进程使用一个 SQLite 文件。
- SQLite 数据卷不可同时由没有协调机制的多个服务副本写入。
- MTeam 的真实站点行为尚未验收；上线前须验证实际 API 权限和 profile 分类映射。
- 站点的验证码、限流、反机器人页面或模板变化可能使搜索和 RSS 读取失败；服务不会伪造空结果。
