"""Operaciones de imagen compartidas por el pipeline (OpenCV).

Se inyectan en el caso de uso como callables, de modo que la capa de
aplicación no dependa de OpenCV.
"""

from __future__ import annotations

from typing import Any

import cv2


def encode_snapshot(
    frame: Any,
    max_width: int = 480,
    jpeg_quality: int = 65,
) -> bytes:
    """Comprime un frame BGR a JPEG para enviarlo al modelo de visión.

    Reduce el ancho para disminuir el coste de inferencia sin perder contexto.
    Devuelve ``b""`` si la codificación falla.
    """
    height, width = frame.shape[:2]
    if width > max_width:
        frame = cv2.resize(frame, (max_width, int(height * max_width / width)))
    ok, buffer = cv2.imencode(
        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
    )
    return bytes(buffer) if ok else b""


def mark_pending(annotated: Any, count: int) -> None:
    """Dibuja una banda naranja de "sospecha en verificación" (in-place).

    La confirmación de la IA puede tardar varios segundos; este aviso evita
    que el operador confunda una sospecha con una alarma confirmada.
    """
    text = f"SOSPECHA {count} EN VERIFICACION IA..."
    (text_width, text_height), _ = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2
    )
    cv2.rectangle(
        annotated, (10, 10), (20 + text_width, 20 + text_height + 10),
        (0, 165, 255), -1,
    )
    cv2.putText(
        annotated, text, (15, 15 + text_height + 5),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA,
    )
