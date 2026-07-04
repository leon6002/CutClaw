"""Prompt templates for asset annotation and selection."""

# ── Video annotation ───────────────────────────────────────────────────────

VIDEO_ANNOTATION_SYSTEM = (
    "You are a professional video asset curator for a video editing pipeline. "
    "Analyze the key frames provided and produce a structured catalog entry "
    "that captures the visual content, mood, and suitability for editing."
)

VIDEO_ANNOTATION_PROMPT = """You are viewing {frame_count} key frames evenly sampled from a video clip.

**Task**: Provide a structured catalog entry for this video asset. Output ONLY valid JSON.

**JSON Schema**:
{{
  "summary": "Concise description in 1-2 sentences (e.g. 'DJI drone footage of expansive grasslands at golden hour with slow forward movement')",
  "tags": ["tag1", "tag2", ...],
  "emotion": "Primary emotional tone (e.g. 'serene', 'energetic', 'nostalgic', 'awe-inspiring')",
  "quality_score": <float 0-10, where 10 is cinema-grade and 5 is average phone footage>,
  "visual_tags": ["aerial", "wide_shot", "handheld", "static", "slow_pan", "timelapse", ...],
  "key_colors": ["#HEX", ...],  // 2-5 dominant hex colors
  "scene_types": ["establishing_shot", "landscape", "detail", "action", "portrait", "transition", ...],
  "suggested_use": "How this clip could be used (e.g. 'opening establishing shot for travel montage', 'B-roll for transitions')",
  "has_people": <true/false>,
  "people_description": "<empty string if no people, otherwise brief description>",
  "camera_movement": "e.g. 'slow pan left', 'static tripod', 'handheld walking', 'drone ascending'",
  "time_of_day": "e.g. 'golden_hour', 'daylight', 'sunset', 'night', 'overcast', 'indoor'",
  "duration_summary_sec": 0.0
}}

STRICT rules:
- tags: 5-10 concise lowercase tags
- quality_score: be honest — blurry/shaky footage gets 2-4, well-shot footage gets 7-9
- Only return valid JSON, no markdown, no extra text.
"""

# ── Image annotation ───────────────────────────────────────────────────────

IMAGE_ANNOTATION_SYSTEM = (
    "You are a professional photo curator for a video editing pipeline. "
    "Analyze the image and produce a structured catalog entry."
)

IMAGE_ANNOTATION_PROMPT = """You are viewing a single still image.

**Task**: Provide a structured catalog entry. Output ONLY valid JSON.

**JSON Schema**:
{{
  "summary": "Concise description in 1-2 sentences",
  "tags": ["tag1", "tag2", ...],
  "emotion": "Primary emotional tone",
  "quality_score": <float 0-10>,
  "visual_tags": ["landscape", "portrait", "macro", "silhouette", "long_exposure", "HDR", ...],
  "key_colors": ["#HEX", ...],
  "suggested_use": "How this photo could be used in a video montage",
  "has_people": <true/false>,
  "people_description": "<empty string if no people>",
  "composition": "e.g. 'rule_of_thirds', 'centered', 'leading_lines', 'symmetrical', 'negative_space'"
}}

STRICT rules:
- quality_score: 10 = professional photography, 5 = average snapshot, 1 = unusable
- Only return valid JSON, no extra text.
"""

# ── Audio annotation ───────────────────────────────────────────────────────

AUDIO_ANNOTATION_SYSTEM = (
    "You are a professional music analyst. Based on the structured audio analysis "
    "provided below, produce a high-level catalog entry for this music track."
)

AUDIO_ANNOTATION_PROMPT = """Below is a structured analysis of a music track produced by automatic beat/energy/emotion detection.

**Audio Analysis**:
{audio_analysis_json}

**Task**: Distill this into a concise catalog entry. Output ONLY valid JSON.

**JSON Schema**:
{{
  "summary": "Overall genre/mood description in 1-2 sentences",
  "genre": "Inferred genre (e.g. 'cinematic orchestral', 'electronic pop', 'acoustic folk')",
  "emotion": "Dominant emotional tone",
  "energy_level": "<high | medium | low | building | varied>",
  "bpm": <float>,
  "sections_summary": "Brief summary of track structure (e.g. 'Intro 0-20s, Verse 20-50s, Chorus 50-80s, Outro 80-100s')",
  "tags": ["tag1", "tag2", ...],
  "quality_score": <float 0-10>,
  "suggested_use": "How to use this track (e.g. 'climax montage', 'calm intro', 'transition background')",
  "duration_sec": <float>
}}

STRICT rules:
- Only return valid JSON, no extra text.
"""

# ── Asset selector ─────────────────────────────────────────────────────────

SELECTOR_SYSTEM = (
    "You are an expert video editor and creative director. "
    "Your job is to select the best combination of media assets for a video montage "
    "based on the user's creative brief."
)

SELECTOR_PROMPT = """**User's Creative Brief**:
{instruction}

**Available Assets** (one per line, format: [type] | filename | quality:N/10 | summary):
{asset_summaries}

**Selection Guidelines**:
1. **Relevance**: Assets must match the creative brief's theme, mood, and content.
2. **Quality**: Prefer higher quality scores, but don't ignore lower-scored assets if they are thematically perfect.
3. **Diversity**: Pick a mix — different angles, scales, camera movements. Avoid 3 nearly identical drone shots.
4. **Duration**: Select enough video content to fill the target duration. Each video clip provides ~10-30s of usable footage.
5. **Images**: Include 1-5 strong photos that complement the videos (for variety in the montage).
6. **Audio**: Pick ONE track whose energy, emotion, and genre best fit the brief.

**Target**: {target_duration_sec}s total output.

**Output**: Return ONLY valid JSON matching this schema:
{{
  "selected_videos": ["relative/path/video1.mp4", ...],
  "selected_images": ["relative/path/photo1.jpg", ...],
  "selected_audio": ["relative/path/music.mp3"],
  "rationale": "Brief explanation of choices (2-3 sentences)",
  "alternative_videos": ["path/to/runner_up.mp4", ...],
  "alternative_audio": ["path/to/runner_up.mp3"],
  "target_duration_sec": <float>,
  "narrative_idea": "Creative vision for the montage (1-2 sentences)"
}}

STRICT rules:
- Use EXACT file paths from the available assets list.
- selected_videos: 3-5 videos.
- selected_images: 0-5 images.
- selected_audio: exactly 1 audio track.
- Only return valid JSON, no extra text.
"""
