"""Deterministic, inspectable lexical matching; no model calls or editing."""

import re
import unicodedata

from schemas.script import SceneRequirement
from schemas.source_analysis import SourceAnalysis

SCORING_VERSION = 'lexical-v1'
WEIGHTS = {'characters': .20, 'action': .20, 'emotion': .10, 'location': .10,
           'shot_size': .10, 'visual_quality': .10, 'continuity': .10, 'usable_duration': .10}
STOP_WORDS = {'a', 'an', 'the', 'is', 'are', 'was', 'were', 'to', 'of', 'in', 'at', 'on', 'and', 'with'}
NEGATION = {'no', 'not', 'never', 'without', 'không', 'chưa', 'chẳng'}
SHOT_GROUPS = [
    {'extreme wide', 'extreme wide shot', 'ews', 'establishing'},
    {'wide', 'wide shot', 'ws', 'long shot', 'full shot'},
    {'medium wide', 'medium wide shot', 'mws', 'cowboy'},
    {'medium', 'medium shot', 'ms'},
    {'medium close up', 'medium close up shot', 'mcu'},
    {'close up', 'close up shot', 'cu'},
    {'extreme close up', 'extreme close up shot', 'ecu'},
]
POSITIVE_QUALITY = {'sharp', 'clear', 'clean', 'excellent', 'good', 'crisp'}
NEGATIVE_QUALITY = {'blur', 'blurry', 'blurred', 'flicker', 'flickering', 'artifact', 'artifacts',
                    'distorted', 'distortion', 'noisy', 'overexposed', 'underexposed', 'poor', 'unusable'}


def normalize(text: str) -> str:
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFC', text).casefold(), re.UNICODE))


def tokens(text: str) -> set[str]:
    return set(normalize(text).split()) - STOP_WORDS


def text_score(required: str, observed: str | None) -> tuple[float, dict]:
    requested = tokens(required)
    available = tokens(observed or '')
    matched = requested & available
    polarity_conflict = bool(requested & NEGATION) != bool(available & NEGATION)
    score = len(matched) / len(requested) if requested else float(normalize(required) == normalize(observed or ''))
    if polarity_conflict:
        score = 0.0
    return score, {'matched_tokens': sorted(matched), 'missing_tokens': sorted(requested - available),
                   'negation_mismatch': polarity_conflict,
                   'rule': 'required-token coverage; negation mismatch forces zero'}


def score_source(scene: SceneRequirement, source: SourceAnalysis) -> dict:
    """Eight weighted components. Missing scene preferences are excluded.

    Missing source evidence earns zero. Scores measure rule-based suitability,
    not a probability of correctness or proof of semantic equivalence.
    """
    components = {}

    def add(name, score, active, required, observed, reason):
        components[name] = {'score': score, 'weight': WEIGHTS[name], 'active': active,
                            'required': required, 'observed': observed, 'explanation': reason}

    required_names = {normalize(name) for name in scene.characters if normalize(name)}
    source_names = {normalize(name) for name in source.characters if normalize(name)}
    add('characters', len(required_names & source_names) / len(required_names) if required_names else 0,
        bool(required_names), scene.characters, source.characters,
        {'rule': 'exact normalized character-name coverage; extra characters do not reduce score',
         'matched': sorted(required_names & source_names), 'missing': sorted(required_names - source_names)})
    for name in ('action', 'emotion', 'location'):
        required, observed = getattr(scene, name), getattr(source, name)
        value, reason = text_score(required or '', observed)
        add(name, value if required else 0, bool(required and required.strip()), required, observed, reason)

    preferred = normalize(scene.preferred_shot_size or '')
    observed = normalize(source.shot_size or '')
    preferred_group = next((i for i, aliases in enumerate(SHOT_GROUPS) if preferred in aliases), None)
    source_group = next((i for i, aliases in enumerate(SHOT_GROUPS) if observed in aliases), None)
    shot_score = float(bool(preferred) and preferred == observed)
    if preferred_group is not None and source_group is not None:
        shot_score = 1.0 if preferred_group == source_group else .5 if abs(preferred_group - source_group) == 1 else 0.0
    add('shot_size', shot_score, bool(preferred), scene.preferred_shot_size, source.shot_size,
        {'rule': 'same alias group = 1; neighboring size = 0.5; otherwise 0; unknown labels require exact match'})

    quality_tokens = tokens(source.visual_quality or '')
    positive = sorted(quality_tokens & POSITIVE_QUALITY)
    negative = sorted(quality_tokens & NEGATIVE_QUALITY)
    # Negation in quality prose is deliberately conservative: no unsupported
    # claim of good quality from phrases such as "not sharp" or "no blur".
    negated = bool(quality_tokens & NEGATION)
    baseline = 0.0 if not quality_tokens else .25 if negative or negated else 1.0 if positive else .5
    penalty = min(.5, .1 * len({normalize(p) for p in source.problems if normalize(p)}))
    add('visual_quality', max(0.0, baseline - penalty), True, 'usable visual quality', source.visual_quality,
        {'rule': 'missing=0; negative/negated=.25; positive=1; unrecognized=.5; subtract .1 per distinct reported problem, capped at .5',
         'positive_keywords': positive, 'negative_keywords': negative, 'negation_present': negated,
         'baseline': baseline, 'problem_penalty': penalty, 'problems': source.problems})

    checks = []
    for requirement in scene.continuity_requirements:
        if not requirement.strip():
            continue
        comparisons = [(text_score(requirement, note)[0], note) for note in source.continuity_notes]
        best, note = max(comparisons, key=lambda item: item[0]) if comparisons else (0.0, None)
        checks.append({'requirement': requirement, 'best_note': note, 'score': best,
                       **text_score(requirement, note)[1]})
    add('continuity', sum(item['score'] for item in checks) / len(checks) if checks else 0,
        bool(checks), scene.continuity_requirements, source.continuity_notes,
        {'rule': 'mean of best note token coverage per requirement; no cross-scene continuity inference', 'checks': checks})
    available = source.usable_end - source.usable_start if source.usable_start is not None else None
    add('usable_duration', min(available / scene.estimated_duration, 1.0) if available is not None else 0,
        True, scene.estimated_duration, available,
        {'rule': 'min(usable seconds / estimated scene seconds, 1); unknown interval = 0',
         'usable_start': source.usable_start, 'usable_end': source.usable_end,
         'shortfall_seconds': max(0.0, scene.estimated_duration - available) if available is not None else None})
    denominator = sum(item['weight'] for item in components.values() if item['active'])
    total = 0.0
    for component in components.values():
        contribution = component['score'] * component['weight'] / denominator if component['active'] else 0.0
        total += contribution
        component['contribution'] = contribution
    return {'source_id': source.source_id, 'score': min(1.0, max(0.0, total)), 'active_weight_total': denominator,
            'components': components}


def ranking_policy() -> dict:
    return {'version': SCORING_VERSION, 'weights': dict(WEIGHTS),
            'formula': 'sum(active weight * component score) / sum(active weights)',
            'tie_break': 'source_id ascending',
            'limitations': 'Lexical evidence only: no synonyms, translation, coreference, or semantic contradiction reasoning. Scores are not probabilities. Review candidates before editing.'}
