"""slidedesk: index, browse and search .pptx slide decks."""
from .models import Deck, Slide
from .project import SlideProject

__all__ = ["SlideProject", "Deck", "Slide"]
__version__ = "0.2.0"
