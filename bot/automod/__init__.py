"""Automated moderation package (Phase 4).

Pipeline: Discord message event -> normalization -> exemption checks ->
detectors -> moderation decision -> enforcement policy -> case service ->
audit logging. Detection and enforcement are strictly separated: detectors
return :class:`bot.automod.detectors.Detection` objects and never touch
Discord; the engine's enforcement policy performs the actual actions through
the existing moderation service.
"""
