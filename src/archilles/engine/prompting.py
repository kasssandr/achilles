"""Prompt building: system prompt, XML result formatting, Claude prompt
assembly and markdown export. Extracted from the ArchillesRAG monolith (8.16)."""
from datetime import datetime
from html import escape as _html_escape
from typing import Any, Dict, List, Optional

from src.archilles.constants import ChunkType
from src.archilles.i18n import t


class PromptBuilder:
    """Back-reference pattern — see Searcher."""

    def __init__(self, rag):
        self._rag = rag

    def export_to_markdown(
        self,
        results: List[Dict[str, Any]],
        query_text: str,
        output_file: str = None,
        lang: str = "en"
    ) -> str:
        """
        Export search results to Markdown format (optimized for Joplin).

        Args:
            results: Search results from query()
            query_text: Original search query
            output_file: Optional file path (default: auto-generated)
            lang: Operator/interface language for the visible labels
                (``get_languages(...)[0]``); defaults to English.

        Returns:
            Path to the created markdown file
        """
        if not output_file:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            safe_query = "".join(c if c.isalnum() else "_" for c in query_text[:30])
            output_file = f"archilles_search_{safe_query}_{timestamp}.md"

        # Build markdown content
        lines = []

        # Header
        lines.append(f"# {t('export.title', lang)}")
        lines.append(f"")
        lines.append(f"**{t('export.query', lang)}:** `{query_text}`  ")
        lines.append(f"**{t('export.date', lang)}:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
        lines.append(f"**{t('export.results', lang)}:** {len(results)}")
        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

        # Results
        for result in results:
            rank = result['rank']
            similarity = result['similarity']
            metadata = result['metadata']
            text = result['text']

            # Build citation
            book_title = metadata.get('book_title', metadata.get('book_id', 'Unknown'))

            citation_parts = []

            section_citation = self._rag._format_section_citation(metadata)
            if section_citation:
                citation_parts.append(section_citation)

            # Add page info
            page_val, is_pdf, _ = self._rag._resolve_page_info(metadata)
            if page_val:
                page_label = t('page.pdf', lang) if is_pdf else t('page.plain', lang)
                citation_parts.append(f"{page_label} {page_val}")

            # Result header with author and year
            author = metadata.get('author', '')
            year = metadata.get('year', '')

            # Add chunk type indicator
            chunk_type = metadata.get('chunk_type', '')
            type_indicator = ''
            if chunk_type == ChunkType.CALIBRE_COMMENT:
                type_indicator = ' 📝'  # Emoji for markdown
            elif chunk_type == ChunkType.PHASE1_METADATA:
                type_indicator = ' ℹ️'

            if author and year:
                header = f"## [{rank}] {author}: {book_title} ({year}){type_indicator}"
            elif author:
                header = f"## [{rank}] {author}: {book_title}{type_indicator}"
            elif year:
                header = f"## [{rank}] {book_title} ({year}){type_indicator}"
            else:
                header = f"## [{rank}] {book_title}{type_indicator}"

            lines.append(header)

            # Location (section + page)
            if citation_parts:
                lines.append(f"**{t('export.location', lang)}:** {' | '.join(citation_parts)}  ")

            # Relevance
            lines.append(f"**{t('label.relevance', lang)}:** {similarity:.3f}  ")

            # Direct link to PDF/EPUB (file:/// protocol)
            source_file = metadata.get('source_file')
            calibre_id = metadata.get('calibre_id')

            link_parts = []

            if source_file:
                # Create file:/// URL for clickable links in Joplin/Obsidian
                # Windows: file:///D:/path/to/file.pdf
                # Linux/Mac: file:///home/user/file.pdf

                # Normalize path separators to forward slashes for URLs
                url_path = source_file.replace('\\', '/')

                # Add file:/// prefix
                if url_path.startswith('/'):
                    # Unix path
                    file_url = f"file://{url_path}"
                else:
                    # Windows path (e.g., D:/...)
                    file_url = f"file:///{url_path}"

                # Extract filename (handle both Windows and Unix paths)
                if '/' in url_path:
                    filename = url_path.split('/')[-1]
                else:
                    filename = url_path

                link_parts.append(f"[{filename}]({file_url})")

            # Add Calibre URI if available (opens in Calibre library viewer)
            if calibre_id:
                # Format: calibre://view/<calibre_id>
                # Optional: add #page=N if we have a page number
                calibre_url = f"calibre://view/{calibre_id}"

                # Add page anchor if we have page info (the physical page:
                # the viewer counts pages, not printed labels)
                if metadata.get('page_number'):
                    calibre_url += f"#page={metadata['page_number']}"

                link_parts.append(f"[📚 {t('export.open_in_calibre', lang)}]({calibre_url})")

            if link_parts:
                lines.append(f"**{t('export.source', lang)}:** {' | '.join(link_parts)}  ")

            lines.append(f"")

            # Quote
            snippet = self._rag.searcher._get_context_snippet(text, query_text) if query_text else text[:300]
            lines.append(f"> {snippet}")
            lines.append(f"")

            # Additional metadata
            meta_lines = []
            if metadata.get('language'):
                meta_lines.append(f"{t('label.language', lang)}: {metadata['language']}")
            if metadata.get('subject'):
                meta_lines.append(f"{t('export.subject', lang)}: {metadata['subject']}")
            if metadata.get('publisher'):
                meta_lines.append(f"{t('export.publisher', lang)}: {metadata['publisher']}")
            if metadata.get('isbn'):
                isbn_text = f"ISBN: {metadata['isbn']}"
                # Add warning if ISBN from Calibre (not from file)
                if metadata.get('isbn_source') == 'calibre':
                    isbn_text += " ?"
                meta_lines.append(isbn_text)

            if meta_lines:
                lines.append(f"*{'   '.join(meta_lines)}*  ")

            lines.append(f"")
            lines.append(f"---")
            lines.append(f"")

        # Footer with tags
        tags = ["#archilles", "#rag", t('export.tag_search', lang)]
        if any(r['metadata'].get('language') == 'la' for r in results):
            tags.append(t('export.tag_latin', lang))
        if any(r['metadata'].get('language') == 'de' for r in results):
            tags.append(t('export.tag_german', lang))

        lines.append(f"")
        lines.append(" ".join(tags))

        # Write file
        content = "\n".join(lines)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(content)

        return output_file

    @staticmethod
    def get_system_prompt(citation_config=None) -> str:
        """
        Get the system prompt for Claude with citation instructions.

        Args:
            citation_config: Optional CitationConfig instance. When provided,
                rule 5 includes the user's preferred bibliography style.

        Returns XML-formatted instructions that tell Claude to cite sources.
        """
        # Build bibliography instruction (rule 5)
        if citation_config is not None:
            from src.citation.config import format_bibliography_instruction
            bib_instruction = (
                "Summarize all cited sources as a bibliography at the end. "
                + format_bibliography_instruction(citation_config)
            )
        else:
            bib_instruction = "Summarize all cited sources as a bibliography at the end."

        return f"""<system_instructions>
You are an academic research assistant. Your task is to answer the user's question based ONLY on the provided document excerpts.

<rules>
1. Cite every factual claim immediately with the document's ID in square brackets, e.g. [doc_1].
2. Do not use external information. If the answer is not in the documents, say so clearly.
3. Answer in the user's language, but keep the scholarly terminology.
4. For multiple sources supporting the same statement, give all relevant IDs, e.g. [doc_1, doc_3].
5. {bib_instruction}
</rules>
</system_instructions>"""

    def format_results_as_xml(
        self,
        results: List[Dict[str, Any]],
        query_text: str,
        expand_context: bool = False,
        expansion_chars: int = 400
    ) -> str:
        """
        Format search results as XML-structured documents for Claude.

        Creates a <documents> block with individual <document> entries,
        each containing <meta> and <content> sections.

        Args:
            results: Search results from query()
            query_text: Original user query
            expand_context: Enable context expansion (Small-to-Big) if char_offsets available
            expansion_chars: Characters to add before/after chunk (default: 400)

        Returns:
            XML-formatted string ready for Claude
        """
        lines = []

        lines.append("<documents>")

        # Children of the same section share one parent — fetch each parent
        # chunk only once per prompt instead of once per result.
        parent_cache: Dict[str, Any] = {}

        for i, result in enumerate(results, start=1):
            doc_id = f"doc_{i}"
            metadata = result['metadata']
            text = result['text']

            # Apply context expansion if enabled and available
            if expand_context:
                text = self.expand_chunk_context(
                    text, metadata, expansion_chars, parent_cache=parent_cache
                )

            # Build metadata line (matches inline metadata format)
            meta_parts = []

            if metadata.get('author'):
                meta_parts.append(f"Author: {metadata['author']}")
            if metadata.get('book_title'):
                meta_parts.append(f"Title: {metadata['book_title']}")
            if metadata.get('year'):
                meta_parts.append(f"Year: {metadata['year']}")

            section_meta = self._rag._format_section_meta(metadata)
            if section_meta:
                meta_parts.append(section_meta)

            page_val, _, _ = self._rag._resolve_page_info(metadata)
            if page_val:
                meta_parts.append(f"Page: {page_val}")

            meta_str = " | ".join(meta_parts) if meta_parts else "Metadata not available"

            # Build inline metadata for content injection
            inline_meta = self._build_inline_metadata(metadata, doc_id)

            # Inject metadata into text content
            # This helps Claude understand context (e.g., a quote from Arendt vs. Heidegger)
            text_with_metadata = f"{inline_meta}\n{text}\n<<<END SOURCE>>>"

            # Build XML document entry
            lines.append(f"   <document id=\"{doc_id}\">")
            lines.append(f"      <meta>{meta_str}</meta>")
            lines.append(f"      <content>{self._escape_xml(text_with_metadata)}</content>")
            lines.append(f"   </document>")

        lines.append("</documents>")
        lines.append("")
        lines.append("<user_query>")
        lines.append(self._escape_xml(query_text))
        lines.append("</user_query>")

        return "\n".join(lines)

    def expand_chunk_context(
        self,
        chunk_text: str,
        metadata: Dict[str, Any],
        expansion_chars: int = 400,
        parent_cache: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Expand chunk context using the parent chunk or stored window_text (Small-to-Big Retrieval).

        Priority:
        1. Use the parent chunk text if parent_id resolves (hierarchical index):
           the full structural section (~2048 tokens) is the richer "Big" context.
        2. Use window_text if stored (flat index / chunk without a parent):
           the pre-computed ±500-char window.
        3. Fall back to original chunk text.

        Args:
            chunk_text: Original chunk text from search result
            metadata: Chunk metadata (may contain window_text, parent_id)
            expansion_chars: Characters to add before and after (not used here)

        Returns:
            Expanded text with context, or original chunk if expansion not possible
        """
        # Option 1: Load parent chunk for context (hierarchical Small-to-Big).
        # Preferred over window_text — the parent is the whole structural section,
        # a broader context than the ±500-char window. Children carry a parent_id;
        # flat-index chunks do not and fall through to window_text below.
        parent_id = metadata.get('parent_id', '')
        if parent_id and hasattr(self._rag, 'store'):
            if parent_cache is not None and parent_id in parent_cache:
                parent = parent_cache[parent_id]
            else:
                parent = self._rag.store.get_by_id(parent_id)
                if parent_cache is not None:
                    parent_cache[parent_id] = parent
            if parent and parent.get('text'):
                return parent['text']

        # Option 2: Use pre-computed window_text from the index (flat fallback)
        window_text = metadata.get('window_text', '')
        if window_text and len(window_text) > len(chunk_text):
            return window_text

        # Graceful degradation: return original chunk
        return chunk_text

    def _build_inline_metadata(self, metadata: Dict[str, Any], doc_id: str) -> str:
        """
        Build inline metadata string to inject before chunk text.

        Format: <<<SOURCE ID=doc_1>>>
                [Author: Arendt | Title: Vita activa | Year: 1958 | Chapter: Action | Page: 213]

        This provides context for interpretation -- a sentence from Arendt
        means something different than the same sentence from Heidegger.
        """
        meta_parts = []

        if metadata.get('author'):
            meta_parts.append(f"Author: {metadata['author']}")
        if metadata.get('book_title'):
            meta_parts.append(f"Title: {metadata['book_title']}")
        if metadata.get('year'):
            meta_parts.append(f"Year: {metadata['year']}")

        section_meta = self._rag._format_section_meta(metadata)
        if section_meta:
            meta_parts.append(section_meta)

        page_val, _, _ = self._rag._resolve_page_info(metadata)
        if page_val:
            meta_parts.append(f"Page: {page_val}")

        if metadata.get('language'):
            meta_parts.append(f"Language: {metadata['language']}")

        meta_str = " | ".join(meta_parts) if meta_parts else "no metadata"

        return f"<<<SOURCE ID={doc_id}>>>\n[{meta_str}]"

    @staticmethod
    def _escape_xml(text: str) -> str:
        """Escape XML special characters."""
        return _html_escape(text, quote=True)

    def create_claude_prompt(
        self,
        results: List[Dict[str, Any]],
        query_text: str,
        expand_context: bool = False,
        expansion_chars: int = 400,
        citation_config=None,
    ) -> Dict[str, str]:
        """
        Create a complete prompt package for Claude with system instructions and XML documents.

        This combines:
        - System prompt with citation rules (style-aware when citation_config is provided)
        - XML-formatted documents with metadata
        - User query

        Args:
            results: Search results from query()
            query_text: Original user query
            expand_context: Enable context expansion (Small-to-Big) if available
            expansion_chars: Characters to add before/after chunk (default: 400)
            citation_config: Optional CitationConfig for bibliography style

        Returns:
            Dictionary with 'system' and 'user' prompts
        """
        system_prompt = self.get_system_prompt(citation_config=citation_config)
        xml_content = self.format_results_as_xml(
            results,
            query_text,
            expand_context=expand_context,
            expansion_chars=expansion_chars
        )

        return {
            'system': system_prompt,
            'user': xml_content,
            'num_sources': len(results),
            'total_tokens_approx': len(system_prompt.split()) + len(xml_content.split())
        }
