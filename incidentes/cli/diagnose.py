"""Diagnóstico por etapas del pipeline: YOLO -> heurística -> Ollama.

Sirve para responder preguntas concretas cuando no hay alarma:

- ¿YOLO detecta personas en el video?
- ¿La heurística encuentra grupos, movimiento alto o armas?
- ¿El modelo de visión confirma violencia en el mejor frame?

Uso::

    python -m incidentes.cli.diagnose "C:\\ruta\\a\\video.mp4"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
from ultralytics import YOLO

from incidentes.config import AppConfig
from incidentes.domain.entities import DetectionConfig, Suspicion
from incidentes.infrastructure.detection.heuristics import (
    DetBox,
    find_clusters,
    find_fallen,
    motion_score,
)
from incidentes.infrastructure.verification.ollama_verifier import (
    OllamaViolenceVerifier,
)
from incidentes.infrastructure.video.opencv_reader import get_video_info


def _classify_boxes(boxes, names, config: DetectionConfig):
    """Separa las detecciones relevantes en personas, armas y bolsos."""
    persons: list[DetBox] = []
    weapons: list[DetBox] = []
    bags: list[DetBox] = []
    if boxes is None:
        return persons, weapons, bags

    for box in boxes:
        x1, y1, x2, y2 = (float(value) for value in box.xyxy[0].tolist())
        class_id = int(box.cls[0])
        confidence = float(box.conf[0])
        detection = DetBox(
            x1,
            y1,
            x2,
            y2,
            confidence,
            class_id,
            str(names.get(class_id, class_id)),
        )
        if class_id == config.person_class_id:
            persons.append(detection)
        elif class_id in config.weapon_class_ids:
            weapons.append(detection)
        elif class_id in config.bag_class_ids:
            bags.append(detection)
    return persons, weapons, bags


def _diagnose_stage_one(video_path: str, config: AppConfig) -> tuple[dict, tuple | None]:
    """Recorre ~30 frames muestreados y resume las señales de la etapa 1."""
    detection = DetectionConfig(
        model_path=config.model_path,
        person_class_id=config.person_class_id,
        confidence=config.confidence,
        motion_threshold=config.motion_threshold,
        proximity_factor=config.proximity_factor,
        min_persistence=config.suspicion_min_persistence,
        cooldown_frames=config.cooldown_frames,
    )
    info = get_video_info(video_path)
    print(
        f"Video: {info.width}x{info.height} @ {info.fps}fps, "
        f"{info.total_frames} frames"
    )

    model = YOLO(detection.model_path)
    wanted = sorted(
        {
            detection.person_class_id,
            *detection.weapon_class_ids,
            *detection.bag_class_ids,
        }
    )
    capture = cv2.VideoCapture(video_path)
    step = max(1, info.total_frames // 30)  # ~30 muestras

    stats = {
        "frames_vistos": 0,
        "con_personas": 0,
        "con_grupo": 0,
        "con_movimiento_alto": 0,
        "con_arma": 0,
        "max_personas": 0,
    }
    previous_gray = None
    best: tuple | None = None
    index = 0

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            index += 1
            if index % step != 0:
                continue

            stats["frames_vistos"] += 1
            result = model(
                frame, classes=wanted, conf=detection.confidence, verbose=False
            )[0]
            persons, weapons, bags = _classify_boxes(
                result.boxes, model.names, detection
            )

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # El movimiento se mide entre muestras (step frames), no entre
            # frames consecutivos; sirve como indicador, no como valor exacto.
            motion = motion_score(previous_gray, gray)
            previous_gray = gray

            clusters = find_clusters(persons, detection.proximity_factor)
            fallen = find_fallen(
                persons, detection.min_person_area, detection.fall_aspect_ratio
            )
            if persons:
                stats["con_personas"] += 1
            if clusters:
                stats["con_grupo"] += 1
            if motion >= detection.motion_threshold:
                stats["con_movimiento_alto"] += 1
            if weapons:
                stats["con_arma"] += 1
            stats["max_personas"] = max(stats["max_personas"], len(persons))

            print(
                f"frame {index}: personas={len(persons)} "
                f"areas={[round(person.area) for person in persons]} "
                f"grupos={len(clusters)} mov={motion:.1f} "
                f"armas={len(weapons)} bolsos={len(bags)} caidas={len(fallen)}"
            )

            if best is None or len(persons) > len(best[1]):
                best = (frame, persons, index, motion, clusters, weapons)
    finally:
        capture.release()

    print("\nResumen etapa 1:", stats)
    return stats, best


def _diagnose_stage_two(
    best: tuple,
    info,
    config: AppConfig,
) -> None:
    """Envía el mejor frame al modelo de visión y muestra el veredicto."""
    frame, persons, frame_index, motion, clusters, weapons = best
    height, width = frame.shape[:2]
    snapshot_width = min(config.snapshot_width, width)
    small = cv2.resize(frame, (snapshot_width, int(height * snapshot_width / width)))
    _, buffer = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 65])

    suspicion = Suspicion(
        frame=frame_index,
        time_sec=frame_index / (info.fps or 30),
        type="PELEA",
        label=(
            f"PELEA: {len(persons)} personas, mov {motion:.0f}, "
            f"grupos {len(clusters)}, armas {len(weapons)}"
        ),
        heuristic_confidence=0.8,
    )
    verifier = OllamaViolenceVerifier(
        model=config.ollama_model,
        base_url=config.ollama_url,
        timeout_sec=config.ollama_timeout_sec,
    )
    print(f"\nEnviando el frame {frame_index} a {config.ollama_model}...")
    started = time.time()
    verdict = verifier.verify(bytes(buffer), suspicion)
    elapsed = time.time() - started
    print(
        f"Veredicto en {elapsed:.1f}s: violent={verdict.violent} "
        f"tipo={verdict.type} conf={verdict.confidence} "
        f"motivo='{verdict.reason}'"
    )
    if not verdict.violent:
        print(
            "La IA vio la imagen pero NO la considera violencia "
            "(o falló: revisa el motivo)."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnóstico por etapas del detector de incidentes"
    )
    parser.add_argument("video", help="ruta al video a analizar")
    args = parser.parse_args(argv)

    video_path = str(Path(args.video).expanduser())
    if not Path(video_path).is_file():
        print(f"[ERROR] No existe el video: {video_path}")
        return 2

    config = AppConfig.from_env()
    verifier = OllamaViolenceVerifier(
        model=config.ollama_model,
        base_url=config.ollama_url,
        timeout_sec=config.ollama_timeout_sec,
    )
    ollama_ok = verifier.ping()
    print(
        f"Ollama ping ({config.ollama_url}): "
        f"{'OK' if ollama_ok else 'FALLA - sin IA no hay alarmas'}"
    )

    stats, best = _diagnose_stage_one(video_path, config)
    if stats["con_personas"] == 0:
        print(
            "CAUSA: YOLO no ve personas (video oscuro/lejano/borroso). "
            "Baja INCIDENTES_CONFIDENCE o usa otra resolución."
        )
        return 0
    if stats["con_grupo"] == 0:
        print(
            "CAUSA: hay personas pero nunca se juntan según proximity_factor. "
            "Una pelea con una sola persona visible no genera sospecha."
        )
        return 0
    if stats["con_movimiento_alto"] == 0:
        print(
            f"CAUSA: movimiento bajo el umbral "
            f"({config.motion_threshold}). Baja INCIDENTES_MOTION_THRESHOLD. "
            "Se probará igual la IA con el mejor frame."
        )

    if not ollama_ok:
        print("[AVISO] Se omite la etapa 2 porque Ollama no está disponible.")
        return 0
    if best is None:
        print("[AVISO] No se encontró un frame representativo.")
        return 0

    info = get_video_info(video_path)
    _diagnose_stage_two(best, info, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
