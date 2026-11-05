"""Explicit, expiring cover-only trial; approved packages remain immutable."""
import hashlib
import json
import re
from datetime import datetime

from .queue_pages import official_slide_url, verify_slide_paths, verify_story_path


def verified_story_source(root, manifest, *, now, fetcher):
    """Keep the same Story child identity and all publication/retry fences.

    A trial changes only its outgoing source URL after full package validation.
    Missing trial means the standard asset. Invalid/expired matching trial fails
    closed, never silently falling back to a different image on a retry.
    """
    config = root / 'publication-state/story-cover-trial.json'
    if config.is_symlink():
        raise ValueError('unsafe Story trial config')
    trial = None
    if config.exists():
        trial = json.loads(config.read_bytes())
        fields = {'schema_version', 'mode', 'slug', 'feed_publication_key',
                  'story_publication_key', 'source_cover_sha256', 'authorized_at', 'expires_at'}
        if not isinstance(trial, dict) or set(trial) != fields:
            raise ValueError('invalid Story trial fields')
        if type(trial['schema_version']) is not int or trial['schema_version'] != 1 or trial['mode'] != 'original-cover':
            raise ValueError('invalid Story trial mode')
        for field in fields - {'schema_version'}:
            if not isinstance(trial[field], str) or not trial[field]:
                raise ValueError('invalid Story trial value')
        if not re.fullmatch(r'[0-9a-f]{64}', trial['source_cover_sha256']):
            raise ValueError('invalid Story trial hash')
        authorized = datetime.fromisoformat(trial['authorized_at'].replace('Z', '+00:00'))
        expires = datetime.fromisoformat(trial['expires_at'].replace('Z', '+00:00'))
        if any(t.tzinfo is None or t.utcoffset() is None for t in (now, authorized, expires)) or expires <= authorized:
            raise ValueError('invalid Story trial interval')
        if manifest.slug == trial['slug']:
            if not authorized <= now < expires:
                raise ValueError('Story trial outside authorized interval')
            if (manifest.publication_key != trial['feed_publication_key'] or
                    manifest.story.publication_key != trial['story_publication_key']):
                raise ValueError('Story trial identity differs')
            cover = root / 'posts' / manifest.slug / f'{manifest.slug}-slide-01.png'
            if cover.is_symlink() or not cover.is_file() or hashlib.sha256(cover.read_bytes()).hexdigest() != trial['source_cover_sha256']:
                raise ValueError('Story trial cover differs')
            verify_slide_paths(manifest.slug, (cover,), fetcher=fetcher, max_wait_seconds=0)
            print(json.dumps({'story_asset_mode': 'original-cover-trial', 'slug': manifest.slug,
                              'source_cover_sha256': trial['source_cover_sha256']}))
            return official_slide_url(manifest.slug, cover.name)
    return verify_story_path(manifest.slug, root / 'posts' / manifest.slug / f'{manifest.slug}-story.png',
                             fetcher=fetcher)
