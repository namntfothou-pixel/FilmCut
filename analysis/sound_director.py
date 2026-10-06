"""Offline sound-intent rules; providers may supply structured intents instead."""

from analysis.matching import normalize, tokens
from schemas.sound import MusicIntent, SFXIntent, Ducking

MOODS = [
    ({'anxious', 'fear', 'scared', 'tension', 'afraid', 'lo', 'lắng'}, 'tense', .6, ['tense', 'suspense']),
    ({'angry', 'anger', 'fight', 'furious', 'giận'}, 'aggressive', .85, ['action', 'intense']),
    ({'sad', 'sadness', 'grief', 'buồn'}, 'sad', .25, ['sad', 'reflective']),
    ({'happy', 'joy', 'relieved', 'vui'}, 'hopeful', .4, ['hopeful', 'warm']),
]


def music_intent(scene, analysis, clip):
    words = tokens(' '.join(filter(None, [scene.emotion, analysis.emotion])))
    mood, energy, tags = next(((m, e, t) for markers, m, e, t in MOODS if markers & words),
                             ('neutral', .3, ['ambient', 'neutral']))
    duration = clip.duration
    fade = min(.25, duration / 2)
    return MusicIntent(mood=mood, energy=energy, start=clip.timeline_start, end=clip.timeline_end,
        recommended_tags=tags, ducking=Ducking(enabled=bool(scene.dialogue or analysis.dialogue)),
        fade_in=fade, fade_out=fade)


def sfx_intents(scene, analysis, clip):
    # Action prose cannot locate an impact within a shot. Approximate onset is
    # reported explicitly; use externally supplied exact intents for sync work.
    text = normalize(analysis.action or scene.action or '')
    words = set(text.split())
    events = []
    if ('body' in words and words & {'hits', 'hit', 'impact'} and 'wall' in words):
        tags = ['body', 'impact', 'wall', 'heavy']
        if 'concrete' in words:
            tags.append('concrete')
        events.append(('body hits concrete wall' if 'concrete' in words else 'body hits wall', tags, .9))
    if 'door' in words and words & {'opens', 'open', 'closes', 'close'}:
        closing = bool(words & {'closes', 'close'})
        events.append(('door closes' if closing else 'door opens', ['door', 'close' if closing else 'open'], .4))
    if words & {'footsteps', 'walking', 'walks'}:
        events.append(('footsteps', ['footsteps'], .3))
    if 'glass' in words and words & {'breaks', 'break', 'shatters'}:
        events.append(('glass breaks', ['glass', 'break'], .8))
    return [SFXIntent(event=event, timestamp=clip.timeline_start, tags=tags, intensity=intensity,
                      timing='approximate') for event, tags, intensity in events]
