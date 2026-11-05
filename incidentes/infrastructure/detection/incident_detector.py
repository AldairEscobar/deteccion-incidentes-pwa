"""Adaptador de detección basado en YOLO + heurísticas.

Limitación honesta: ``yolo11n.pt`` (COCO) **no** tiene una clase de "pelea" ni
de "robo". Este adaptador combina señales indirectas:

- personas (clase 0) y su proximidad;
- armas visibles de COCO (bate, cuchillo, tijeras);
- bolsos/mochilas como pista de robo;
- movimiento alto entre frames y personas caídas.

Si existe un modelo propio (``violence.pt``) se usa directamente y se omite
la heurística. Las reglas solo generan **sospechas**; la confirmación final
la hace la etapa 2.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any

import cv2
from ultralytics import YOLO

from incidentes.domain.entities import (
    DetectionConfig,
    FrameAnalysis,
    Incident,
    IncidentType,
)
from incidentes.domain.ports import IncidentDetector
from incidentes.infrastructure.detection.heuristics import (
    DetBox,
    find_clusters,
    find_fallen,
    motion_score,
)

log = logging.getLogger(__name__)


class YoloIncidentDetector(IncidentDetector):
    """Detector de sospechas sobre YOLO-COCO o un modelo de violencia propio."""

    def __init__(
        self,
        config: DetectionConfig,
        violence_model_path: str = "violence.pt",
        mark_confirmed: bool = True,
    ) -> None:
        """
        Args:
            config: umbrales y clases a detectar.
            violence_model_path: modelo propio opcional; si no existe se usa
                la heurística sobre COCO.
            mark_confirmed: ``False`` activa el modo sospecha (sin overlay
                rojo y con ``alert=False``) usado por el pipeline de 2 etapas.
        """
        self._config = config
        self._model = YOLO(config.model_path)
        self._custom = self._try_load_custom(violence_model_path)
        self._mark_confirmed = mark_confirmed
        self._wanted = sorted(
            {config.person_class_id, *config.weapon_class_ids, *config.bag_class_ids}
        )
        self._prev_gray = None
        self._streak: dict[str, int] = defaultdict(int)

    @staticmethod
    def _try_load_custom(path: str):
        """Carga el modelo propio si existe; ante error cae a la heurística."""
        if path and os.path.isfile(path):
            try:
                return YOLO(path)
            except Exception as exc:
                log.warning(
                    "no se pudo cargar %s: %s. Se usará la heurística.", path, exc
                )
        return None

    def analyze(self, frame: Any) -> FrameAnalysis:
        """Analiza un frame BGR y devuelve sospechas + frame anotado."""
        if self._custom is not None:
            return self._analyze_custom(frame)
        return self._analyze_heuristic(frame)

    # -- Ruta A: modelo propio de violencia ---------------------------------
    def _analyze_custom(self, frame: Any) -> FrameAnalysis:
        assert self._custom is not None  # garantizado por analyze()
        result = self._custom(frame, conf=self._config.confidence, verbose=False)[0]
        raw: list[Incident] = []
        if result.boxes is not None:
            for box in result.boxes:
                class_id = int(box.cls[0])
                confidence = float(box.conf[0])
                name = self._custom.names.get(class_id, str(class_id))
                raw.append(
                    Incident(
                        IncidentType.VIOLENCE,
                        confidence,
                        f"{name} {confidence:.2f}",
                    )
                )

        confirmed = self._smooth(raw)
        if not self._mark_confirmed:
            return FrameAnalysis(result.plot(), tuple(confirmed), False)
        return FrameAnalysis(
            self._overlay(result.plot(), confirmed),
            tuple(confirmed),
            bool(confirmed),
        )

    # -- Ruta B: heurística sobre COCO --------------------------------------
    def _analyze_heuristic(self, frame: Any) -> FrameAnalysis:
        result = self._model(
            frame,
            classes=self._wanted,
            conf=self._config.confidence,
            verbose=False,
        )[0]

        persons: list[DetBox] = []
        weapons: list[DetBox] = []
        bags: list[DetBox] = []
        if result.boxes is not None:
            names = self._model.names
            for box in result.boxes:
                x1, y1, x2, y2 = (float(value) for value in box.xyxy[0].tolist())
                class_id = int(box.cls[0])
                confidence = float(box.conf[0])
                detection = DetBox(
                    x1, y1, x2, y2, confidence, class_id,
                    str(names.get(class_id, class_id)),
                )
                if class_id == self._config.person_class_id:
                    persons.append(detection)
                elif class_id in self._config.weapon_class_ids:
                    weapons.append(detection)
                elif class_id in self._config.bag_class_ids:
                    bags.append(detection)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        motion = motion_score(self._prev_gray, gray)
        self._prev_gray = gray
        high_motion = motion >= self._config.motion_threshold

        clusters = find_clusters(persons, self._config.proximity_factor)
        fallen = find_fallen(
            persons, self._config.min_person_area, self._config.fall_aspect_ratio
        )
        clustered_people = sum(len(cluster) for cluster in clusters)

        raw: list[Incident] = []
        if weapons:
            weapon = max(weapons, key=lambda item: item.conf)
            raw.append(
                Incident(
                    IncidentType.WEAPON,
                    weapon.conf,
                    f"ARMA: {weapon.label} {weapon.conf:.2f}",
                )
            )
        if clusters and high_motion:
            confidence = min(
                0.95, 0.55 + 0.1 * clustered_people + min(motion / 60.0, 0.3)
            )
            raw.append(
                Incident(
                    IncidentType.FIGHT,
                    confidence,
                    f"PELEA: {clustered_people} personas, mov {motion:.0f}",
                )
            )
        elif clusters and len(clusters[0]) >= 3:
            raw.append(
                Incident(
                    IncidentType.FIGHT,
                    0.6,
                    f"PELEA: grupo de {len(clusters[0])} personas",
                )
            )
        if fallen and (clusters or high_motion or weapons):
            raw.append(
                Incident(
                    IncidentType.FALL,
                    0.7,
                    f"CAIDA: {len(fallen)} persona(s) en suelo",
                )
            )
        # Robo: arma + grupo junto, o grupo + movimiento + bolso (tirón).
        if weapons and clusters:
            raw.append(Incident(IncidentType.ROBBERY, 0.8, "ROBO: arma + grupo junto"))
        elif clusters and high_motion and bags:
            raw.append(
                Incident(IncidentType.ROBBERY, 0.6, "ROBO SOSPECHOSO: tirón de bolso")
            )

        confirmed = self._smooth(raw)
        annotated = result.plot()
        if not self._mark_confirmed:
            return FrameAnalysis(annotated, tuple(confirmed), False)
        return FrameAnalysis(
            self._overlay(annotated, confirmed, persons, clusters, fallen),
            tuple(confirmed),
            bool(confirmed),
        )

    # -- Suavizado temporal --------------------------------------------------
    def _smooth(self, raw: list[Incident]) -> list[Incident]:
        """Exige ``min_persistence`` frames consecutivos para confirmar un tipo.

        Evita falsos positivos de un solo frame (p. ej. una detección
        esporádica de YOLO).
        """
        present = {incident.type for incident in raw}
        best: dict[str, Incident] = {}
        for incident in raw:
            current = best.get(incident.type)
            if current is None or incident.confidence > current.confidence:
                best[incident.type] = incident

        confirmed: list[Incident] = []
        for incident_type in (
            IncidentType.WEAPON,
            IncidentType.FIGHT,
            IncidentType.FALL,
            IncidentType.ROBBERY,
            IncidentType.VIOLENCE,
        ):
            if incident_type in present:
                self._streak[incident_type] += 1
            else:
                self._streak[incident_type] = 0
            if (
                self._streak[incident_type] >= self._config.min_persistence
                and incident_type in best
            ):
                confirmed.append(best[incident_type])
        return confirmed

    # -- Dibujo --------------------------------------------------------------
    @staticmethod
    def _overlay(
        annotated: Any,
        incidents: list[Incident],
        persons: list[DetBox] | None = None,
        clusters: list[list[int]] | None = None,
        fallen: list[int] | None = None,
    ) -> Any:
        """Dibuja banda roja, etiquetas y cajas de los implicados (in-place)."""
        if not incidents:
            return annotated

        height, width = annotated.shape[:2]
        cv2.rectangle(annotated, (0, 0), (width - 1, height - 1), (0, 0, 255), 8)
        y = 30
        for incident in incidents:
            text = f"ALERTA: {incident.label}"
            (text_width, text_height), _ = cv2.getTextSize(
                text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2
            )
            cv2.rectangle(
                annotated,
                (10, y - text_height - 10),
                (10 + text_width + 10, y + 10),
                (0, 0, 255),
                -1,
            )
            cv2.putText(
                annotated, text, (15, y), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (255, 255, 255), 2, cv2.LINE_AA,
            )
            y += text_height + 20

        if persons and clusters:
            involved = {index for cluster in clusters for index in cluster}
            for index in involved:
                person = persons[index]
                cv2.rectangle(
                    annotated,
                    (int(person.x1), int(person.y1)),
                    (int(person.x2), int(person.y2)),
                    (0, 0, 255),
                    3,
                )
        if persons and fallen:
            for index in fallen:
                person = persons[index]
                cv2.rectangle(
                    annotated,
                    (int(person.x1), int(person.y1)),
                    (int(person.x2), int(person.y2)),
                    (0, 165, 255),
                    3,
                )
        return annotated
