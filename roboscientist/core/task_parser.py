"""Small deterministic parser for the intentionally narrow S0/S1 task grammar."""

from roboscientist.schemas import TaskSpec


COLORS = {"red": "red", "红": "red", "blue": "blue", "蓝": "blue", "yellow": "yellow", "黄": "yellow"}


def parse_task(text: str) -> TaskSpec:
    normalized = text.lower()
    color = next((value for key, value in COLORS.items() if key in normalized), None)
    if not color or not any(word in normalized for word in ("方块", "cube", "block")):
        raise ValueError("task must name a supported color block")
    zone = "right" if any(word in normalized for word in ("右", "right")) else "target"
    if not any(word in normalized for word in ("放", "place", "目标", "target")):
        raise ValueError("task must name a target zone")
    return TaskSpec(source_text=text, target_color=color, target_zone=zone)

