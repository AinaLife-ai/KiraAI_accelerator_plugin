"""壁纸管理 —— 自动发现 + 提供文件服务。

设计：
  - 壁纸放在插件的 `wallpapers/` 目录（与 web/ 同级）
  - **自动发现**：扫目录里的 .webp/.jpg/.png，按文件名排序 ⇒ 用户丢新图进去就自动轮换
  - 通过 `@register.api` 暴露 `/wallpapers/<name>` 供面板读取
    （面板挂载在 `/page/plugin/<id>/index`，**无法直接访问插件目录里的图片**，
     必须走 API 路由）
  - 文件名白名单校验：只允许已发现的文件名，杜绝路径穿越
"""
from __future__ import annotations

import pathlib
from typing import Optional

# 支持的图片扩展名（按优先级排序，webp 最省流量）
EXTS = (".webp", ".jpg", ".jpeg", ".png")

IMAGE_MIME = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def wallpaper_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "wallpapers"


def discover(limit: int = 40) -> list[str]:
    """扫描壁纸目录，返回排序后的文件名列表（不含路径）。"""
    d = wallpaper_dir()
    if not d.is_dir():
        return []
    names: list[str] = []
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith(".") or p.name.startswith("_"):
            continue          # 跳过隐藏文件与 _contact_sheet 之类的辅助图
        if p.suffix.lower() in EXTS:
            names.append(p.name)
        if len(names) >= limit:
            break
    return names


def resolve(name: str) -> Optional[pathlib.Path]:
    """把请求的文件名解析成磁盘路径。

    ★ 安全：只接受 `discover()` 返回过的名字，**不做任何路径拼接**
      ⇒ 天然拒绝 `../`、绝对路径、符号链接等穿越手法。
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if name not in set(discover()):
        return None
    p = wallpaper_dir() / name
    return p if p.is_file() else None


def mime_for(name: str) -> str:
    return IMAGE_MIME.get(pathlib.Path(name).suffix.lower(), "application/octet-stream")


# ══════════════════════════════════════════════════════════════
# 用户上传（面板里的「+」方框）
# ══════════════════════════════════════════════════════════════
USER_PREFIX = "u_"                     # 用户上传的图统一前缀，便于区分/清理
MAX_UPLOAD_BYTES = 12 * 1024 * 1024    # 单张上限 12MB（再大就不是壁纸了）

def safe_name(original: str, data: bytes) -> Optional[str]:
    """给上传的图生成一个**安全的文件名**。

    ★ 不直接用客户端给的名字 —— 那是路径穿越的经典入口（`../../x.png`）。
      这里：只取扩展名、白名单校验，主体用时间戳+随机串。
    """
    import time, secrets
    ext = pathlib.Path(original or "").suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in EXTS:
        ext = ".png"                   # 没给扩展名就按 png 存（下面还会按 magic 校验）
    if len(data) > MAX_UPLOAD_BYTES:
        return None
    # 简单的 magic 校验：确认它真的是一张图，而不是改了名的其它文件
    head = data[:16]
    ok = (
        head.startswith(b"\xff\xd8\xff")                       # jpeg
        or head.startswith(b"\x89PNG\r\n\x1a\n")             # png
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")    # webp
        or head[:6] in (b"GIF87a", b"GIF89a")                 # gif（浏览器能显示）
    )
    if not ok:
        return None
    return f"{USER_PREFIX}{int(time.time())}_{secrets.token_hex(3)}{ext}"

def save_upload(original: str, data: bytes) -> Optional[str]:
    """把上传内容写进壁纸目录，返回文件名（失败返回 None）。"""
    name = safe_name(original, data)
    if not name:
        return None
    d = wallpaper_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / name).write_bytes(data)
    except Exception:  # noqa: BLE001
        return None
    return name

def remove(name: str) -> bool:
    """删除一张**用户上传**的图（内置图不允许删）。"""
    if not name.startswith(USER_PREFIX):
        return False               # ★ 内置壁纸不让删，避免用户误删后无法恢复
    p = resolve(name)
    if p is None:
        return False
    try:
        p.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False
