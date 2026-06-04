"""PostgreSQL 自動備份至 Google Drive

環境變數：
  GOOGLE_DRIVE_FOLDER_ID       — Google Drive 目標資料夾 ID
  GOOGLE_SERVICE_ACCOUNT_JSON  — Service Account JSON 字串（Railway 環境變數）

依賴（requirements.txt 已含）：
  google-api-python-client, google-auth
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta

from loguru import logger

BACKUP_RETENTION_DAYS = 7
_BACKUP_PREFIX = "bloomberg_backup"


# ── Google Drive ───────────────────────────────────────────────────────────────

def _folder_id() -> str:
    fid = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
    if not fid:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID 未設定")
    return fid


def _build_drive_service():
    """從環境變數 GOOGLE_SERVICE_ACCOUNT_JSON 建立 Drive API 服務"""
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON 未設定")

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── pg_dump + gzip ─────────────────────────────────────────────────────────────

async def _pg_dump(db_url: str, gz_path: str) -> None:
    """執行 pg_dump，輸出壓縮 .gz 檔"""
    raw_path = gz_path[:-3]  # strip .gz

    proc = await asyncio.create_subprocess_exec(
        "pg_dump", "--format=plain", "--no-password",
        "--clean", "--if-exists", "--encoding=UTF8",
        f"--file={raw_path}", db_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_dump 失敗 (rc={proc.returncode}): {stderr.decode()[:400]}"
        )

    # Python gzip 壓縮（不依賴系統 gzip 指令）
    with open(raw_path, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    os.remove(raw_path)


# ── Upload ─────────────────────────────────────────────────────────────────────

async def _upload_to_drive(gz_path: str, filename: str) -> str:
    """上傳備份到 Drive，回傳 file_id"""
    from googleapiclient.http import MediaFileUpload

    loop = asyncio.get_running_loop()

    def _do():
        svc = _build_drive_service()
        metadata = {"name": filename, "parents": [_folder_id()]}
        media = MediaFileUpload(gz_path, mimetype="application/gzip", resumable=False)
        result = svc.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()
        return result.get("id", "")

    return await loop.run_in_executor(None, _do)


# ── List / delete ──────────────────────────────────────────────────────────────

async def list_backups() -> list[dict]:
    """列出 Google Drive 備份資料夾中的備份（最多 20 筆，按時間倒序）"""
    loop = asyncio.get_running_loop()

    def _do():
        svc = _build_drive_service()
        resp = svc.files().list(
            q=f"'{_folder_id()}' in parents and trashed=false "
              f"and name contains '{_BACKUP_PREFIX}'",
            fields="files(id, name, createdTime, size)",
            orderBy="createdTime desc",
            pageSize=20,
        ).execute()
        return resp.get("files", [])

    return await loop.run_in_executor(None, _do)


async def _delete_old_backups() -> int:
    """刪除超過 BACKUP_RETENTION_DAYS 天的備份，回傳刪除數"""
    loop = asyncio.get_running_loop()

    def _do():
        svc = _build_drive_service()
        resp = svc.files().list(
            q=f"'{_folder_id()}' in parents and trashed=false "
              f"and name contains '{_BACKUP_PREFIX}'",
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=50,
        ).execute()
        files = resp.get("files", [])

        cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION_DAYS)
        deleted = 0
        for f in files:
            ct = f.get("createdTime", "")
            if not ct:
                continue
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if dt < cutoff:
                try:
                    svc.files().delete(fileId=f["id"]).execute()
                    logger.info("[Backup] 刪除舊備份：%s", f["name"])
                    deleted += 1
                except Exception as e:
                    logger.warning("[Backup] 刪除失敗 %s: %s", f["name"], e)
        return deleted

    return await loop.run_in_executor(None, _do)


# ── LINE 警告 ──────────────────────────────────────────────────────────────────

async def _push_failure_alert(error_msg: str, ts: str) -> None:
    """備份失敗時推播 LINE 警告給管理員"""
    try:
        from .line_push import push_line_messages
        from ..models.database import settings

        admin_uid = settings.admin_line_uid or os.getenv("ADMIN_LINE_UID", "")
        if not admin_uid:
            logger.warning("[Backup] ADMIN_LINE_UID 未設定，無法推送失敗通知")
            return

        text = (
            f"⚠️ 資料庫備份失敗\n"
            f"時間：{ts}\n"
            f"錯誤：{error_msg[:120]}\n"
            f"請手動檢查"
        )
        await push_line_messages(
            admin_uid,
            [{"type": "text", "text": text}],
            context="backup.failure_alert",
        )
    except Exception as e:
        logger.error("[Backup] 推送失敗通知時出錯：%s", e)


# ── 主流程 ─────────────────────────────────────────────────────────────────────

async def run_backup() -> dict:
    """
    完整備份流程：pg_dump → gzip → Drive 上傳 → 清理舊檔
    回傳：{"ok": bool, "filename": str, "file_id": str, "size_mb": float, "error": str}
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    filename = f"{_BACKUP_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql.gz"

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        err = "DATABASE_URL 未設定"
        logger.error("[Backup] %s", err)
        await _push_failure_alert(err, ts)
        return {"ok": False, "filename": "", "file_id": "", "size_mb": 0, "error": err}

    tmp_dir = tempfile.mkdtemp(prefix="bloomberg_bk_")
    gz_path = os.path.join(tmp_dir, filename)

    try:
        logger.info("[Backup] 開始 pg_dump → %s", filename)
        await _pg_dump(db_url, gz_path)

        size_mb = round(os.path.getsize(gz_path) / 1024 / 1024, 2)
        logger.info("[Backup] pg_dump 完成，大小：%.2f MB", size_mb)

        logger.info("[Backup] 上傳 Google Drive…")
        file_id = await _upload_to_drive(gz_path, filename)
        logger.info("[Backup] 上傳成功 file_id=%s", file_id)

        deleted = await _delete_old_backups()
        if deleted:
            logger.info("[Backup] 刪除 %d 筆過期備份", deleted)

        return {"ok": True, "filename": filename, "file_id": file_id,
                "size_mb": size_mb, "error": ""}

    except Exception as e:
        err_str = str(e)
        logger.error("[Backup] 備份失敗：%s", err_str, exc_info=True)
        await _push_failure_alert(err_str, ts)
        return {"ok": False, "filename": filename, "file_id": "",
                "size_mb": 0, "error": err_str}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── 格式化 ─────────────────────────────────────────────────────────────────────

def format_backup_list(backups: list[dict]) -> str:
    """格式化備份清單為 LINE 訊息"""
    if not backups:
        return (
            "📦 目前無備份記錄\n\n"
            "請確認 Google Drive 設定是否正確，\n"
            "或輸入 /backup 立即執行一次備份"
        )

    lines = [f"📦 資料庫備份清單（保留 {BACKUP_RETENTION_DAYS} 天）", "─" * 18]
    for i, f in enumerate(backups[:7], 1):
        name    = f.get("name", "")
        created = f.get("createdTime", "")[:10]
        size_b  = int(f.get("size", 0) or 0)
        size_s  = f"{size_b / 1024 / 1024:.1f} MB" if size_b > 0 else "–"
        lines.append(f"{i}. {created}　{size_s}\n   {name}")

    lines.append(f"─" * 18)
    lines.append(f"共 {len(backups)} 筆備份")
    return "\n".join(lines)
