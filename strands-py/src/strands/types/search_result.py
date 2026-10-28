"""Search result types modeled after the Bedrock API."""

from typing_extensions import NotRequired, TypedDict

from .citations import CitationsConfig


class SearchResultContentBlock(TypedDict):
    """Text content from a search result.

    Attributes:
        text: The retrieved text.
    """

    text: str


class SearchResultBlock(TypedDict):
    """Retrieved content with source attribution for native citations.

    Attributes:
        source: The source URL or identifier for the content.
        title: A descriptive title for the search result.
        content: The retrieved text blocks.
        citations: Optional configuration for citations.
    """

    source: str
    title: str
    content: list[SearchResultContentBlock]
    citations: NotRequired[CitationsConfig]
