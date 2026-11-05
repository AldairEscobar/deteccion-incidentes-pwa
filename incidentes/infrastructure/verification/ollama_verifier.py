"""Verificación de violencia con un modelo de visión servido por Ollama.

Solo usa la librería estándar (``urllib``) para no añadir dependencias al
cliente. Contrato **fail-closed**: ante error, timeout o respuesta inválida
devuelve ``Verdict(violent=False)``. La alarma solo suena con confirmación
positiva.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.request

from incidentes.domain.entities import IncidentType, Suspicion, Verdict
from incidentes.domain.ports import ViolenceVerifier

log = logging.getLogger(__name__)

VALID_TYPES = {
    IncidentType.FIGHT,
    IncidentType.ROBBERY,
    IncidentType.WEAPON,
    IncidentType.FALL,
    IncidentType.VIOLENCE,
    IncidentType.NOTHING,
}

PROMPT_TEMPLATE = (
    "Eres un verificador de videovigilancia. YOLO ya detecto senales de '{stype}' "
    "({slabel}): grupo de personas juntas + movimiento alto y/o caida. "
    "Confirma con la imagen: ¿hay persona en el SUELO con otros encima/alrededor, "
    "forcejeo, agarrones, empujones, golpes o arma esgrimida? "
    "Someter/inmovilizar a alguien en el suelo SI es violencia. "
    "Responde SOLO este JSON, sin markdown ni texto extra: "
    '{{"violent": true|false, "type": "PELEA"|"ROBO"|"ARMA"|"CAIDA"|"VIOLENCIA"|"NADA", '
    '"confidence": 0.0-1.0, "reason": "motivo en 10 palabras o menos"}}'
)


class OllamaViolenceVerifier(ViolenceVerifier):
    """Cliente del endpoint ``/api/chat`` de Ollama con modelos multimodales."""

    def __init__(
        self,
        model: str = "qwen2.5vl:7b",
        base_url: str = "http://localhost:11434",
        timeout_sec: int = 60,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec

    @property
    def name(self) -> str:
        return f"ollama/{self._model}"

    def ping(self) -> bool:
        """Comprueba que Ollama responde. Nunca lanza."""
        try:
            request = urllib.request.Request(f"{self._base_url}/api/tags")
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status == 200
        except Exception:
            return False

    def verify(self, jpeg_bytes: bytes, suspicion: Suspicion) -> Verdict:
        """Envía el JPEG y el contexto al modelo; parsea un veredicto JSON."""
        suspicion_type = getattr(suspicion, "type", IncidentType.NOTHING)
        suspicion_label = getattr(suspicion, "label", "")
        try:
            payload = {
                "model": self._model,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 200},
                "messages": [
                    {
                        "role": "user",
                        "content": PROMPT_TEMPLATE.format(
                            stype=suspicion_type, slabel=suspicion_label
                        ),
                        "images": [base64.b64encode(jpeg_bytes).decode()],
                    }
                ],
            }
            request = urllib.request.Request(
                f"{self._base_url}/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode())
            return self.parse_verdict(
                body["message"]["content"], fallback_type=suspicion_type
            )
        except Exception as exc:
            log.warning(
                "verificador %s falló (fail-closed, sin alarma): %s",
                self._model,
                exc,
            )
            return Verdict(
                False, 0.0, IncidentType.NOTHING, f"verifier-error: {exc}"[:200]
            )

    @staticmethod
    def parse_verdict(text: str, fallback_type: str = IncidentType.NOTHING) -> Verdict:
        """Extrae el JSON aunque el modelo añada fences o texto alrededor."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            newline = cleaned.find("\n")
            cleaned = cleaned[newline + 1:] if newline != -1 else cleaned

        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return Verdict(False, 0.0, IncidentType.NOTHING, "respuesta no-JSON")

        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return Verdict(False, 0.0, IncidentType.NOTHING, "JSON inválido")

        violent = bool(data.get("violent", False))
        incident_type = str(data.get("type", IncidentType.NOTHING)).upper()
        if incident_type not in VALID_TYPES:
            incident_type = fallback_type if violent else IncidentType.NOTHING

        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5

        reason = str(data.get("reason", data.get("what", "")))[:200]
        if not violent:
            return Verdict(False, confidence, IncidentType.NOTHING, reason)
        return Verdict(True, confidence, incident_type, reason)
