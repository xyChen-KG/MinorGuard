"""One numbered image per observation, and one JSONL record per operation."""

import io
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from PIL import ImageDraw

from .common import Frame, box_pixels


class Recorder:
    def __init__(self, output: Path):
        self.root = output / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.root.chmod(0o700)
        (self.root / "images").mkdir(mode=0o700)
        self.trace_path = self.root / "trace.jsonl"
        self._write(self.trace_path, b"")

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        temporary.replace(path)

    def frame_path(self, number: int) -> str:
        return "images/%03d.png" % number

    def save_frame(self, number: int, frame: Frame) -> str:
        path = self.frame_path(number)
        self._write(self.root / path, frame.png)
        return path

    def mark_tap(self, number: int, frame: Frame, box: Dict[str, Any]) -> None:
        image = frame.image()
        draw = ImageDraw.Draw(image)
        left, top, right, bottom = box_pixels(box, frame.width, frame.height)
        thickness = max(2, round(min(frame.width, frame.height) / 250))
        draw.rectangle((left, top, right, bottom), outline="#ff253a", width=thickness)
        x, y = (left + right) // 2, (top + bottom) // 2
        radius = max(4, thickness * 2)
        draw.line((x - radius, y, x + radius, y), fill="#ff253a", width=thickness)
        draw.line((x, y - radius, x, y + radius), fill="#ff253a", width=thickness)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self._write(self.root / self.frame_path(number), buffer.getvalue())

    def append(self, record: Dict[str, Any]) -> None:
        fd = os.open(str(self.trace_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def save_json(self, name: str, value: Dict[str, Any]) -> None:
        self._write(self.root / name, (json.dumps(value, ensure_ascii=False, indent=2,
                                                allow_nan=False) + "\n").encode("utf-8"))
