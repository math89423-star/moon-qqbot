import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from astrbot.core.utils.astrbot_path import (
        get_astrbot_data_path,
        get_astrbot_plugin_data_path,
    )
except ModuleNotFoundError:
    get_astrbot_data_path = lambda: "data"  # type: ignore[assignment]
    get_astrbot_plugin_data_path = lambda: "data/plugin_data"  # type: ignore[assignment]

# 获取当前插件目录的绝对路径
PLUGIN_DIR = Path(__file__).resolve().parent
CURRENT_DIR = str(PLUGIN_DIR)
DEFAULT_PLUGIN_NAME = "astrbot_plugin_suli_meme"


def resolve_plugin_name(plugin_name: str | None = None) -> str:
    """优先使用运行时插件名，失败时回退到硬编码插件名。"""
    candidate = plugin_name or DEFAULT_PLUGIN_NAME
    return candidate.strip() or DEFAULT_PLUGIN_NAME


def get_legacy_plugin_data_dir() -> Path | None:
    """返回旧版插件数据目录 data/memes_data。"""
    try:
        return (Path(get_astrbot_data_path()) / "memes_data").resolve()
    except Exception:
        return None


def get_plugin_data_dir(plugin_name: str | None = None) -> Path:
    """返回插件数据目录，规范落在 data/plugin_data/{plugin_name}/ 下。"""
    resolved_plugin_name = resolve_plugin_name(plugin_name)
    try:
        plugin_data_root = Path(get_astrbot_plugin_data_path())
        return (plugin_data_root / resolved_plugin_name).resolve()
    except Exception:
        fallback_data_path = (
            PLUGIN_DIR / "data" / "plugin_data" / resolved_plugin_name
        ).resolve()
        logger.warning(
            "获取 AstrBot 数据目录失败，回退到本地路径: %s", fallback_data_path
        )
        return fallback_data_path


def _plugin_data_dir_has_content(plugin_data_dir: Path) -> bool:
    """判断目标插件数据目录是否已有有效内容。"""
    metadata_file = plugin_data_dir / "memes_data.json"
    if metadata_file.is_file():
        return True

    memes_dir = plugin_data_dir / "memes"
    return memes_dir.is_dir() and any(memes_dir.iterdir())


def _copy_directory_contents(source_dir: Path, target_dir: Path) -> None:
    """合并复制目录内容，不覆盖已存在文件。"""
    for item in source_dir.iterdir():
        target_path = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target_path, dirs_exist_ok=True)
            continue
        if not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target_path)


def migrate_legacy_data_dir_if_needed(plugin_data_dir: Path) -> None:
    """将旧版 data/memes_data 安全迁移到 data/plugin_data/astrbot_plugin_suli_meme。"""
    legacy_data_dir = get_legacy_plugin_data_dir()
    if legacy_data_dir is None or not legacy_data_dir.exists():
        return

    if legacy_data_dir.resolve() == plugin_data_dir.resolve():
        return

    if _plugin_data_dir_has_content(plugin_data_dir):
        return

    try:
        plugin_data_dir.mkdir(parents=True, exist_ok=True)
        _copy_directory_contents(legacy_data_dir, plugin_data_dir)
        logger.info("检测到旧版插件数据目录，已迁移到: %s", plugin_data_dir)
    except Exception as exc:
        logger.error("迁移旧版插件数据目录失败: %s", exc)


PLUGIN_DATA_DIR = get_plugin_data_dir()
migrate_legacy_data_dir_if_needed(PLUGIN_DATA_DIR)
BASE_DATA_DIR = PLUGIN_DATA_DIR
MEMES_DIR = PLUGIN_DATA_DIR / "memes"
MEMES_DATA_PATH = PLUGIN_DATA_DIR / "memes_data.json"  # 类别描述数据文件路径
TEMP_DIR = PLUGIN_DATA_DIR / "temp"

# 确保目录存在
os.makedirs(MEMES_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# 启动时输出关键路径 (通过 logger 而非 print)
logger.info("插件目录: %s", PLUGIN_DIR)
logger.info("插件数据目录: %s", PLUGIN_DATA_DIR)
logger.info("表情包目录: %s", MEMES_DIR)

# 默认的类别描述
DEFAULT_CATEGORY_DESCRIPTIONS = {
    "开心": "心情愉快、好事发生、群友分享喜讯时",
    "笑死": "看到好笑的梗、精准吐槽、笑到停不下来",
    "生气": "被惹到、打抱不平、表达不满时",
    "难过": "伤心事、感动落泪、emo时刻",
    "惊讶": "意想不到、震撼消息、大跌眼镜",
    "无语": "看傻了、无话可说、扶额叹气（水群高频）",
    "害羞": "被夸害羞、被戳穿脸红、不好意思",
    "贴贴": "撒娇亲近、求安慰、对群友表达喜爱",
    "摸头": "安慰群友、长辈式关怀、摸摸不哭",
    "得意": "炫耀成果、得逞了、叉腰骄傲",
    "嫌弃": "拒绝看不上、吐槽、哒咩走开",
    "吃瓜": "围观群友吵架、前排看戏、吃瓜不嫌事大",
    "问好": "早安晚安打招呼、上线冒泡、离线道别",
    "点赞": "赞同附议、说得好、给你点个赞",
    "挑逗": "逗弄群友、撩一下就跑、故意气人、钓鱼执法",
    "水群": "万用表情，不知道发什么又想冒泡时从这里取",
    "尴尬": "社死现场、不知所措、尬住了、脚趾抠出三室一厅",
    "喝茶": "淡定围观、我就看看不说话、优雅看戏",
    "卖萌": "装可爱、撒娇、求关注、萌混过关",
    "打趣": "开玩笑、调侃群友、友好吐槽、损一下就跑",
    "摆烂": "放弃治疗、躺平、不想努力了、随便吧、开摆",
}
