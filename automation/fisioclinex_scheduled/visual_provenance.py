"""Read-only validation of the split operational package layout."""
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil

from publication_package import PLANNED_STORY_VERSIONS, PublicationPackageError, validate_publication_package
from .fingerprint import fingerprint_package

VISUAL_MANIFEST = 'publication-manifest.json'


def requires_visual_manifest(manifest):
    # S4 planning is emitted for visual packages with a planned Story. Historical optional
    # S5 Story state does not retroactively acquire this requirement.
    return manifest.story_plan_version == 1


def validate_visual_provenance(root, manifest):
    root = Path(root)
    posts = root/'posts'/manifest.slug
    visual = posts/VISUAL_MANIFEST
    required = requires_visual_manifest(manifest)
    if not visual.exists():
        if required:
            raise PublicationPackageError('required visual publication manifest missing')
        return None
    if posts.is_symlink() or visual.is_symlink():
        raise PublicationPackageError('unsafe visual publication manifest')
    with TemporaryDirectory(prefix='visual-provenance-') as temporary:
        package = Path(temporary)/manifest.slug
        package.mkdir()
        for path in posts.iterdir():
            if path.is_symlink() or not path.is_file():
                raise PublicationPackageError('unsafe remote package asset')
            shutil.copyfile(path, package/path.name)
        caption = root/'publication-state/queue'/manifest.slug/manifest.caption_file
        if caption.is_symlink():
            raise PublicationPackageError('unsafe remote caption')
        shutil.copyfile(caption, package/'legenda.txt')
        validated = validate_publication_package(package)
        if required and validated.manifest["schema_version"] not in PLANNED_STORY_VERSIONS:
            raise PublicationPackageError("planned Story requires a compatible visual manifest")
        names = ['legenda.txt', *(p.name for p in validated.slides), validated.story.name]
        if (len(validated.slides) != manifest.slides_count or
                fingerprint_package(package, names) != manifest.package_sha256):
            raise PublicationPackageError('visual/operational package fingerprint mismatch')
        return validated.manifest
