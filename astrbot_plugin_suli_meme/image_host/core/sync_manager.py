import logging
from pathlib import Path

from tqdm import tqdm

from ..interfaces.image_host import ImageHostInterface
from .file_handler import FileHandler
from .upload_tracker import UploadTracker

logger = logging.getLogger(__name__)


class SyncManager:
    """同步管理器"""

    def __init__(
        self,
        image_host: ImageHostInterface,
        local_dir: Path,
        upload_tracker: UploadTracker | None = None,
    ):
        self.image_host = image_host
        self.file_handler = FileHandler(local_dir)
        self.upload_tracker = upload_tracker

    def _normalize_remote_id(self, remote_id: str, provider_name: str = None) -> str:
        """
        根据不同的图床提供商规范化远程文件ID

        Args:
            remote_id: 远程文件ID
            provider_name: 提供商名称

        Returns:
            规范化后的文件ID，用于与本地文件ID比较
        """
        if not provider_name:
            if hasattr(self.image_host, "config") and self.image_host.config:
                provider_name = self.image_host.config.get("provider", "").lower()
            elif hasattr(self.image_host, "__class__"):
                provider_name = self.image_host.__class__.__name__.lower()

        normalized_id = remote_id.replace("\\", "/")

        if provider_name:
            if "cloudflare_r2" in provider_name or "r2" in provider_name:
                if normalized_id.startswith("memes/"):
                    return normalized_id[6:]
            elif "stardots" in provider_name:
                pass

        return normalized_id

    def _extract_remote_size(self, image_info: dict) -> int | None:
        """提取远端文件大小，兼容不同 provider 的字段命名。"""
        candidate_keys = ("size", "file_size", "fileSize", "bytes", "length")
        for key in candidate_keys:
            value = image_info.get(key)
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    def _extract_local_size(self, image_info: dict) -> int:
        """提取本地文件大小。"""
        file_path = Path(image_info["path"])
        try:
            return file_path.stat().st_size
        except OSError:
            return 0

    def check_sync_status(self) -> dict[str, list[dict]]:
        """检查同步状态 - 简化版，只检查存在性"""
        logger.info("正在扫描本地文件...")
        local_images = self.file_handler.scan_local_images()
        logger.info("本地文件总数: %d", len(local_images))
        if len(local_images) > 5:
            logger.info("前5个文件:")
            for img in local_images[:5]:
                logger.info(
                    "  - [%s] %s", img.get("category", "根目录"), img["filename"]
                )

        logger.info("正在获取远程文件列表...")
        remote_images = self.image_host.get_image_list()
        logger.info("远程文件总数: %d", len(remote_images))
        if len(remote_images) > 5:
            logger.info("前5个文件:")
            for img in remote_images[:5]:
                logger.info(
                    "  - [%s] %s", img.get("category", "根目录"), img["filename"]
                )

        local_sizes_by_id = {
            img["id"].replace("\\", "/"): self._extract_local_size(img)
            for img in local_images
        }
        local_total_bytes = sum(local_sizes_by_id.values())
        local_average_size = (
            local_total_bytes / len(local_images) if local_images else 0
        )

        remote_sizes = [
            remote_size
            for img in remote_images
            if (remote_size := self._extract_remote_size(img)) is not None
        ]
        remote_total_bytes = sum(remote_sizes) if remote_sizes else 0
        remote_size_complete = len(remote_sizes) == len(remote_images)

        provider_name = None
        if hasattr(self.image_host, "config") and self.image_host.config:
            provider_name = self.image_host.config.get("provider", "").lower()

        remote_file_ids = set()
        for img in remote_images:
            remote_id = img["id"].replace("\\", "/")
            normalized_remote_id = self._normalize_remote_id(remote_id, provider_name)
            remote_file_ids.add(normalized_remote_id)

        local_file_ids = {img["id"].replace("\\", "/") for img in local_images}
        to_download = []
        for img in remote_images:
            remote_id = img["id"].replace("\\", "/")
            normalized_remote_id = self._normalize_remote_id(remote_id, provider_name)
            if normalized_remote_id not in local_file_ids:
                to_download.append(img)
        logger.info("本地不存在的文件: %d 个", len(to_download))

        to_upload = []
        for img in local_images:
            category = img.get("category", "")
            file_path = Path(img["path"])
            local_id = img["id"].replace("\\", "/")
            missing_on_remote = local_id not in remote_file_ids
            missing_upload_record = bool(self.upload_tracker) and (
                not self.upload_tracker.is_uploaded(file_path, category)
            )

            if missing_on_remote or missing_upload_record:
                to_upload.append(img)

        if self.upload_tracker:
            logger.info(
                "待上传文件: %d 个（按云端缺失文件和上传记录联合判断）", len(to_upload)
            )
        else:
            logger.info(
                "待上传文件: %d 个（按云端缺失文件判断）", len(to_upload)
            )

        to_delete_remote = to_download.copy()
        logger.info("云端多出的文件: %d 个", len(to_delete_remote))

        to_delete_local = []
        for img in local_images:
            local_id = img["id"].replace("\\", "/")
            if local_id not in remote_file_ids:
                to_delete_local.append(img)
        logger.info("本地多出的文件: %d 个", len(to_delete_local))

        remote_total_bytes_estimated = None
        remote_size_source = (
            "exact" if remote_size_complete or not remote_images else "unknown"
        )

        if remote_size_complete or not remote_images:
            remote_total_bytes_estimated = remote_total_bytes
        else:
            local_only_bytes = sum(
                local_sizes_by_id.get(img["id"].replace("\\", "/"), 0)
                for img in to_delete_local
            )
            download_known_bytes = 0
            download_unknown_count = 0
            for img in to_download:
                remote_size = self._extract_remote_size(img)
                if remote_size is None:
                    download_unknown_count += 1
                else:
                    download_known_bytes += remote_size

            if local_images or download_known_bytes:
                remote_total_bytes_estimated = int(
                    max(
                        0,
                        local_total_bytes
                        - local_only_bytes
                        + download_known_bytes
                        + (download_unknown_count * local_average_size),
                    )
                )
                remote_size_source = "local_estimate"

        return {
            "to_upload": to_upload,
            "to_download": to_download,
            "to_delete_local": to_delete_local,
            "to_delete_remote": to_delete_remote,
            "is_synced": not (
                to_upload or to_download or to_delete_local or to_delete_remote
            ),
            "remote_image_count": len(remote_images),
            "remote_total_bytes": (
                remote_total_bytes
                if remote_size_complete or not remote_images
                else None
            ),
            "remote_total_bytes_estimated": remote_total_bytes_estimated,
            "remote_size_source": remote_size_source,
            "remote_size_complete": remote_size_complete,
            "local_total_bytes": local_total_bytes,
        }

    def sync_to_remote(self) -> bool:
        """同步本地文件到远程 - 只上传未上传过的文件"""
        status = self.check_sync_status()

        if status.get("is_synced", False):
            logger.info("所有文件已上传，无需重复上传")
            return True

        to_upload = status["to_upload"]
        if to_upload:
            logger.info("开始上传 %d 个文件...", len(to_upload))
            uploaded_count = 0
            skipped_count = 0

            with tqdm(total=len(to_upload), desc="上传进度") as pbar:
                for image in to_upload:
                    file_path = Path(image["path"])
                    category = image.get("category", "")

                    try:
                        result = self.image_host.upload_image(file_path)

                        if self.upload_tracker:
                            remote_url = result.get("url", "")
                            self.upload_tracker.mark_uploaded(
                                file_path, category, remote_url
                            )

                        uploaded_count += 1
                        pbar.update(1)

                    except Exception as e:
                        logger.error("上传失败: %s - %s", file_path.name, e)
                        skipped_count += 1
                        pbar.update(1)

            logger.info(
                "上传完成: 成功 %d 个，失败 %d 个", uploaded_count, skipped_count
            )
        else:
            logger.info("没有需要上传的文件")

        return True

    def sync_from_remote(self) -> bool:
        """从远程同步文件到本地 - 只下载本地不存在的文件"""
        status = self.check_sync_status()

        if status.get("is_synced", False):
            logger.info("所有文件已存在，无需下载")
            return True

        to_download = status["to_download"]
        if to_download:
            logger.info("开始下载 %d 个文件...", len(to_download))
            downloaded_count = 0
            skipped_count = 0

            with tqdm(total=len(to_download), desc="下载进度") as pbar:
                for image in to_download:
                    try:
                        category = image.get("category", "")
                        filename = image["filename"]

                        save_path = self.file_handler.get_file_path(category, filename)

                        if save_path.exists():
                            logger.info("文件已存在，跳过: %s", filename)
                            skipped_count += 1
                            pbar.update(1)
                            continue

                        if self.image_host.download_image(image, save_path):
                            downloaded_count += 1
                            pbar.update(1)
                        else:
                            logger.error("下载失败: %s", filename)
                            skipped_count += 1
                            pbar.update(1)
                    except Exception as e:
                        logger.error("下载失败: %s - %s", filename, e)
                        skipped_count += 1
                        pbar.update(1)

            logger.info(
                "下载完成: 成功 %d 个，失败/跳过 %d 个",
                downloaded_count,
                skipped_count,
            )
        else:
            logger.info("没有需要下载的文件")

        return True

    def overwrite_to_remote(self) -> bool:
        """从本地覆盖云端 - 让云端完全和本地一致"""
        status = self.check_sync_status()

        self.sync_to_remote()

        to_delete_remote = status.get("to_delete_remote", [])
        if to_delete_remote:
            logger.info("开始清理云端多出的 %d 个文件...", len(to_delete_remote))
            deleted_count = 0
            for img in tqdm(to_delete_remote, desc="清理云端"):
                try:
                    if self.image_host.delete_image(img["id"]):
                        deleted_count += 1
                except Exception as e:
                    logger.error("删除云端文件失败: %s - %s", img["filename"], e)
            logger.info("云端清理完成: 成功删除 %d 个", deleted_count)
        else:
            logger.info("云端没有多出的文件，无需清理")

        return True

    def overwrite_from_remote(self) -> bool:
        """从云端覆盖本地 - 让本地完全和云端一致"""
        status = self.check_sync_status()

        self.sync_from_remote()

        to_delete_local = status.get("to_delete_local", [])
        if to_delete_local:
            logger.info("开始清理本地多出的 %d 个文件...", len(to_delete_local))
            deleted_count = 0
            for img in tqdm(to_delete_local, desc="清理本地"):
                try:
                    file_path = Path(img["path"])
                    if file_path.exists():
                        file_path.unlink()
                        deleted_count += 1
                        if self.upload_tracker:
                            self.upload_tracker.remove_record(
                                file_path, img.get("category", "")
                            )
                except Exception as e:
                    logger.error("删除本地文件失败: %s - %s", img["filename"], e)
            logger.info("本地清理完成: 成功删除 %d 个", deleted_count)
        else:
            logger.info("本地没有多出的文件，无需清理")

        return True
