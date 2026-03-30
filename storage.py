"""
File storage layer for Railway Volume.
Manages persistent PDF/Markdown file storage with organized directory structure.
"""

import hashlib
import logging
import os
import shutil
from datetime import datetime
from typing import Optional

from config import StorageConfig

logger = logging.getLogger("pageindex.storage")


class FileStorage:
    """Persistent file storage on Railway Volume."""

    def __init__(self, config: StorageConfig):
        self.config = config
        self.base_path = config.volume_path

    async def initialize(self):
        """Create storage directories."""
        for subdir in ["uploads", "processing", "archive"]:
            path = os.path.join(self.base_path, subdir)
            os.makedirs(path, exist_ok=True)
            logger.info(f"Storage directory ready: {path}")

        # Check writability
        test_file = os.path.join(self.base_path, ".write_test")
        try:
            with open(test_file, "w") as f:
                f.write("ok")
            os.unlink(test_file)
            logger.info(f"Volume storage verified: {self.base_path}")
        except OSError as e:
            logger.error(f"Volume not writable: {self.base_path} — {e}")
            raise RuntimeError(f"Storage volume not writable: {e}")

    def health_check(self) -> dict:
        """Check storage health and disk usage."""
        try:
            stat = shutil.disk_usage(self.base_path)
            total_gb = round(stat.total / (1024 ** 3), 2)
            used_gb = round(stat.used / (1024 ** 3), 2)
            free_gb = round(stat.free / (1024 ** 3), 2)
            pct_used = round(stat.used / stat.total * 100, 1)

            file_count = sum(
                len(files)
                for _, _, files in os.walk(self.base_path)
            )

            return {
                "status": "healthy",
                "path": self.base_path,
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": free_gb,
                "pct_used": pct_used,
                "file_count": file_count,
            }
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    def _doc_dir(self, doc_id: str) -> str:
        """Get the directory for a specific document."""
        # Use first 4 chars as shard prefix to avoid too many files in one dir
        shard = doc_id[3:7] if len(doc_id) > 6 else "0000"
        return os.path.join(self.base_path, "uploads", shard, doc_id)

    async def store_file(
        self,
        doc_id: str,
        file_content: bytes,
        original_filename: str,
    ) -> str:
        """
        Store an uploaded file persistently.
        Returns the stored file path.
        """
        # Validate
        ext = os.path.splitext(original_filename)[1].lower()
        if ext not in self.config.allowed_extensions:
            raise ValueError(
                f"File type '{ext}' not allowed. "
                f"Accepted: {', '.join(self.config.allowed_extensions)}"
            )

        if len(file_content) > self.config.max_file_size_bytes:
            raise ValueError(
                f"File too large ({len(file_content) / 1024 / 1024:.1f}MB). "
                f"Max: {self.config.max_file_size_mb}MB"
            )

        # Create document directory
        doc_dir = self._doc_dir(doc_id)
        os.makedirs(doc_dir, exist_ok=True)

        # Store with sanitized filename
        safe_name = self._sanitize_filename(original_filename)
        file_path = os.path.join(doc_dir, safe_name)

        with open(file_path, "wb") as f:
            f.write(file_content)

        # Write metadata sidecar
        meta_path = os.path.join(doc_dir, "_meta.json")
        import json
        with open(meta_path, "w") as f:
            json.dump({
                "doc_id": doc_id,
                "original_filename": original_filename,
                "stored_filename": safe_name,
                "size_bytes": len(file_content),
                "sha256": hashlib.sha256(file_content).hexdigest(),
                "stored_at": datetime.utcnow().isoformat(),
            }, f, indent=2)

        logger.info(
            f"File stored: {file_path} "
            f"({len(file_content) / 1024:.1f}KB)"
        )
        return file_path

    async def get_file_path(self, doc_id: str) -> Optional[str]:
        """Get the stored file path for a document."""
        doc_dir = self._doc_dir(doc_id)
        if not os.path.exists(doc_dir):
            return None

        # Find the actual file (not the metadata sidecar)
        for fname in os.listdir(doc_dir):
            if fname.startswith("_"):
                continue
            fpath = os.path.join(doc_dir, fname)
            if os.path.isfile(fpath):
                return fpath
        return None

    async def delete_file(self, doc_id: str) -> bool:
        """Delete all files for a document."""
        doc_dir = self._doc_dir(doc_id)
        if os.path.exists(doc_dir):
            shutil.rmtree(doc_dir)
            logger.info(f"Files deleted for {doc_id}")
            return True
        return False

    async def get_file_size(self, doc_id: str) -> Optional[int]:
        """Get file size in bytes."""
        path = await self.get_file_path(doc_id)
        if path and os.path.exists(path):
            return os.path.getsize(path)
        return None

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """Sanitize filename for safe storage."""
        # Keep only the basename
        name = os.path.basename(filename)
        # Replace unsafe chars
        safe = "".join(
            c if c.isalnum() or c in ".-_" else "_"
            for c in name
        )
        # Ensure it's not empty
        return safe or "document.pdf"
