"""Detección de incidentes violentos en video con verificación por IA local.

El proyecto implementa un pipeline de dos etapas:

1. **Sospecha** (rápida): YOLO + heurísticas marcan posibles incidentes.
2. **Confirmación** (lenta): un modelo de visión local (Ollama) decide si la
   sospecha es violencia real. Solo una confirmación positiva genera alarma y
   envía una alerta a la central REST.

La organización sigue arquitectura hexagonal: ``domain``, ``application``,
``infrastructure`` y ``presentation``.
"""

__version__ = "1.0.0"
