# MCP 工具说明

服务只通过 Streamable HTTP 提供 MCP 工具。所有工具调用都使用相同的服务端配置与调用方身份，不向模型暴露站点凭据或下载器凭据。

## 工具目录

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `list_sites` | 无 | 列出已配置站点及其可用能力。 |
| `list_categories` | `site_id` | 返回站点原生分类 ID 和显示名称。 |
| `search_torrents` | `site_id`、`keyword`、`category_ids`、`page` | 单站点搜索。分类 ID 原样传给该站点适配器，页码从 1 开始。 |
| `read_rss` | `site_id`、`cursor`、`limit` | 创建或继续一个独立 RSS 快照；返回的游标用于续读。 |
| `list_downloaders` | 无 | 列出服务端配置的下载器名称和能力。 |
| `add_download` | `resource_id`、`request_id`、`downloader_id`、`save_profile_id`、`paused` | 添加已搜索到的资源。默认立即开始。 |
| `get_submission` | `request_id` | 查询提交状态，包括已接受、已拒绝、重复任务和未知结果。 |
| `list_downloads` | `downloader_id`、`hashes` | 按下载器和种子 hash 核对任务状态。 |

工具的精确 JSON Schema 由服务实现生成。参数缺失、类型错误或未知字段会被拒绝；不要依赖本表代替 `tools/list` 返回的 Schema。

## 资源引用与站点分类

搜索结果中的 `resource_id` 是服务端生成的引用，不是种子 URL。添加任务只接受该引用和服务端已配置的下载器及保存配置。引用绑定站点配置版本，并在过期后拒绝使用。

分类 ID 属于站点自己的命名空间。调用前先读 `list_categories`，搜索时将所选 ID 原样传回。不要把分类映射成电影、电视剧或音乐。

结果中的促销倍率、做种要求和下载数等字段可能为 `null`。`null` 表示站点未提供或当前解析器无法确认，不能当成免费、无 H&R 或零下载。

## RSS 游标

`read_rss` 不传游标时创建独立快照，并返回不透明游标。该游标只用于继续同一站点的快照，不应解析、修改或跨站复用。服务不会因为一次读取就推进共享站点位置。

RSS 项可能没有站点 GUID。适配器使用稳定的站点原生标识，缺失时使用可用字段构造去重键；无法可靠构造时会保留不确定状态，避免把不同条目合并。

## 下载提交与重试

每次用户发起的新下载意图都应生成稳定且唯一的 `request_id`。同一意图重试时沿用原 ID；同一 ID 不能用于其他资源、下载器或保存配置。

提交成功后，重复调用返回原提交回执，不应重复添加。若返回 `unknown`，表示服务无法确认 qBittorrent 是否已接受任务。先用 `get_submission` 查询；必要时使用 `list_downloads` 按 hash 核对。核验前不要生成新 ID 再次提交。

重复 hash 已存在时，服务返回现有任务状态，不隐式启动、解除暂停或修改已有任务。`paused=false` 只控制新任务的初始状态。
