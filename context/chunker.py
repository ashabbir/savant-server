"""Content chunking for efficient indexing."""

from typing import List, Tuple


class ContentChunker:
    """Split file content into searchable chunks."""

    DEFAULT_CHUNK_SIZE = 500     # lines per chunk
    DEFAULT_OVERLAP = 50         # overlap lines between chunks

    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP):
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk(self, content: str) -> List[str]:
        if not content or not content.strip():
            return []

        lines = content.split("\n")
        chunks = []
        chunk_index = 0

        while chunk_index * (self.chunk_size - self.overlap) < len(lines):
            start = max(0, chunk_index * (self.chunk_size - self.overlap))
            end = min(len(lines), start + self.chunk_size)

            chunk_text = "\n".join(lines[start:end])
            if chunk_text.strip():
                chunks.append(chunk_text)

            chunk_index += 1
            if end >= len(lines):
                break

        return chunks

    def chunk_with_metadata(self, content: str) -> List[Tuple[int, str]]:
        """Return list of (chunk_index, chunk_content) tuples."""
        return list(enumerate(self.chunk(content)))
