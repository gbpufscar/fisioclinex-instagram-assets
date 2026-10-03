"""Validation primitives for minimal canonical publication packages."""
from __future__ import annotations
import hashlib, json, re, struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

SLUG_RE = re.compile(r"^fisioclinex-[a-z0-9]+(?:-[a-z0-9]+)*$")
FORBIDDEN_SLUGS = frozenset({"publication-package", "final", "output", "post", "temp"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUIRED_DIMENSIONS = (1080, 1350); REQUIRED_STORY_DIMENSIONS = (1080, 1920)
BUILDER_VERSION = "2.0.0"
CAPTION_HARD_LIMIT = 2200
CAPTION_SAFE_LIMIT = 2000
CAPTION_HASHTAG_LIMIT = 30
HASHTAG_RE = re.compile(r"(?<![\w#])#[\w]+", re.UNICODE)

class PublicationPackageError(ValueError): pass

@dataclass(frozen=True)
class ValidatedPublicationPackage:
    folder: Path; slug: str; slides: tuple[Path, ...]; caption: Path; story: Path; manifest: dict[str, Any]

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()

def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not slug.isascii() or "/" in slug or "\\" in slug or ".." in slug: raise PublicationPackageError("slug inválida ou insegura")
    if slug in FORBIDDEN_SLUGS or slug.removeprefix("fisioclinex-") in FORBIDDEN_SLUGS: raise PublicationPackageError("slug genérica não é permitida")
    if not SLUG_RE.fullmatch(slug): raise PublicationPackageError("slug deve usar o formato fisioclinex-tema-em-minusculas")
    return slug

def canonical_caption_text(text: str) -> str:
    if not isinstance(text, str):
        raise PublicationPackageError("caption_empty: legenda.txt ausente ou vazia")
    # This is the single transport normalization used before counting and sending.
    # Internal spaces, line breaks and Unicode code points remain untouched.
    canonical = text.strip()
    if not canonical:
        raise PublicationPackageError("caption_empty: legenda.txt ausente ou vazia")
    return canonical

def caption_character_count(text: str) -> int:
    return len(canonical_caption_text(text))

def validate_caption_text(text: str) -> str:
    canonical = canonical_caption_text(text)
    current = len(canonical)
    if current > CAPTION_SAFE_LIMIT:
        raise PublicationPackageError(
            "caption_too_long: "
            f"current={current} permitted={CAPTION_SAFE_LIMIT} "
            f"reduction={current - CAPTION_SAFE_LIMIT} hard_limit={CAPTION_HARD_LIMIT}"
        )
    hashtags = len(HASHTAG_RE.findall(canonical))
    if hashtags > CAPTION_HASHTAG_LIMIT:
        raise PublicationPackageError(
            "caption_too_many_hashtags: "
            f"current={hashtags} permitted={CAPTION_HASHTAG_LIMIT}"
        )
    return canonical

def png_dimensions(path: Path) -> tuple[int, int]:
    try: header = path.read_bytes()[:33]
    except OSError as exc: raise PublicationPackageError(f"não foi possível ler {path.name}") from exc
    if len(header) < 33 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR": raise PublicationPackageError(f"PNG inválido: {path.name}")
    return struct.unpack(">II", header[16:24])

def _load_json(path: Path) -> dict[str, Any]:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise PublicationPackageError(f"JSON inválido ou ausente: {path.name}") from exc
    if not isinstance(value, dict): raise PublicationPackageError(f"objeto JSON esperado: {path.name}")
    return value

def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str): return False
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None

def validate_publication_package(folder: str | Path, *, expected_slug: str | None = None) -> ValidatedPublicationPackage:
    root = Path(folder).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink(): raise PublicationPackageError("pasta do pacote é insegura")
    slug = validate_slug(expected_slug) if expected_slug is not None else validate_slug(root.name)
    manifest = _load_json(root / "publication-manifest.json")
    required = {"schema_version", "slug", "post_type", "slide_count", "slides", "story", "caption_file", "approval", "publicable", "files", "created_at", "builder_version"}
    if set(manifest) != required or manifest.get("schema_version") != "2.0" or manifest.get("slug") != slug: raise PublicationPackageError("publication-manifest.json inválido ou divergente")
    if expected_slug is None and root.name != slug: raise PublicationPackageError("basename do pacote diverge da slug")
    count = manifest.get("slide_count")
    if isinstance(count, bool) or not isinstance(count, int) or count not in range(1, 11): raise PublicationPackageError("quantidade de slides deve estar entre 1 e 10")
    names = [f"{slug}-slide-{number:02d}.png" for number in range(1, count + 1)]
    if manifest.get("slides") != names or manifest.get("post_type") != ("single_image" if count == 1 else "carousel"): raise PublicationPackageError("nomes, sequência ou tipo de post divergentes")
    if manifest.get("caption_file") != "legenda.txt": raise PublicationPackageError("nome da legenda canônica divergente")
    approval = manifest.get("approval"); fields = {"status", "copy_sha256", "copy_approved_at", "final_approved_at"}
    if not isinstance(approval, dict) or set(approval) != fields or approval.get("status") != "approved": raise PublicationPackageError("registro de aprovação inválido")
    if not re.fullmatch(r"[0-9a-f]{64}", approval.get("copy_sha256", "")): raise PublicationPackageError("hash da copy inválido")
    if not all(_valid_timestamp(approval.get(key)) for key in ("copy_approved_at", "final_approved_at")): raise PublicationPackageError("datas de aprovação inválidas")
    if manifest.get("publicable") is not True: raise PublicationPackageError("pacote não está publicável")
    if not _valid_timestamp(manifest.get("created_at")) or manifest.get("builder_version") != BUILDER_VERSION: raise PublicationPackageError("metadados do builder inválidos")
    slides = tuple(root / name for name in names)
    for slide in slides:
        if not slide.is_file() or slide.is_symlink() or png_dimensions(slide) != REQUIRED_DIMENSIONS: raise PublicationPackageError(f"slide ausente, inseguro ou com dimensões inválidas: {slide.name}")
    story_name = f"{slug}-story.png"; story_data = manifest.get("story")
    expected_story = {"filename": story_name, "dimensions": [1080, 1920], "source_cover": names[0], "source_cover_sha256": sha256(slides[0]), "adapter_version": "cover-contain-v1", "placement": {"x": 0, "y": 285, "width": 1080, "height": 1350}}
    if story_data != expected_story: raise PublicationPackageError("integridade do contrato do Story é inválida")
    story = root / story_name
    if not story.is_file() or story.is_symlink() or png_dimensions(story) != REQUIRED_STORY_DIMENSIONS: raise PublicationPackageError("asset do Story ausente, inseguro ou com dimensões inválidas")
    caption = root / "legenda.txt"
    if not caption.is_file() or caption.is_symlink(): raise PublicationPackageError("legenda.txt ausente ou vazia")
    validate_caption_text(caption.read_text(encoding="utf-8"))
    expected_files = set(names) | {story_name, "legenda.txt"}
    actual = {item.name for item in root.iterdir() if item.is_file() and not item.is_symlink()}
    if actual != expected_files | {"publication-manifest.json"} or any(item.is_symlink() or not item.is_file() for item in root.iterdir()): raise PublicationPackageError("pacote contém arquivos inesperados ou inseguros")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != len(expected_files) or {item.get("path") for item in records if isinstance(item, dict)} != expected_files: raise PublicationPackageError("inventário de arquivos está incompleto ou divergente")
    roles = {name: "slide" for name in names} | {story_name: "story", "legenda.txt": "caption"}
    for item in records:
        if not isinstance(item, dict) or set(item) != {"path", "role", "size_bytes", "sha256"} or item.get("role") != roles.get(item.get("path")): raise PublicationPackageError("registro de arquivo inválido")
        relative = PurePosixPath(item["path"])
        if len(relative.parts) != 1 or relative.is_absolute() or ".." in relative.parts: raise PublicationPackageError("caminho inseguro no manifesto")
        path = root / item["path"]
        if not path.is_file() or path.is_symlink() or path.stat().st_size != item["size_bytes"] or sha256(path) != item["sha256"]: raise PublicationPackageError(f"integridade divergente: {item['path']}")
    return ValidatedPublicationPackage(root, slug, slides, caption, story, manifest)
