"""Heurísticas geométricas y de movimiento para detectar incidentes.

Las funciones son deterministas y no dependen de YOLO, lo que permite
probarlas con cajas sintéticas. ``motion_score`` importa OpenCV de forma
perezosa para que el resto del módulo se pueda usar sin cargar la librería.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DetBox:
    """Caja delimitadora normalizada (esquinas en píxeles)."""

    x1: float
    y1: float
    x2: float
    y2: float
    conf: float = 1.0
    cls_id: int = 0
    label: str = ""

    @property
    def w(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def h(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2.0

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def aspect(self) -> float:
        """Relación ancho/alto; una persona de pie suele tener < 1."""
        return self.w / self.h if self.h > 0 else 0.0


def center_distance(a: DetBox, b: DetBox) -> float:
    """Distancia euclídea entre los centros de dos cajas."""
    return ((a.cx - b.cx) ** 2 + (a.cy - b.cy) ** 2) ** 0.5


def is_close(a: DetBox, b: DetBox, factor: float = 0.7) -> bool:
    """Indica si dos personas están lo bastante cerca como para forcejear.

    Se compara la distancia entre centros con el tamaño medio de las cajas:
    así el umbral se adapta a la escala de la escena (personas lejanas o
    cercanas). ``factor=0.7`` equivale aproximadamente a hombro con hombro.
    """
    average_size = (a.w + a.h + b.w + b.h) / 4.0
    if average_size <= 0:
        return False
    return center_distance(a, b) < factor * average_size


def find_clusters(persons: list[DetBox], factor: float = 0.7) -> list[list[int]]:
    """Agrupa personas en contacto mediante componentes conexas.

    Returns:
        Índices de los grupos de dos o más personas. Los individuos aislados
        no se devuelven porque una persona sola no es una pelea.
    """
    count = len(persons)
    parent = list(range(count))

    def find(index: int) -> int:
        # Union-Find con compresión de caminos.
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_left] = root_right

    for i in range(count):
        for j in range(i + 1, count):
            if is_close(persons[i], persons[j], factor):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)
    return [group for group in groups.values() if len(group) >= 2]


def find_fallen(
    persons: list[DetBox],
    min_area: float = 4000,
    fall_aspect: float = 1.15,
) -> list[int]:
    """Detecta personas posiblemente caídas (caja más ancha que alta)."""
    return [
        index
        for index, person in enumerate(persons)
        if person.area >= min_area and person.aspect >= fall_aspect
    ]


def motion_score(prev_gray, gray) -> float:
    """Energía de movimiento media (0-255) entre dos frames en gris.

    Se importa OpenCV aquí dentro para mantener las heurísticas puras
    utilizables (y testeables) sin cargar la librería completa.
    """
    import cv2
    import numpy as np

    if prev_gray is None or gray is None:
        return 0.0
    if prev_gray.shape != gray.shape:
        gray = cv2.resize(gray, (prev_gray.shape[1], prev_gray.shape[0]))
    return float(np.mean(cv2.absdiff(prev_gray, gray)))
