"""
Mello API modules - External service integrations.
"""
from .librespot import LibrespotAPI, NullLibrespotAPI, LibrespotAPIProtocol
from .soloist import SoloistAPI
from .catalog import CatalogManager

__all__ = ['LibrespotAPI', 'NullLibrespotAPI', 'LibrespotAPIProtocol', 'SoloistAPI', 'CatalogManager']

