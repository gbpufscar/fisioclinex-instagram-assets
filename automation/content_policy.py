"""Canonical institutional content-count policy for every FisioClinEx workflow."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Literal, Sequence

CONTENT_POLICY_VERSION = "editorial-v3-1-to-10"
COMPATIBLE_POLICY_VERSIONS = {CONTENT_POLICY_VERSION, "editorial-v3-1-or-3-to-10"}
LEGACY_POLICY_VERSION = "legacy-pre-max-5"

SINGLE_POST_SLIDES = 1
CAROUSEL_MIN_SLIDES = 2
CAROUSEL_MAX_SLIDES = 10
RECOMMENDED_CAROUSEL_SLIDES: tuple[int, ...] = ()

ACTIVE_ARTIFACT_STATUS = "active"
LEGACY_READ_ONLY_STATUS = "legacy_read_only"

ERROR_TOO_MANY = (
    "Novo carrossel excede o limite institucional de 10 slides. Reduza o escopo, "
    "mova detalhes para a legenda ou divida o conteúdo em uma série."
)
ERROR_ZERO = "O conteúdo deve possuir pelo menos uma imagem."
ERROR_LEGACY_REUSE = (
    "Artefato histórico disponível somente para leitura. Crie uma nova versão "
    "compatível com a política atual de 1–10 slides."
)
ERROR_POLICY_VERSION = "Conteúdo novo deve declarar a política editorial-v3-1-to-10."
ERROR_LOCAL_VALIDATION_ONLY = (
    "Pacote autorizado somente para validação local. Publicação, fila, staging, Git e Meta são proibidos."
)

ContentFormat = Literal["single_post", "carousel"]
FitsMax5 = Literal[
    "true", "false", "requires_scope_reduction", "requires_caption_expansion", "requires_series"
]


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
    content_format: ContentFormat | Literal["legacy_read_only"]
    policy_version: str
    artifact_status: str
    executable: bool


@dataclass(frozen=True, slots=True)
class EditorialScopeDecision:
    central_question: str
    estimated_slides: int
    fits_max_5_slides: FitsMax5
    caption_expansion: tuple[str, ...]
    may_write_final_script: bool


def classify_content_format(count: int, *, allow_two: bool = True) -> ContentFormat:
    if not isinstance(count, int) or isinstance(count, bool):
        raise ContentPolicyError("A quantidade de imagens deve ser um número inteiro.")
    if count == 0:
        raise ContentPolicyError(ERROR_ZERO)
    if count < 0:
        raise ContentPolicyError(ERROR_ZERO)
    if count == SINGLE_POST_SLIDES:
        return "single_post"
    if count == 2 and not allow_two:
        raise ContentPolicyError(
            "Este artefato usa uma política histórica que não aceita dois arquivos."
        )
    if 2 <= count <= CAROUSEL_MAX_SLIDES:
        return "carousel"
    raise ContentPolicyError(ERROR_TOO_MANY)


def is_legacy_read_only(policy_version: str | None, artifact_status: str | None) -> bool:
    return policy_version == LEGACY_POLICY_VERSION and artifact_status == LEGACY_READ_ONLY_STATUS


def validate_slide_count(
    count: int,
    *,
    policy_version: str | None,
    artifact_status: str | None = ACTIVE_ARTIFACT_STATUS,
    allow_trusted_legacy_read: bool = False,
    trusted_legacy_manifest: str | Path | None = None,
) -> SlideCountDecision:
    """Validate active execution. Legacy access is read-only and must be explicitly trusted.

    Production callers must never pass ``allow_trusted_legacy_read=True``. That switch exists
    only for dedicated historical readers/tests backed by an immutable registry or fixture.
    """
    if is_legacy_read_only(policy_version, artifact_status):
        if allow_trusted_legacy_read and trusted_legacy_manifest is not None and _is_registered_legacy_manifest(trusted_legacy_manifest) and isinstance(count, int) and 1 <= count <= 10:
            return SlideCountDecision(count, "legacy_read_only", policy_version, artifact_status, False)
        raise ContentPolicyError(ERROR_LEGACY_REUSE)
    if policy_version not in COMPATIBLE_POLICY_VERSIONS:
        raise ContentPolicyError(ERROR_POLICY_VERSION)
    if artifact_status != ACTIVE_ARTIFACT_STATUS:
        raise ContentPolicyError("Status de artefato incompatível com produção ativa.")
    return SlideCountDecision(
        count=count,
        content_format=classify_content_format(count,allow_two=policy_version==CONTENT_POLICY_VERSION),
        policy_version=policy_version,
        artifact_status=ACTIVE_ARTIFACT_STATUS,
        executable=True,
    )


def _is_registered_legacy_manifest(manifest_path: str | Path) -> bool:
    """Trust only a repository registry plus exact path and SHA-256, never package flags/slug."""
    repository = Path(__file__).resolve().parent
    registry_path = repository / "config" / "legacy-artifacts.json"
    try:
        candidate = Path(manifest_path).resolve(strict=True)
        candidate.relative_to(repository)
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
    for entry in registry.get("artifacts", []):
        registered = (repository / entry.get("trusted_manifest", "")).resolve()
        if candidate == registered and digest == entry.get("trusted_manifest_sha256"):
            return entry.get("artifact_status") == LEGACY_READ_ONLY_STATUS and not entry.get("execution_allowed", True)
    return False


def validate_active_slide_count(count: int) -> SlideCountDecision:
    return validate_slide_count(
        count,
        policy_version=CONTENT_POLICY_VERSION,
        artifact_status=ACTIVE_ARTIFACT_STATUS,
    )


def require_executable_artifact(*, policy_version: str | None, artifact_status: str | None) -> None:
    if is_legacy_read_only(policy_version, artifact_status):
        raise ContentPolicyError(ERROR_LEGACY_REUSE)
    if policy_version not in COMPATIBLE_POLICY_VERSIONS:
        raise ContentPolicyError(ERROR_POLICY_VERSION)
    if artifact_status != ACTIVE_ARTIFACT_STATUS:
        raise ContentPolicyError("Status de artefato incompatível com produção ativa.")


def validate_editorial_scope(
    *, central_question: str, estimated_slides: int, fits_max_5_slides: FitsMax5,
    caption_expansion: Sequence[str] = (),
) -> EditorialScopeDecision:
    """Gate editorial anterior à copy; nunca comprime ou reescreve texto automaticamente."""
    if not central_question.strip():
        raise ContentPolicyError("O planejamento deve declarar uma pergunta central.")
    if fits_max_5_slides not in {
        "true", "false", "requires_scope_reduction", "requires_caption_expansion", "requires_series"
    }:
        raise ContentPolicyError("Valor inválido para fits_max_5_slides.")
    count_fits = 1 <= estimated_slides <= CAROUSEL_MAX_SLIDES
    may_write = count_fits and fits_max_5_slides in {"true", "requires_caption_expansion"}
    if fits_max_5_slides == "true" and not count_fits:
        raise ContentPolicyError(ERROR_TOO_MANY if estimated_slides > 10 else ERROR_ZERO)
    return EditorialScopeDecision(
        central_question=central_question.strip(), estimated_slides=estimated_slides,
        fits_max_5_slides=fits_max_5_slides, caption_expansion=tuple(caption_expansion),
        may_write_final_script=may_write,
    )
