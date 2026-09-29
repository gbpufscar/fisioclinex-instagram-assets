"""Canonical institutional content-count policy for every FisioClinEx workflow."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Literal

CONTENT_POLICY_VERSION = "editorial-v3-1-to-10"

SINGLE_POST_SLIDES = 1
CAROUSEL_MIN_SLIDES = 2
CAROUSEL_MAX_SLIDES = 10
RECOMMENDED_CAROUSEL_SLIDES: tuple[int, ...] = ()

ACTIVE_ARTIFACT_STATUS = "active"

ERROR_TOO_MANY = (
    "Novo carrossel excede o limite institucional de 10 slides. Reduza o escopo, "
    "mova detalhes para a legenda ou divida o conteúdo em uma série."
)
ERROR_ZERO = "O conteúdo deve possuir pelo menos uma imagem."
ERROR_POLICY_VERSION = "Conteúdo novo deve declarar a política editorial-v3-1-to-10."
ERROR_LOCAL_VALIDATION_ONLY = (
    "Pacote autorizado somente para validação local. Publicação, fila, staging, Git e Meta são proibidos."
)

ContentFormat = Literal["single_post", "carousel"]


class ContentPolicyError(ValueError):
    pass


def reject_local_validation_package(folder: str | Path) -> None:
    """Fail closed when a package is explicitly marked as local-validation-only."""
    root = Path(folder)
    candidates = (
        root / "SYSTEM-VALIDATION-STATUS.json",
        root / "local-validation-content-authorization.json",
        root / "approvals/local-validation-content-authorization.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContentPolicyError(ERROR_LOCAL_VALIDATION_ONLY) from exc
        if (
            data.get("authorization_type") == "local_system_validation_only"
            or data.get("package_type") == "local_system_validation"
            or data.get("production_package") is False
        ):
            raise ContentPolicyError(ERROR_LOCAL_VALIDATION_ONLY)


@dataclass(frozen=True, slots=True)
class SlideCountDecision:
    count: int
    content_format: ContentFormat
    policy_version: str
    artifact_status: str
    executable: bool


def classify_content_format(count: int) -> ContentFormat:
    if not isinstance(count, int) or isinstance(count, bool):
        raise ContentPolicyError("A quantidade de imagens deve ser um número inteiro.")
    if count == 0:
        raise ContentPolicyError(ERROR_ZERO)
    if count < 0:
        raise ContentPolicyError(ERROR_ZERO)
    if count == SINGLE_POST_SLIDES:
        return "single_post"
    if 2 <= count <= CAROUSEL_MAX_SLIDES:
        return "carousel"
    raise ContentPolicyError(ERROR_TOO_MANY)


def validate_slide_count(
    count: int,
    *,
    policy_version: str | None,
    artifact_status: str | None = ACTIVE_ARTIFACT_STATUS,
) -> SlideCountDecision:
    """Validate the single active 1–10 policy."""
    if policy_version != CONTENT_POLICY_VERSION:
        raise ContentPolicyError(ERROR_POLICY_VERSION)
    if artifact_status != ACTIVE_ARTIFACT_STATUS:
        raise ContentPolicyError("Status de artefato incompatível com produção ativa.")
    return SlideCountDecision(
        count=count,
        content_format=classify_content_format(count),
        policy_version=policy_version,
        artifact_status=ACTIVE_ARTIFACT_STATUS,
        executable=True,
    )


def validate_active_slide_count(count: int) -> SlideCountDecision:
    return validate_slide_count(
        count,
        policy_version=CONTENT_POLICY_VERSION,
        artifact_status=ACTIVE_ARTIFACT_STATUS,
    )


def require_executable_artifact(*, policy_version: str | None, artifact_status: str | None) -> None:
    if policy_version != CONTENT_POLICY_VERSION:
        raise ContentPolicyError(ERROR_POLICY_VERSION)
    if artifact_status != ACTIVE_ARTIFACT_STATUS:
        raise ContentPolicyError("Status de artefato incompatível com produção ativa.")
