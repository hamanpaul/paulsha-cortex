"""Deployment canary 用的極小模組。"""


def normalize_label(value: str) -> str:
    """去除標籤前後空白。"""

    return value.strip()
