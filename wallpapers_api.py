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
