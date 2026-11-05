"""Pruebas de las heurísticas geométricas (sin YOLO ni OpenCV)."""

from __future__ import annotations

from incidentes.infrastructure.detection.heuristics import (
    DetBox,
    center_distance,
    find_clusters,
    find_fallen,
    is_close,
)


def box(cx: float, cy: float, width: float = 100, height: float = 200) -> DetBox:
    """Crea una caja centrada en ``(cx, cy)``."""
    return DetBox(
        x1=cx - width / 2,
        y1=cy - height / 2,
        x2=cx + width / 2,
        y2=cy + height / 2,
    )


def test_center_distance() -> None:
    assert center_distance(box(0, 0), box(30, 40)) == 50


def test_is_close_uses_person_scale() -> None:
    assert is_close(box(100, 100), box(150, 100))
    assert not is_close(box(100, 100), box(500, 500))


def test_is_close_rejects_degenerate_boxes() -> None:
    empty = DetBox(0, 0, 0, 0)
    assert not is_close(empty, empty)


def test_find_clusters_groups_only_close_people() -> None:
    persons = [box(100, 100), box(150, 100), box(600, 100)]
    clusters = find_clusters(persons, factor=0.7)
    assert clusters == [[0, 1]]


def test_find_clusters_ignores_single_person() -> None:
    assert find_clusters([box(100, 100)]) == []


def test_find_fallen_detects_wide_large_boxes() -> None:
    fallen = box(100, 100, width=300, height=120)
    standing = box(300, 100, width=100, height=250)
    tiny = box(500, 100, width=30, height=10)
    assert [index for index in find_fallen([fallen, standing, tiny])] == [0]
