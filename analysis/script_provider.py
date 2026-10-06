"""Provider-neutral script contract and an offline, format-based parser."""

import re
from typing import Protocol

from schemas.script import SceneRequirement, ScriptBreakdown


class ScriptAnalysisProvider(Protocol):
    def analyze(self, script: str) -> ScriptBreakdown | dict:
        """Return ScriptBreakdown intent; adapters own model choice and timeouts."""
        ...


class ScriptParseError(ValueError):
    pass


# A requirement is one scene or one explicit shot. No implicit shot subdivision.
HEADING = re.compile(r"^(?:(?:INT\.?/EXT\.?|EXT\.?/INT\.?|INT\.|EXT\.)\s+.+|(?:SCENE|SHOT|CẢNH)\s+\d+\b.*)$", re.I)
SLUG = re.compile(r"^(?:INT\.?/EXT\.?|EXT\.?/INT\.?|INT\.|EXT\.)\s+", re.I)
LABELS = {"characters", "location", "action", "emotion", "dialogue", "preferred_shot_size",
          "shot_size", "continuity", "continuity_requirements", "duration", "estimated_duration", "notes"}


class LocalScriptParser:
    """Extract explicit screenplay/shot-script structure without model inference.

    Unknown semantic fields remain null. Untagged prose is action, not an
    invented interpretation. Duration uses a documented rough reading estimate.
    """

    def analyze(self, script: str) -> ScriptBreakdown:
        blocks = []
        current = None
        preamble = []
        for raw in script.splitlines():
            line = raw.strip()
            if HEADING.fullmatch(line):
                current = (line, [])
                blocks.append(current)
            elif current is not None:
                current[1].append(line)
            elif line:
                preamble.append(line)
        if not blocks:
            raise ScriptParseError("Use INT./EXT., SCENE 1, SHOT 1, or CẢNH 1 headings, or supply a ScriptAnalysisProvider")
        requirements = []
        for number, (heading, lines) in enumerate(blocks, 1):
            fields = {label: [] for label in LABELS}
            actions, dialogue, characters = [], [], []
            speaker = None
            for line in lines:
                if not line:
                    speaker = None
                    continue
                label, separator, value = line.partition(":")
                key = label.lower().strip().replace(" ", "_")
                if separator and key in LABELS:
                    fields[key].append(value.strip())
                    speaker = None
                elif line.upper() in {"CUT TO:", "FADE IN:", "FADE OUT.", "FADE OUT:", "DISSOLVE TO:"}:
                    fields["notes"].append(line)
                    speaker = None
                elif separator and label.isupper() and any(c.isalpha() for c in label):
                    characters.append(label.strip())
                    dialogue.append(f"{label.strip()}: {value.strip()}")
                    speaker = label.strip()
                elif line.isupper() and any(c.isalpha() for c in line) and len(line.split()) <= 4 and not line.endswith((".", "!", "?", ":")):
                    speaker = line
                    characters.append(line)
                elif speaker is not None:
                    if line.startswith("(") and line.endswith(")"):
                        fields["notes"].append(f"{speaker} {line}")
                    else:
                        dialogue.append(f"{speaker}: {line}")
                else:
                    actions.append(line)
            for value in fields["characters"]:
                characters.extend(name.strip() for name in value.split(",") if name.strip())
            location = "\n".join(fields["location"]) or None
            if location is None and SLUG.match(heading):
                location = SLUG.sub("", heading)
            action = "\n".join(actions + fields["action"]) or None
            dialogue_text = "\n".join(dialogue + fields["dialogue"]) or None
            notes = [f"Script heading: {heading}", *fields["notes"]]
            if number == 1 and preamble:
                notes.append("Script preamble: " + "\n".join(preamble))
            durations = fields["duration"] + fields["estimated_duration"]
            if durations:
                if len(durations) != 1:
                    raise ScriptParseError(f"Multiple durations in {heading}")
                match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?", durations[0], re.I)
                if not match:
                    raise ScriptParseError(f"Duration must be positive seconds in {heading}")
                duration = float(match[1])
                if duration <= 0:
                    raise ScriptParseError(f"Duration must be positive seconds in {heading}")
                notes.append("Duration explicitly supplied in script (seconds).")
            else:
                duration = round(max(3.0, len((dialogue_text or "").split()) / 2.5 + len((action or "").split()) / 3), 2)
                notes.append("Rough duration estimate: dialogue words/2.5 + action words/3, minimum 3 seconds; review before editing.")
            requirements.append(SceneRequirement(
                scene_id=f"scene_{number:03d}", story_order=number,
                characters=list(dict.fromkeys(characters)), location=location, action=action,
                emotion="\n".join(fields["emotion"]) or None, dialogue=dialogue_text,
                preferred_shot_size="\n".join(fields["preferred_shot_size"] + fields["shot_size"]) or None,
                continuity_requirements=fields["continuity"] + fields["continuity_requirements"],
                estimated_duration=duration, notes=notes))
        return ScriptBreakdown(requirements=requirements)
