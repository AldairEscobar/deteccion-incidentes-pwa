"""Pruebas del parser del veredicto de Ollama (fail-closed)."""

from __future__ import annotations

from incidentes.infrastructure.verification.ollama_verifier import (
    OllamaViolenceVerifier,
)


def test_parses_plain_json() -> None:
    verdict = OllamaViolenceVerifier.parse_verdict(
        '{"violent": true, "type": "pelea", "confidence": 0.9, "reason": "forcejeo"}'
    )
    assert verdict.violent is True
    assert verdict.type == "PELEA"
    assert verdict.confidence == 0.9
    assert verdict.reason == "forcejeo"


def test_parses_json_inside_markdown_fence() -> None:
    text = '```json\n{"violent": true, "type": "ROBO", "confidence": 0.7}\n```'
    verdict = OllamaViolenceVerifier.parse_verdict(text)
    assert verdict.violent is True
    assert verdict.type == "ROBO"


def test_garbage_is_not_violence() -> None:
    verdict = OllamaViolenceVerifier.parse_verdict("no hay JSON aquí")
    assert verdict.violent is False
    assert verdict.type == "NADA"


def test_confidence_is_clamped() -> None:
    verdict = OllamaViolenceVerifier.parse_verdict(
        '{"violent": true, "type": "PELEA", "confidence": 4.2}'
    )
    assert verdict.confidence == 1.0


def test_unknown_type_falls_back_to_suspicion_type() -> None:
    verdict = OllamaViolenceVerifier.parse_verdict(
        '{"violent": true, "type": "MAGIA", "confidence": 0.5}',
        fallback_type="CAIDA",
    )
    assert verdict.type == "CAIDA"


def test_non_violent_always_reports_nothing() -> None:
    verdict = OllamaViolenceVerifier.parse_verdict(
        '{"violent": false, "type": "PELEA", "confidence": 0.8}'
    )
    assert verdict.violent is False
    assert verdict.type == "NADA"


def test_name_includes_model() -> None:
    verifier = OllamaViolenceVerifier(model="qwen2.5vl:7b")
    assert verifier.name == "ollama/qwen2.5vl:7b"
