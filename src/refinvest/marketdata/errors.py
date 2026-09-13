"""Adapter-independent market-data boundary errors."""


class CalendarResolutionError(ValueError):
    """The immutable snapshot lacks a calendar classification for an AssetSession."""
