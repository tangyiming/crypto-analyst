"""Prompt 模板加载与渲染。

Prompts 单独放在 prompts/ 目录下，每个版本独立文件，方便 A/B 测试。
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"


def load_system_prompt(version: str = "v1") -> str:
    """加载系统提示词。"""
    path = PROMPTS_DIR / f"system_{version}.md"
    return path.read_text(encoding="utf-8")


def load_user_template(version: str = "v1") -> str:
    """加载用户消息模板（带 {placeholders}）。"""
    path = PROMPTS_DIR / f"user_template_{version}.md"
    return path.read_text(encoding="utf-8")


def render_user_message(template: str, **kwargs) -> str:
    """渲染用户消息（简单 format 替换）。"""
    return template.format(**kwargs)
