"""Caption quality checks shared by the audio captioner and the analysis cache.

Kept dependency-free (stdlib only) so the cache layer can import it without
pulling in litellm / soundfile / numpy.
"""

from typing import Dict, List

# Phrases a model emits when it answered the prompt without actually receiving
# the audio attachment (e.g. a text-only model configured as AUDIO_LITELLM_MODEL).
# Matched case-insensitively as substrings.
NO_AUDIO_MARKERS = (
    "audio segment not provided",
    "no audio segment",
    "audio not provided",
    "audio was not provided",
    "without audio content",
    "no audio was provided",
    "attach the audio",
    "cannot hear the audio",
    "need the actual audio",
)


def caption_reports_missing_audio(text: str) -> bool:
    """True if the response text claims the audio was never received."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in NO_AUDIO_MARKERS)


def find_poisoned_subsegments(captions_data: Dict) -> List[str]:
    """Scan a captions.json payload for baked-in failure placeholders.

    Returns human-readable reasons (empty list = cache looks healthy).
    Written before the fail-fast contract was enforced in the captioner,
    old caches may contain empty descriptions or "no audio" refusal text.
    """
    problems: List[str] = []
    for si, section in enumerate(captions_data.get("sections", []) or []):
        if not isinstance(section, dict):
            continue
        subs = (section.get("detailed_analysis") or {}).get("sections", []) or []
        for bi, sub in enumerate(subs):
            if not isinstance(sub, dict):
                continue
            desc = str(sub.get("description", "") or "")
            if not desc.strip():
                problems.append(f"section {si + 1} sub {bi + 1}: empty description")
            elif caption_reports_missing_audio(desc):
                problems.append(f"section {si + 1} sub {bi + 1}: 'no audio' placeholder text")
    return problems
