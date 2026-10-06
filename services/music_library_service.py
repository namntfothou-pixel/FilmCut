"""Tagged existing local music catalog; the schema matches the SFX catalog."""
from pathlib import Path
from schemas.sound import MusicLibrary
from services.analysis_service import AnalysisError

MUSIC_LIBRARY_ROOT = Path(__file__).resolve().parents[1] / 'assets' / 'music'


def list_music_library(*, library_root=None):
    try:
        root = Path(library_root or MUSIC_LIBRARY_ROOT).resolve(strict=True)
        catalog = MusicLibrary.model_validate_json((root / 'library.json').read_text(encoding='utf-8'))
        items, errors = [], []
        for entry in sorted(catalog.items, key=lambda item: item.id):
            path = (root / entry.file).resolve()
            if not path.is_relative_to(root):
                raise ValueError('Music catalog file escapes library')
            items.append({**entry.model_dump(mode='json'), 'file': str(path), 'available': path.is_file()})
            if not path.is_file():
                errors.append({'code': 'missing_music_asset', 'message': f'Missing music: {entry.file}'})
        return {'library_root': str(root), 'items': items, 'errors': errors}
    except (OSError, ValueError, RuntimeError) as exc:
        raise AnalysisError('invalid_music_library', str(exc)) from exc
