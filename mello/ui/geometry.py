"""
Rect - a rectangle on the panel, used to find out what a touch hit.
"""
from typing import Tuple


class Rect:
    """x, y, width, height in panel pixels. A point on the right or bottom
    edge is outside, like the touch areas always were."""

    __slots__ = ('x', 'y', 'width', 'height')

    def __init__(self, x: int, y: int, width: int, height: int):
        self.x = x
        self.y = y
        self.width = width
        self.height = height

    def collidepoint(self, *pos) -> bool:
        x, y = pos[0] if len(pos) == 1 else pos
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> Tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def __iter__(self):
        return iter((self.x, self.y, self.width, self.height))

    def __eq__(self, other) -> bool:
        return tuple(self) == tuple(other)

    def __repr__(self) -> str:
        return f'Rect({self.x}, {self.y}, {self.width}, {self.height})'
