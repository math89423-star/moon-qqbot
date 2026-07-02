"""BGE-M3 Embedding HTTP Service — GPU 推理，容器通过 HTTP 调用。

在 WSL2 宿主机上运行，解决 Docker + NVIDIA Container Toolkit
无法挂载 /usr/lib/wsl/lib/libdxcore.so 的问题 (TRAPS §二十七)。

启动:
  conda activate qqbot
  python tools/embedding_service.py
  或: bash tools/start_embedding_service.sh start

端点:
  GET /health  → {"status":"ok","device":"cuda","dimension":1024,"model_loaded":true}
  POST /encode → {"embeddings":[[...],...],"dim":1024,"device":"cuda"}

配置 (环境变量):
  EMBEDDING_HOST=0.0.0.0
  EMBEDDING_PORT=8880
  EMBEDDING_MODEL_PATH=/mnt/d/BaiduNetdiskDownload/model/hub/...
  EMBEDDING_BATCH_SIZE=16
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("embedding_service")

# ── 配置 (环境变量) ──────────────────────────────────────────

_BGE_M3_PATH = os.environ.get(
    "EMBEDDING_MODEL_PATH",
    "/mnt/d/BaiduNetdiskDownload/model/hub/"
    "models--BAAI--bge-m3/snapshots/"
    "5617a9f61b028005a4858fdac845db406aefb181",
)
_HOST = os.environ.get("EMBEDDING_HOST", "0.0.0.0")
_PORT = int(os.environ.get("EMBEDDING_PORT", "8880"))
_DEFAULT_BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", "16"))

# ── 全局状态 ─────────────────────────────────────────────────

_IDLE_UNLOAD_SEC = int(os.environ.get("EMBEDDING_IDLE_UNLOAD", "300"))  # 空闲卸载秒数, 0=不卸载

_model = None          # SentenceTransformer
_device = "cpu"        # "cuda" | "cpu"
_dim = 0               # embedding 维度
_model_loaded = False
_last_activity = 0.0   # 上次请求时间戳
_unload_task: asyncio.Task | None = None

# ── Pydantic models ──────────────────────────────────────────


class EncodeRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=512)
    normalize_embeddings: bool = True


class EncodeResponse(BaseModel):
    embeddings: list[list[float]]
    dim: int
    device: str


class HealthResponse(BaseModel):
    status: str
    device: str
    dimension: int
    model_loaded: bool


# ── 模型加载 / 卸载 ──────────────────────────────────────────


def _load_model() -> bool:
    """加载 BGE-M3 到 GPU。成功返回 True。"""
    global _model, _device, _dim, _model_loaded
    if _model is not None:
        if _device == "cuda":
            # 已在 GPU 上，直接返回
            _model_loaded = True
            return True
        # 从 CPU 迁回 GPU
        import torch
        _model.to("cuda")
        torch.cuda.empty_cache()
        _device = "cuda"
        _model_loaded = True
        logger.info("BGE-M3 已从 CPU 迁回 GPU")
        return True

    try:
        from sentence_transformers import SentenceTransformer
        import torch

        _device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("加载 BGE-M3: %s (device=%s)", _BGE_M3_PATH, _device)
        _model = SentenceTransformer(_BGE_M3_PATH, device=_device)
        _dim = _model.get_sentence_embedding_dimension()
        _model_loaded = True
        logger.info("BGE-M3 加载完成: device=%s dim=%d", _device, _dim)
        return True
    except ModuleNotFoundError:
        logger.error("sentence-transformers 未安装，服务不可用")
        return False
    except Exception:
        logger.error("BGE-M3 加载失败", exc_info=True)
        return False


def _unload_model() -> None:
    """将模型从 GPU 卸载到 CPU 并释放显存。"""
    global _model, _device, _model_loaded
    if _model is None or _device != "cuda":
        return
    import torch
    _model.to("cpu")
    torch.cuda.empty_cache()
    _device = "cpu"
    _model_loaded = False
    logger.info("BGE-M3 已从 GPU 卸载，显存已释放")


def _touch() -> None:
    """标记活动时间，重置空闲计时器。"""
    global _last_activity
    _last_activity = time.time()


async def _idle_watchdog() -> None:
    """后台协程：空闲超时后卸载模型释放显存。"""
    global _last_activity, _model_loaded
    while True:
        await asyncio.sleep(30)
        if not _model_loaded or _IDLE_UNLOAD_SEC <= 0:
            continue
        idle = time.time() - _last_activity
        if idle >= _IDLE_UNLOAD_SEC:
            _unload_model()


# ── Lifespan ──────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时加载 BGE-M3，启动空闲 watchdog，关闭时清理。"""
    global _unload_task

    _load_model()
    if _IDLE_UNLOAD_SEC > 0:
        _touch()
        _unload_task = asyncio.create_task(_idle_watchdog())
        logger.info("空闲卸载已启用: %ds 无活动后释放 GPU 显存", _IDLE_UNLOAD_SEC)

    yield

    if _unload_task:
        _unload_task.cancel()
    _model = None
    _model_loaded = False
    logger.info("Embedding 服务已关闭")


app = FastAPI(title="BGE-M3 Embedding Service", lifespan=lifespan)


# ── 端点 ─────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health():
    _touch()
    return HealthResponse(
        status="ok" if _model_loaded else "unloaded",
        device=_device,
        dimension=_dim,
        model_loaded=_model_loaded,
    )


@app.post("/encode", response_model=EncodeResponse)
async def encode(req: EncodeRequest):
    global _model_loaded, _device
    _touch()

    if not _model_loaded:
        # 空闲卸载后按需重载到 GPU
        logger.info("模型已卸载，重新加载到 GPU...")
        if not _load_model():
            raise HTTPException(
                status_code=503,
                detail="模型加载失败 — 查看服务日志",
            )

    try:
        embeddings: np.ndarray = _model.encode(
            req.texts,
            normalize_embeddings=req.normalize_embeddings,
            batch_size=_DEFAULT_BATCH_SIZE,
            show_progress_bar=False,
        )
    except Exception:
        logger.error("encode 失败: %d texts", len(req.texts), exc_info=True)
        raise HTTPException(status_code=500, detail="编码失败")

    logger.debug("encode: %d texts → %s (device=%s)", len(req.texts), embeddings.shape, _device)

    return EncodeResponse(
        embeddings=embeddings.tolist(),
        dim=_dim,
        device=_device,
    )


# ── 主入口 ───────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%m-%d %H:%M:%S",
    )

    logger.info("启动 Embedding 服务: %s:%d", _HOST, _PORT)
    uvicorn.run(app, host=_HOST, port=_PORT, log_level="info")
