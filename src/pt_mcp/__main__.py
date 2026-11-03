"""运行 PT MCP Streamable HTTP 服务。"""

from __future__ import annotations

import argparse

import uvicorn

from pt_mcp.config import load_config
from pt_mcp.server import create_app


def _parse_args() -> argparse.Namespace:
    """解析配置文件及监听地址覆盖项。"""
    parser = argparse.ArgumentParser(description="运行 PT MCP Streamable HTTP 服务")
    parser.add_argument("--config", help="配置文件路径；默认读取 PT_MCP_CONFIG")
    parser.add_argument("--host", help="覆盖配置文件中的监听地址")
    parser.add_argument("--port", type=int, help="覆盖配置文件中的监听端口")
    return parser.parse_args()


def main() -> None:
    """加载配置并运行 Uvicorn。"""
    args = _parse_args()
    config = load_config(config_path=args.config, host=args.host, port=args.port)
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, lifespan="on")


if __name__ == "__main__":
    main()
