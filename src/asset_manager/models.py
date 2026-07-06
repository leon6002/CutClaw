"""Data models for the Asset Manager.

All models use Pydantic for type safety and JSON schema generation,
consistent with the rest of the CutClaw codebase.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Base metadata ──────────────────────────────────────────────────────────

class AssetMetadata(BaseModel):
    """File-level metadata present for every asset."""
    file_path: str = Field(description="Relative path from asset root (e.g. 'DJI_001.mp4')")
    absolute_path: str = Field(description="Full absolute path on disk")
    file_name: str = Field(description="Original file name")
    content_hash: str = Field(description="SHA-256 of first 1 MB of the file")
    file_size_bytes: int = Field(description="File size in bytes", ge=0)
    file_size_mb: float = Field(description="File size in MB", ge=0.0)
    mime_type: str = Field(description="MIME type, e.g. 'video/mp4'")


class VideoAssetMetadata(AssetMetadata):
    """Video-specific metadata extracted via ffprobe."""
    asset_type: Literal["video"] = "video"
    duration_sec: float = Field(description="Duration in seconds", ge=0.0)
    width: int = Field(description="Frame width in pixels", ge=0)
    height: int = Field(description="Frame height in pixels", ge=0)
    fps: float = Field(description="Average frames per second", ge=0.0)
    codec: str = Field(default="", description="Video codec name")
    has_audio: bool = Field(default=True, description="Whether the video contains an audio track")


class ImageAssetMetadata(AssetMetadata):
    """Image-specific metadata extracted via PIL."""
    asset_type: Literal["image"] = "image"
    width: int = Field(description="Image width in pixels", ge=0)
    height: int = Field(description="Image height in pixels", ge=0)
    format: str = Field(default="", description="Image format, e.g. 'JPEG'")


class AudioAssetMetadata(AssetMetadata):
    """Audio-specific metadata extracted via soundfile / ffprobe."""
    asset_type: Literal["audio"] = "audio"
    duration_sec: float = Field(description="Duration in seconds", ge=0.0)
    sample_rate: int = Field(default=0, description="Sample rate in Hz")
    channels: int = Field(default=0, description="Number of audio channels")


AssetMetadataUnion = VideoAssetMetadata | ImageAssetMetadata | AudioAssetMetadata


# ── AI annotations ─────────────────────────────────────────────────────────

class VideoAnnotation(BaseModel):
    """Compact summary produced by VLM analysis of key frames."""
    summary: str = Field(description="Concise description in 1-2 sentences")
    tags: list[str] = Field(default_factory=list, description="Content tags, e.g. ['drone', 'landscape', 'golden_hour']")
    emotion: str = Field(default="", description="Primary emotional tone, e.g. 'serene, awe-inspiring'")
    quality_score: float = Field(default=5.0, description="Visual quality score 0-10", ge=0.0, le=10.0)
    visual_tags: list[str] = Field(default_factory=list, description="Visual style tags: ['aerial', 'wide_shot', 'slow_pan']")
    key_colors: list[str] = Field(default_factory=list, description="Dominant hex colors, e.g. ['#87CEEB', '#228B22']")
    scene_types: list[str] = Field(default_factory=list, description="e.g. ['establishing_shot', 'landscape', 'detail']")
    suggested_use: str = Field(default="", description="How the clip could be used, e.g. 'B-roll for travel montage'")
    has_people: bool = Field(default=False)
    people_description: str = Field(default="")
    camera_movement: str = Field(default="", description="e.g. 'slow pan left', 'static', 'tracking forward'")
    time_of_day: str = Field(default="", description="e.g. 'sunset', 'daylight', 'night', 'golden_hour'")
    duration_summary_sec: float = Field(default=0.0, description="Relevant duration for cataloging")


class ImageAnnotation(BaseModel):
    """Single-shot VLM description of a still image."""
    summary: str = Field(description="Concise description in 1-2 sentences")
    tags: list[str] = Field(default_factory=list)
    emotion: str = Field(default="")
    quality_score: float = Field(default=5.0, ge=0.0, le=10.0)
    visual_tags: list[str] = Field(default_factory=list)
    key_colors: list[str] = Field(default_factory=list)
    suggested_use: str = Field(default="")
    has_people: bool = Field(default=False)
    people_description: str = Field(default="")
    composition: str = Field(default="", description="e.g. 'rule_of_thirds', 'centered', 'leading_lines'")


class AudioAnnotation(BaseModel):
    """High-level summary distilled from the Madmom audio analysis pipeline."""
    summary: str = Field(description="Overall genre/mood description")
    genre: str = Field(default="", description="Inferred genre")
    emotion: str = Field(default="", description="Dominant emotional tone")
    energy_level: str = Field(default="medium", description="'high', 'medium', 'low', or 'building'")
    bpm: float = Field(default=0.0, description="Estimated BPM")
    sections_summary: str = Field(default="", description="MEASURED section list 'Name start-end, …' (drives the UI timeline; never LLM-written)")
    structure_notes: str = Field(default="", description="LLM prose narrative of the musical journey (complements the measured list)")
    sections_detail: list = Field(default_factory=list, description="Per-section detail [{name,start,end,instruments:[…]}] — instruments aggregated from sub-segment captions")
    tags: list[str] = Field(default_factory=list)
    quality_score: float = Field(default=5.0, ge=0.0, le=10.0, description="Subjective quality 0-10")
    suggested_use: str = Field(default="", description="e.g. 'climax sequence', 'intro build-up', 'montage'")
    duration_sec: float = Field(default=0.0)


# ── Annotation envelope ────────────────────────────────────────────────────

class AssetAnnotation(BaseModel):
    """Top-level annotation stored in the index."""
    content_hash: str = Field(description="Primary key — SHA-256 of first 1 MB")
    file_path: str = Field(description="Relative path at annotation time (file may have moved)")
    asset_type: Literal["video", "image", "audio"]
    metadata: AssetMetadataUnion
    annotation: VideoAnnotation | ImageAnnotation | AudioAnnotation
    annotated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    model_used: str = Field(default="")
    model_endpoint: str = Field(default="")


# ── Selection result ───────────────────────────────────────────────────────

class AssetSelection(BaseModel):
    """Output of the selector Agent — the best asset combination."""
    instruction: str = Field(description="User instruction used for selection")
    selected_videos: list[str] = Field(default_factory=list, description="Selected video file_paths")
    selected_images: list[str] = Field(default_factory=list, description="Selected image file_paths")
    selected_audio: list[str] = Field(default_factory=list, description="Selected audio file_paths")
    rationale: str = Field(default="", description="Why these assets were chosen")
    alternative_videos: list[str] = Field(default_factory=list, description="Runner-up video suggestions")
    alternative_audio: list[str] = Field(default_factory=list, description="Runner-up audio suggestions")
    target_duration_sec: float = Field(default=30.0, description="Suggested total output duration")
    narrative_idea: str = Field(default="", description="Suggested narrative / editing idea for the montage")
