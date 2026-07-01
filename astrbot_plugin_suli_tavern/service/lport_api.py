"""L-Port API 客户端 — 轻量 aiohttp 封装。

通过 HTTP 调用本地 L-Port 后端 API，无鉴权（本地部署无认证）。
统一错误处理 + 超时，返回友好的格式化结果供 LLM 消费。

用法:
  from .lport_api import LPortClient
  client = LPortClient()
  status = await client.health_check()
  models = await client.list_models(filter_type="checkpoint")
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# L-Port 默认地址 (本地部署)
DEFAULT_BASE_URL = "http://127.0.0.1:5000"
DEFAULT_TIMEOUT = 10.0


class LPortClient:
    """L-Port API 客户端 — 封装常用查询端点。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str) -> dict:
        """GET 请求，返回 data 字段或完整响应。"""
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        try:
            async with session.get(url, timeout=self._timeout) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("L-Port API %s → %d: %s", path, resp.status, text[:200])
                    return {"error": f"HTTP {resp.status}", "detail": text[:300]}
                data = await resp.json()
                # 统一提取 data 字段
                if isinstance(data, dict) and "data" in data:
                    return {"data": data["data"]}
                # 有些端点用 message/success 包装
                if isinstance(data, dict) and "message" in data:
                    return data
                return {"data": data}
        except aiohttp.ClientError as e:
            logger.error("L-Port API 连接失败 %s: %s", path, e)
            return {"error": "连接失败", "detail": str(e)}
        except Exception as e:
            logger.error("L-Port API 异常 %s: %s", path, e)
            return {"error": "请求异常", "detail": str(e)}

    # ── 公开端点 ──────────────────────────────────────

    async def health_check(self) -> dict:
        """检查 L-Port 系统健康状态。

        Returns:
            {
                "data": {
                    "status": "ok" | "degraded",
                    "database": "connected" | "disconnected",
                    "gpu_count": int,
                    "model_count": int,
                    "node_count": int,
                }
            }
            or {"error": ..., "detail": ...}
        """
        raw = await self._get("/api/health")
        if "error" in raw:
            return raw

        # /api/health 返回 {status, database, ...}
        health_data = raw.get("data", raw)
        if isinstance(health_data, dict) and "status" in health_data:
            # 补充模型和节点计数
            result = {
                "status": health_data.get("status", "unknown"),
                "database": health_data.get("database", "unknown"),
            }
            # 尝试获取更多信息
            try:
                models_raw = await self._get("/api/assets/models")
                if "data" in models_raw and isinstance(models_raw["data"], list):
                    result["model_count"] = len(models_raw["data"])
            except Exception:
                result["model_count"] = "未知"

            try:
                nodes_raw = await self._get("/api/assets/custom_nodes")
                if "data" in nodes_raw and isinstance(nodes_raw["data"], list):
                    result["node_count"] = len(nodes_raw["data"])
            except Exception:
                result["node_count"] = "未知"

            return {"data": result}

        return raw

    async def list_models(self, filter_type: str = "all") -> dict:
        """获取 ComfyUI 可用模型列表。

        Args:
            filter_type: 模型类型 — "checkpoint" | "lora" | "vae" |
                        "controlnet" | "upscale" | "all"

        Returns:
            {
                "data": {
                    "total": int,
                    "filtered": int,
                    "filter_type": str,
                    "models": [{"name": str, "type": str, "path": str}, ...]
                }
            }
        """
        raw = await self._get("/api/assets/models")
        if "error" in raw:
            return raw

        models = raw.get("data", [])
        if not isinstance(models, list):
            return {"data": {"total": 0, "filtered": 0, "filter_type": filter_type, "models": []}}

        # 按类型筛选
        if filter_type and filter_type != "all":
            filtered = []
            for m in models:
                mtype = m.get("type", "").lower()
                if filter_type in mtype or mtype in filter_type:
                    filtered.append(m)
            models = filtered

        # 简化输出 — 只保留关键字段
        simplified = []
        for m in models[:50]:  # 最多 50 条
            simplified.append({
                "name": m.get("name", "?"),
                "type": m.get("type", "unknown"),
                "path": m.get("path", ""),
            })

        return {
            "data": {
                "total": len(models),
                "filtered": len(simplified),
                "filter_type": filter_type,
                "models": simplified,
            }
        }

    async def list_custom_nodes(self) -> dict:
        """获取 ComfyUI 已安装的自定义节点列表。

        Returns:
            {
                "data": {
                    "total": int,
                    "nodes": [{"name": str, "author": str, "version": str}, ...]
                }
            }
        """
        raw = await self._get("/api/assets/custom_nodes")
        if "error" in raw:
            return raw

        nodes = raw.get("data", [])
        if not isinstance(nodes, list):
            return {"data": {"total": 0, "nodes": []}}

        simplified = []
        for n in nodes[:50]:
            simplified.append({
                "name": n.get("name", n.get("title", "?")),
                "author": n.get("author", "unknown"),
                "version": n.get("version", "?"),
            })

        return {
            "data": {
                "total": len(nodes),
                "nodes": simplified,
            }
        }
