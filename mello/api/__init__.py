"""
Mello API modules - External service integrations.
"""
from .player import NullPlayerAPI, PlayerAPIProtocol
from .soloist import SoloistAPI
from .catalog import CatalogManager

__all__ = ['NullPlayerAPI', 'PlayerAPIProtocol', 'SoloistAPI', 'CatalogManager']

