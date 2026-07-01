"""知识库检索引擎 — BGE-M3 embedding + ChromaDB 向量检索 (关键词回退)。

设计:
  - 加载 knowledge/ 目录下的所有 .md 文件
  - 按 ## 标题拆分为章节（Section），以 section 为检索粒度
  - 主路径: BGE-M3 (1024-dim) embedding → ChromaDB 余弦相似度检索
  - 降级路径: embedding 模型/ChromaDB 不可用时回退关键词分词打分
  - 返回 top-N 节内容（截断到 ~500 字），以 tool message 形式注入 LLM

用法:
  from .knowledge_base import KnowledgeBase

  kb = KnowledgeBase()
  results = kb.search("LoRA 训练 优化器", top_n=3)
  # → ["## LoRA 权重解释\\n...", "## 采样器选择\\n..."]
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 知识库目录 (修复: 从 service/ 往上一级到 suli_tavern/)
_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

# BGE-M3 本地模型路径
_BGE_M3_PATH = (
    "/mnt/d/BaiduNetdiskDownload/model/hub/"
    "models--BAAI--bge-m3/snapshots/"
    "5617a9f61b028005a4858fdac845db406aefb181"
)

# BGE 查询前缀 — 必须加在 query 侧，不加以检索质量显著下降
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

# 章节内容最大字符数（超出截断，加 "..." 标记）
MAX_SECTION_CHARS = 500

# ChromaDB 持久化目录
_CHROMA_DIR = Path("data/chroma_kb")

# ChromaDB collection 名称
_COLLECTION_NAME = "knowledge_sections"


@dataclass
class Section:
    """知识库章节。"""

    title: str               # "## LoRA 权重解释"
    content: str             # 正文（不含标题行）
    source: str              # 来源文件名（如 "comfyui_nodes.md"）
    tokens: set[str] = field(default_factory=set)  # 预分词集合（含标题）

    @property
    def full_text(self) -> str:
        return f"{self.title}\n{self.content}"

    def truncated(self, max_chars: int = MAX_SECTION_CHARS) -> str:
        """返回截断后的章节文本。"""
        text = self.full_text
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n..."


def tokenize(text: str) -> set[str]:
    """中文单字+bigram+trigram + 英文词+相邻bigram 的多粒度分词。

    从 KnowledgeBase._tokenize 提取为模块级函数，供 group_chat.py
    的记忆检索和 knowledge_base 的知识库检索共用。

    中文按单字 + 2-3 字滑动窗口。
    英文按词 + 相邻词 bigram（"clip skip" → token "clip_skip"），
    解决单英文词太泛的问题。
    """
    tokens: set[str] = set()
    text_lower = text.lower()

    # 英文词 (>=2 个字母) + 相邻 bigram
    en_matches = list(re.finditer(r"[a-z][a-z0-9_]+", text_lower))
    en_words = [m.group() for m in en_matches]
    tokens.update(en_words)
    for i in range(len(en_matches) - 1):
        gap = en_matches[i + 1].start() - en_matches[i].end()
        if gap <= 2:
            tokens.add(f"{en_words[i]}_{en_words[i + 1]}")

    # 中文连续字符
    cjk_runs = re.findall(r"[一-鿿㐀-䶿]+", text)
    for run in cjk_runs:
        tokens.update(run)  # 单字
        for i in range(len(run) - 1):
            tokens.add(run[i:i + 2])  # bigram
        for i in range(len(run) - 2):
            tokens.add(run[i:i + 3])  # trigram

    return tokens


class KnowledgeBase:
    """Markdown 知识库检索引擎 — 主路径向量检索，降级关键词回退。

    线程安全：只读操作，无锁竞争。
    """

    def __init__(
        self,
        knowledge_dir: Path | None = None,
        *,
        model_name: str | None = None,
        chroma_dir: Path | None = None,
        use_embedding: bool = True,
    ):
        self._dir = knowledge_dir or _KNOWLEDGE_DIR
        self._model_name = model_name or _BGE_M3_PATH
        self._chroma_dir = chroma_dir or _CHROMA_DIR
        self._use_embedding = use_embedding

        self._sections: list[Section] = []
        self._loaded = False
        self._model = None           # SentenceTransformer (lazy)
        self._client = None          # chromadb.PersistentClient (lazy)
        self._collection = None      # chromadb.Collection (lazy)
        self._embedding_ok = False   # 向量检索是否可用

        self.load()

    # ── 模型 / ChromaDB lazy loading ────────────────────

    def _init_model(self) -> bool:
        """Lazy 加载 SentenceTransformer。成功返回 True，失败设 _use_embedding=False。"""
        if self._model is not None:
            return True
        if not self._use_embedding:
            return False
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("加载 embedding 模型: %s", self._model_name)
            self._model = SentenceTransformer(
                self._model_name, device="cpu",
            )
            return True
        except Exception:
            logger.warning(
                "embedding 模型加载失败，回退关键词检索", exc_info=True,
            )
            self._use_embedding = False
            return False

    def _init_chromadb(self) -> bool:
        """Lazy 初始化 ChromaDB client + collection。成功返回 True。"""
        if self._collection is not None:
            return True
        if not self._use_embedding:
            return False
        try:
            import chromadb
            self._chroma_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=str(self._chroma_dir),
            )
            self._collection = self._client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            return True
        except Exception:
            logger.warning(
                "ChromaDB 初始化失败，回退关键词检索", exc_info=True,
            )
            self._use_embedding = False
            return False

    # ── 加载 ─────────────────────────────────────────────

    def load(self) -> int:
        """加载/重载所有知识文档。返回加载的章节总数。"""
        self._sections.clear()

        if not self._dir.exists():
            logger.warning("知识库目录不存在: %s", self._dir)
            self._loaded = True
            return 0

        count = 0
        for path in sorted(self._dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
                sections = self._parse(text, path.name)
                self._sections.extend(sections)
                count += len(sections)
                logger.debug("知识库加载: %s → %d 章节", path.name, len(sections))
            except Exception:
                logger.error("知识库文件读取失败: %s", path, exc_info=True)

        self._loaded = True
        logger.info(
            "知识库加载完成: %d 章节 (来自 %d 文件)",
            count,
            len({s.source for s in self._sections}),
        )

        # 构建向量索引
        if self._use_embedding and self._sections:
            self._build_vector_index()

        return count

    def reload(self) -> int:
        """热重载知识库（文件更新后调用）。"""
        # 清除 ChromaDB collection 以便重建
        self._collection = None
        if self._client is not None:
            try:
                self._client.delete_collection(_COLLECTION_NAME)
            except Exception:
                pass
        self._embedding_ok = False
        return self.load()

    def _build_vector_index(self) -> None:
        """对所有章节做 embedding + 存入 ChromaDB。

        ChromaDB 持久化检查: 若 collection 中条目数与当前章节数一致,
        说明已持久化, 跳过重建 (load() 被重复调用时避免重复 embedding)。
        仅当条目数为 0 或与章节数不匹配时才重建。

        ⚠️ 先检查 ChromaDB 条目数，匹配则直接标记 _embedding_ok 并返回 —
        不加载 embedding 模型。模型仅在索引缺失/不匹配时加载，或在首次
        _vector_search() 时按需懒加载。
        """
        if not self._init_chromadb():
            return

        # 先检查 ChromaDB 持久化状态 — 不加载模型
        try:
            existing_count = self._collection.count()
            if existing_count == len(self._sections) and existing_count > 0:
                self._embedding_ok = True
                logger.info(
                    "向量索引已持久化 (%d 条), 跳过 embedding + 模型加载",
                    existing_count,
                )
                return
            if existing_count > 0:
                logger.info(
                    "向量索引条目数不匹配 (chroma=%d vs sections=%d), 重建",
                    existing_count, len(self._sections),
                )
        except Exception:
            # count() 可能失败 (旧版 chromadb), 回退到全量重建
            logger.debug("ChromaDB count() 失败, 回退到全量重建", exc_info=True)

        # 索引缺失或不匹配 → 需要 embedding → 此时才加载模型
        if not self._init_model():
            return

        try:
            texts = [sec.full_text for sec in self._sections]
            ids = [
                f"{sec.source}:{i}"
                for i, sec in enumerate(self._sections)
            ]
            metadatas = [
                {
                    "title": sec.title,
                    "source": sec.source,
                    "tokens_json": json.dumps(
                        list(sec.tokens), ensure_ascii=False,
                    ),
                }
                for sec in self._sections
            ]

            logger.info("正在嵌入 %d 个章节...", len(texts))
            embeddings = self._model.encode(
                texts,
                show_progress_bar=False,
                normalize_embeddings=True,
                batch_size=16,
            )

            # 清空旧数据
            try:
                old_ids = self._collection.get()["ids"]
                if old_ids:
                    self._collection.delete(ids=old_ids)
            except Exception:
                pass

            self._collection.add(
                ids=ids,
                documents=[sec.truncated() for sec in self._sections],
                embeddings=embeddings.tolist(),
                metadatas=metadatas,
            )
            self._embedding_ok = True
            logger.info("向量索引构建完成: %d 条 (dim=%d)", len(ids), embeddings.shape[1])
        except Exception:
            logger.warning("向量索引构建失败，回退关键词检索", exc_info=True)
            self._use_embedding = False
            self._embedding_ok = False

    # ── 解析 ─────────────────────────────────────────────

    def _parse(self, text: str, source: str) -> list[Section]:
        """解析 markdown 文本为章节列表。

        双层解析：
        - ## 标题创建父章节（概览型查询）
        - ### 标题创建子章节（精准查询），标题含父路径
        父章节和子章节都作为独立检索单元，确保「过曝」「雪花」等
        具体问题能直接命中对应子章节，不被大章节截断遮蔽。
        """
        sections: list[Section] = []
        # 按 ## 分割（保留标题行）
        parts = re.split(r"\n(?=## )", text)

        for part in parts:
            # 跳过纯一级标题或无标题内容
            if not part.startswith("## "):
                continue

            # 提取父标题（第一行）
            lines = part.split("\n", 1)
            title_line = lines[0].strip()  # "## LoRA 权重解释"
            body = lines[1].strip() if len(lines) > 1 else ""

            if not body:
                continue  # 跳过空章节

            # 父标题文本（去掉 ## 前缀用于分词）
            title_text = title_line[3:].strip()

            # ── 添加父章节 ──
            tokens = tokenize(title_text + " " + body)
            title_tokens = tokenize(title_text)

            sections.append(Section(
                title=title_line,
                content=body,
                source=source,
                tokens=tokens,
            ))
            sections[-1].title_tokens = title_tokens  # type: ignore[attr-defined]

            # ── 解析 ### 子章节 ──
            sub_parts = re.split(r"\n(?=### )", body)

            for sub_part in sub_parts:
                if not sub_part.startswith("### "):
                    continue

                sub_lines = sub_part.split("\n", 1)
                sub_title_line = sub_lines[0].strip()  # "### 画面发灰/色彩暗淡/发白"
                sub_body = sub_lines[1].strip() if len(sub_lines) > 1 else ""

                if not sub_body:
                    continue

                sub_title_text = sub_title_line[4:].strip()
                # 复合标题：父 > 子，便于检索结果中识别上下文
                compound_title = f"{title_line}  ›  {sub_title_line}"

                # 子章节 token 包含：子标题 + 父标题 + 子正文
                # 这样「出图异常」能命中所有子章节，「过曝」精准命中对应子章节
                sub_tokens = tokenize(
                    sub_title_text + " " + title_text + " " + sub_body
                )
                sub_title_tokens = tokenize(sub_title_text + " " + title_text)

                sections.append(Section(
                    title=compound_title,
                    content=sub_body,
                    source=source,
                    tokens=sub_tokens,
                ))
                sections[-1].title_tokens = sub_title_tokens  # type: ignore[attr-defined]

        return sections

    # ── 检索 ─────────────────────────────────────────────

    def search(self, query: str, top_n: int = 3) -> list[str]:
        """按查询字符串检索，返回最相关的章节文本。

        主路径: BGE-M3 embedding → ChromaDB 余弦相似度
        降级路径: 关键词分词 + 标题/正文加权打分

        Args:
            query: 中文/英文查询字符串（LLM 生成的搜索词）
            top_n: 返回 top-N 个最相关章节

        Returns:
            截断后的章节文本列表（按相关性降序），可能为空
        """
        if not self._loaded or not self._sections:
            return []

        # 主路径: 向量检索
        if self._embedding_ok and self._collection is not None:
            try:
                return self._vector_search(query, top_n)
            except Exception:
                logger.warning("向量检索失败，回退关键词检索", exc_info=True)
                # 单次失败不全局降级 — 保留 _embedding_ok 供下次重试

        # 降级路径: 关键词检索
        return self._keyword_search(query, top_n)

    def _vector_search(self, query: str, top_n: int) -> list[str]:
        """ChromaDB 向量检索。首次调用时按需加载 embedding 模型。"""
        if self._model is None:
            if not self._init_model():
                return self._keyword_search(query, top_n)

        # BGE 需要 query 侧加 instruction prefix
        query_text = BGE_QUERY_INSTRUCTION + query

        query_embedding = self._model.encode(
            [query_text],
            normalize_embeddings=True,
        )

        results = self._collection.query(
            query_embeddings=query_embedding.tolist(),
            n_results=min(top_n, len(self._sections)),
        )

        documents: list[str] = results.get("documents", [[]])[0]
        distances: list[float] = results.get("distances", [[]])[0]

        if documents:
            # 余弦距离转相似度用于日志
            sims = [round(1.0 - d, 3) for d in distances] if distances else []
            logger.info(
                "知识库向量检索: query=%r → %d 结果 sims=%s",
                query, len(documents), sims,
            )

        return documents

    def _keyword_search(self, query: str, top_n: int) -> list[str]:
        """关键词分词 + 加权打分检索（降级路径）。"""
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        # 对每个章节打分
        scored: list[tuple[float, Section]] = []
        for sec in self._sections:
            score = self._score(query_tokens, sec)
            if score > 0:
                scored.append((score, sec))

        # 按得分降序
        scored.sort(key=lambda x: x[0], reverse=True)

        # 取 top-N，去重（同一标题只取最高分）
        seen_titles: set[str] = set()
        results: list[str] = []
        for score, sec in scored:
            if sec.title in seen_titles:
                continue
            seen_titles.add(sec.title)
            results.append(sec.truncated())
            if len(results) >= top_n:
                break

        if results:
            logger.info(
                "知识库关键词检索: query=%r → %d/%d 结果 top_score=%.1f",
                query, len(results), len(scored),
                scored[0][0] if scored else 0,
            )

        return results

    # ── 打分 (关键词降级路径) ──────────────────────────

    @staticmethod
    def _score(query_tokens: set[str], section: Section) -> float:
        """计算查询与章节的相关性得分。

        标题命中: ×5 权重
        正文命中: ×1 权重
        """
        score = 0.0
        title_tokens = getattr(section, "title_tokens", set())

        for qt in query_tokens:
            if qt in section.tokens:
                if qt in title_tokens:
                    score += 5.0  # 标题命中高权重 — 对抗大章节的正文词量优势
                else:
                    score += 1.0

        return score

    # ── 状态 ─────────────────────────────────────────────

    @property
    def section_count(self) -> int:
        return len(self._sections)

    @property
    def source_files(self) -> list[str]:
        return sorted({s.source for s in self._sections})

    @property
    def using_embedding(self) -> bool:
        """是否正在使用向量检索（用于调试/监控）。"""
        return self._embedding_ok

    def summary(self) -> str:
        """返回知识库摘要（供 LLM 了解可用的知识范围）。"""
        if not self._sections:
            return "知识库为空"

        by_source: dict[str, list[str]] = {}
        for sec in self._sections:
            by_source.setdefault(sec.source, []).append(sec.title)

        mode = "向量检索" if self._embedding_ok else "关键词检索"
        lines = [
            f"知识库 ({mode}): {len(self._sections)} 章节, {len(by_source)} 文件",
        ]
        for source, titles in by_source.items():
            lines.append(f"  [{source}]")
            for t in titles[:8]:
                lines.append(f"    {t}")
            if len(titles) > 8:
                lines.append(f"    ... 还有 {len(titles) - 8} 个章节")
        return "\n".join(lines)


# ── 全局单例 ──────────────────────────────────────────────

_global_kb: KnowledgeBase | None = None


def get_knowledge_base() -> KnowledgeBase:
    """获取全局知识库单例。"""
    global _global_kb
    if _global_kb is None:
        _global_kb = KnowledgeBase()
    return _global_kb
