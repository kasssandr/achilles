"""Search component: query dispatch (hybrid/semantic/keyword/exact-phrase)
and result formatting. Extracted from the ArchillesRAG monolith (8.16)."""
import os
import re
from typing import Any, Dict, List, Literal

from src.archilles.constants import ChunkType, SectionType
from src.archilles.i18n import t
from src.retriever.results import diversify_results, matches_tag_filter


class Searcher:
    """Holds a back-reference to the ArchillesRAG facade and reads shared
    state (store, embedding_model, ...) through it, so attribute mutation
    by callers (e.g. tests replacing ``rag.embedding_model``) stays visible.
    """

    def __init__(self, rag):
        self._rag = rag

    def _remove_stop_words(self, query_text: str) -> tuple:
        """
        Remove common stop words from query for better search results.

        Returns:
            Tuple of (cleaned_query, removed_words)
        """
        original_words = query_text.split()
        result_words = []
        removed = []

        for word in original_words:
            clean_word = word.lower().strip('.,;:!?"\'()[]{}')
            if clean_word in self._rag.stop_words:
                removed.append(word)
            else:
                result_words.append(word)

        return ' '.join(result_words), removed

    def query(
        self,
        query_text: str,
        top_k: int = 10,
        mode: Literal['semantic', 'keyword', 'hybrid'] = 'hybrid',
        language: str = None,
        book_id: str = None,
        exact_phrase: bool = False,
        tag_filter: List[str] = None,
        section_filter: str = SectionType.MAIN,
        chunk_type_filter: str = ChunkType.CONTENT,
        max_per_book: int = 2,
        min_similarity: float = 0.0
    ) -> List[Dict[str, Any]]:
        """
        Search for relevant passages.

        Args:
            query_text: Search query
            top_k: Number of results to return (default: 10)
            mode: Search mode - 'semantic' (BGE-M3), 'keyword' (FTS), or 'hybrid' (both, default)
            language: Filter by language (e.g., 'de', 'en', 'la') or comma-separated list
            book_id: Filter by specific book ID
            exact_phrase: Use exact phrase matching (for Latin quotes, etc.)
            tag_filter: Filter by Calibre tags (e.g., ['Geschichte', 'Philosophie']).
                       AND logic: results must carry ALL given tags.
            section_filter: Filter by section type (default: 'main' = exclude front/back matter)
                           'main' = main content only (excludes bibliography, index, etc.)
                           'main_content' / 'front_matter' / 'back_matter' = exact match
                           None = all sections (no filtering)
            chunk_type_filter: Filter by chunk type (default: 'content' - book text only)
                              'content' = book text only (DEFAULT - excludes Calibre comments)
                              'calibre_comment' = Calibre comments only
                              None = all chunk types (book text + comments mixed)
            max_per_book: Maximum results per book (default: 2, use 999 for unlimited)
            min_similarity: Minimum similarity score (0.0-1.0, default: 0.0)
                           Higher = stricter, fewer but more relevant results
                           ONLY applied in semantic mode (cosine scale) — hybrid
                           (RRF) and keyword (BM25) scores are incompatible scales

        Returns:
            List of relevant chunks with metadata and scores
        """
        # Remove stop words for better search quality (unless exact phrase matching)
        original_query = query_text
        if not exact_phrase:
            query_text, removed_words = self._remove_stop_words(query_text)
            if removed_words:
                print(f"  ℹ️  Removed common words: {', '.join(removed_words)}")
            if not query_text.strip():
                # All words were stop words!
                print("  ⚠️  Query contains only common words. Using original query.")
                query_text = original_query

        # Build filter message
        filters = []
        if language:
            filters.append(f"language={language}")
        if book_id:
            filters.append(f"book={book_id}")
        if exact_phrase:
            filters.append("exact phrase")
        if tag_filter:
            filters.append(f"tags={', '.join(tag_filter)}")
        if section_filter:
            filters.append(f"section={section_filter}")
        if chunk_type_filter:
            filters.append(f"chunk_type={chunk_type_filter}")
        if max_per_book < 999:
            filters.append(f"max {max_per_book}/book")

        filter_msg = f" ({', '.join(filters)})" if filters else ""
        print(f"QUERY [{mode.upper()}]: \"{query_text}\"{filter_msg}")
        print(f"  Searching {self._rag._chunk_count} chunks...\n")

        # Oversample to allow for diversity filtering
        # If max_per_book is set, we need to fetch more results than top_k
        # to ensure we have enough diverse results after filtering
        # Higher factor (5) enables finding more diverse books in large libraries
        oversample_factor = 5 if max_per_book < 999 else 1
        search_top_k = top_k * oversample_factor

        # Route to appropriate search method
        if mode == 'semantic':
            results = self._semantic_search(query_text, search_top_k, language, book_id, chunk_type_filter, section_type=section_filter)
        elif mode == 'keyword':
            results = self._keyword_search(query_text, search_top_k, language, book_id, chunk_type_filter, exact_phrase=exact_phrase, section_type=section_filter)
        elif mode == 'hybrid':
            results = self._hybrid_search(query_text, search_top_k, language, book_id, chunk_type_filter, exact_phrase=exact_phrase, section_type=section_filter)
        else:
            raise ValueError(f"Invalid mode: {mode}. Must be 'semantic', 'keyword', or 'hybrid'")

        # Filter out trivially short chunks (titles, headings, bibliography entries)
        # A meaningful academic passage should be at least ~100 characters
        min_chunk_length = 100
        short_count = sum(1 for r in results if len(r.get('text', '')) < min_chunk_length)
        results = [r for r in results if len(r.get('text', '')) >= min_chunk_length]
        if short_count > 0:
            print(f"  Filtered {short_count} trivially short chunks (<{min_chunk_length} chars)")

        # Post-filter by tags (if specified) — AND logic: a result must carry
        # ALL requested tags, as documented in the MCP tool schema (fix 8.1).
        if tag_filter:
            filtered_results = [
                result for result in results
                if matches_tag_filter(result['metadata'].get('tags', ''), tag_filter)
            ]

            # Re-rank after filtering
            for i, result in enumerate(filtered_results):
                result['rank'] = i + 1

            results = filtered_results  # Don't truncate yet - need data for diversification

        # Diversify results by book (max N results per book)
        if max_per_book < 999 and len(results) > 0:
            results = diversify_results(results, max_per_book, top_k)
        else:
            results = results[:top_k]

        # Apply minimum similarity threshold (semantic mode only — finding 8.2)
        results = self._apply_min_similarity(results, min_similarity, mode)

        return results

    @staticmethod
    def _apply_min_similarity(results: List[Dict[str, Any]], min_similarity: float,
                              mode: str) -> List[Dict[str, Any]]:
        """Apply the min_similarity threshold (finding 8.2).

        min_similarity is a cosine-scale (0-1) threshold — only the semantic
        mode produces cosine scores. Hybrid (RRF, ~1/60) and keyword (BM25)
        scores live on incompatible scales where any cosine-style threshold
        silently empties or arbitrarily truncates the result list.
        """
        if min_similarity <= 0.0:
            return results
        if mode != 'semantic':
            print(f"  ℹ️ min_similarity={min_similarity} ignored — only applies to semantic mode")
            return results
        filtered = [r for r in results if r.get('score', 0) >= min_similarity]
        removed = len(results) - len(filtered)
        if removed > 0:
            print(f"  🎯 Filtered {removed} results below {min_similarity:.0%} similarity")
        return filtered

    def _semantic_search(
        self,
        query_text: str,
        top_k: int,
        language: str = None,
        book_id: str = None,
        chunk_type_filter: str = None,
        section_type: str = None
    ) -> List[Dict[str, Any]]:
        """Semantic search using BGE-M3 embeddings via LanceDB."""
        query_embedding = self._rag.embedding_model.encode(
            query_text,
            convert_to_numpy=True
        )

        resolved_book_id, calibre_id, source_id = self._rag._resolve_book_id(book_id)

        results = self._rag.store.vector_search(
            query_vector=query_embedding,
            top_k=top_k,
            book_id=resolved_book_id,
            calibre_id=calibre_id,
            source_id=source_id,
            chunk_type=chunk_type_filter,
            language=language,
            section_type=section_type
        )

        # Format results
        return self._format_lancedb_results(results, score_type='semantic')

    def _keyword_search(
        self,
        query_text: str,
        top_k: int,
        language: str = None,
        book_id: str = None,
        chunk_type_filter: str = None,
        exact_phrase: bool = False,
        section_type: str = None
    ) -> List[Dict[str, Any]]:
        """Keyword search using LanceDB full-text search."""
        if exact_phrase:
            return self._exact_phrase_search(query_text, top_k, language, book_id, chunk_type_filter)

        resolved_book_id, calibre_id, source_id = self._rag._resolve_book_id(book_id)

        results = self._rag.store.fts_search(
            query_text=query_text,
            top_k=top_k,
            book_id=resolved_book_id,
            calibre_id=calibre_id,
            source_id=source_id,
            chunk_type=chunk_type_filter,
            language=language,
            section_type=section_type
        )

        # Format results
        return self._format_lancedb_results(results, score_type='keyword')

    def _exact_phrase_search(
        self,
        query_text: str,
        top_k: int,
        language: str = None,
        book_id: str = None,
        chunk_type_filter: str = None
    ) -> List[Dict[str, Any]]:
        """
        Exact phrase matching (case-insensitive).

        Uses LanceDB FTS with individual words to get candidates,
        then post-filters for whitespace-normalized exact phrase match.
        """
        resolved_book_id, calibre_id, source_id = self._rag._resolve_book_id(book_id)

        # Use FTS with individual words as pre-filter — much smaller candidate
        # set than a full table scan, but avoids Tantivy phrase-query tokenization issues
        candidates = self._rag.store.fts_search(
            query_text=query_text,
            top_k=top_k * 10,
            book_id=resolved_book_id,
            calibre_id=calibre_id,
            source_id=source_id,
            chunk_type=chunk_type_filter,
            language=language,
        )

        # Post-filter: whitespace-normalized exact phrase match
        query_normalized = re.sub(r'\s+', ' ', query_text.lower().strip())
        matches = []
        for chunk in candidates:
            doc_text = chunk.get('text', '')
            doc_normalized = re.sub(r'\s+', ' ', doc_text.lower())

            if query_normalized in doc_normalized:
                count = doc_normalized.count(query_normalized)
                matches.append({
                    'rank': 0,
                    'text': doc_text,
                    'metadata': chunk,
                    # Raw occurrence count — own scale, labelled via
                    # score_type so consumers don't mix it with RRF/cosine
                    # scores (finding 8.4).
                    'score': count,
                    'similarity': min(count / 10.0, 1.0),
                    'score_type': 'exact_phrase',
                })

        matches.sort(key=lambda x: x['score'], reverse=True)

        for i, match in enumerate(matches[:top_k]):
            match['rank'] = i + 1

        return matches[:top_k]

    def _hybrid_search(
        self,
        query_text: str,
        top_k: int,
        language: str = None,
        book_id: str = None,
        chunk_type_filter: str = None,
        exact_phrase: bool = False,
        section_type: str = None
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search using LanceDB native hybrid search (vector + FTS).

        IMPORTANT: If exact_phrase=True, ONLY returns exact phrase matches!
        """
        # For exact phrase matching, skip hybrid search entirely
        if exact_phrase:
            return self._keyword_search(query_text, top_k, language, book_id, chunk_type_filter, exact_phrase=True, section_type=section_type)

        query_embedding = self._rag.embedding_model.encode(
            query_text,
            convert_to_numpy=True
        )

        resolved_book_id, calibre_id, source_id = self._rag._resolve_book_id(book_id)

        results = self._rag.store.hybrid_search(
            query_text=query_text,
            query_vector=query_embedding,
            top_k=top_k,
            book_id=resolved_book_id,
            calibre_id=calibre_id,
            source_id=source_id,
            chunk_type=chunk_type_filter,
            language=language,
            section_type=section_type
        )

        # Format and apply boost factors
        formatted_results = self._format_lancedb_results(results, score_type='hybrid')

        # Apply boost factors for Calibre comments and tag matches
        query_terms = set(query_text.lower().split())

        for result in formatted_results:
            metadata = result['metadata']

            # Boost for Calibre comments
            if metadata.get('chunk_type') == ChunkType.CALIBRE_COMMENT:
                result['score'] *= 1.2
                result['similarity'] *= 1.2

            # Boost for tag matches
            if metadata.get('tags'):
                result_tags = set(metadata['tags'].lower().split(', '))
                if query_terms & result_tags:
                    result['score'] *= 1.15
                    result['similarity'] *= 1.15

        # Re-sort by boosted scores
        formatted_results.sort(key=lambda x: x['score'], reverse=True)

        # Re-assign ranks
        for i, result in enumerate(formatted_results):
            result['rank'] = i + 1

        return formatted_results

    def _format_lancedb_results(self, results: List[Dict], score_type: str = 'semantic') -> List[Dict[str, Any]]:
        """Format LanceDB results into standard format."""
        formatted_results = []

        for i, result in enumerate(results):
            # Extract text and score
            text = result.get('text', '')
            score = result.get('score', 0.0)

            # Build metadata dict (all fields except text, vector, score)
            metadata = {k: v for k, v in result.items()
                       if k not in ('text', 'vector', 'score', '_distance', '_score')}

            formatted_result = {
                'rank': i + 1,
                'text': text,
                'metadata': metadata,
                'similarity': score,
                'score': score,
                'score_type': score_type
            }
            formatted_results.append(formatted_result)

        return formatted_results

    def _get_context_snippet(self, text: str, query_text: str, context_chars: int = 200) -> str:
        """
        Extract a relevant snippet from text that contains query terms.

        For keyword/hybrid searches, this shows WHERE the match was found.
        Much better UX than showing first 300 chars which might not contain the match!

        IMPORTANT: Handles line breaks in phrases!
        If query is "evangelista et a presbyteris" and text has line break:
        "...evangelista\net a presbyteris..." ? still finds it!

        Args:
            text: Full chunk text
            query_text: Original query
            context_chars: Characters of context around match (default: 200)

        Returns:
            Snippet with "..." prefix/suffix if truncated
        """
        text_lower = text.lower()
        query_lower = query_text.lower()
        best_match_pos = len(text)  # Default: end of text

        # Strategy 1: Try to find the ENTIRE query phrase first (exact phrase matching)
        # This is critical for Latin quotes like "evangelista et a presbyteris"
        phrase_pos = text_lower.find(query_lower)
        if phrase_pos != -1:
            best_match_pos = phrase_pos
        else:
            # Strategy 1b: Try regex matching with flexible whitespace
            # This handles line breaks! "evangelista\s+et\s+a\s+presbyteris" matches "evangelista\net a presbyteris"
            # Escape special regex chars in query, then replace spaces with \s+
            query_escaped = re.escape(query_lower)
            query_pattern = re.sub(r'\\ ', r'\\s+', query_escaped)  # Replace escaped spaces with \s+

            match = re.search(query_pattern, text_lower, re.IGNORECASE)
            if match:
                best_match_pos = match.start()
            else:
                # Strategy 2: Fallback to individual token matching
                # This works for partial matches or when query is multiple concepts
                # Simple tokenization: lowercase and split on word boundaries
                query_tokens = re.findall(r"[\w'-]+", query_text.lower())

                if not query_tokens:
                    # No tokens found, show beginning
                    return text[:300] + ('...' if len(text) > 300 else '')

                # Find first occurrence of any query token
                for token in query_tokens:
                    pos = text_lower.find(token.lower())
                    if pos != -1 and pos < best_match_pos:
                        best_match_pos = pos

        # If no match found (shouldn't happen), show beginning
        if best_match_pos == len(text):
            return text[:300] + ('...' if len(text) > 300 else '')

        # Calculate snippet boundaries
        start = max(0, best_match_pos - context_chars)
        end = min(len(text), best_match_pos + context_chars)

        # Extract snippet
        snippet = text[start:end]

        # Add ellipsis if truncated
        if start > 0:
            snippet = '...' + snippet
        if end < len(text):
            snippet = snippet + '...'

        return snippet

    def print_results(self, results: List[Dict[str, Any]], query_text: str = "",
                      lang: str = "en"):
        """Pretty print search results with context snippets.

        ``lang`` is the operator/interface language (``get_languages(...)[0]``)
        for the visible labels; defaults to English.
        """
        if not results:
            print(f"\n{t('results.no_results', lang)}\n")
            return

        print(f"\n{t('results.header', lang).format(n=len(results))}\n")
        print("=" * 80)

        for result in results:
            rank = result['rank']
            similarity = result['similarity']
            metadata = result['metadata']
            text = result['text']

            # Build citation with section/chapter info
            citation_parts = []
            if metadata.get('book_title'):
                citation_parts.append(metadata['book_title'])

            section_citation = self._rag._format_section_citation(metadata)
            if section_citation:
                citation_parts.append(section_citation)

            # Debug mode: show raw metadata values
            if os.environ.get('DEBUG_METADATA'):
                print(f"    [DEBUG] section: {repr(metadata.get('section'))}, section_title: {repr(metadata.get('section_title'))}")
                print(f"    [DEBUG] page_label: {repr(metadata.get('page_label'))}, label_source: {repr(metadata.get('label_source'))}")

            page_val, is_pdf, page_warning = self._rag._resolve_page_info(metadata)
            if page_val:
                page_label = t('page.pdf', lang) if is_pdf else t('page.plain', lang)
                citation_parts.append(f"{page_label} {page_val}")

            citation = ', '.join(citation_parts) if citation_parts else metadata.get('book_id', 'Unknown')

            # Add chunk type indicator
            chunk_type = metadata.get('chunk_type', '')
            type_indicator = ''
            if chunk_type == ChunkType.CALIBRE_COMMENT:
                type_indicator = ' [CALIBRE_COMMENT]'
            elif chunk_type == ChunkType.PHASE1_METADATA:
                type_indicator = ' [METADATA]'

            if similarity > 0.8:
                quality = t('results.relevance_very_high', lang)
            elif similarity > 0.6:
                quality = t('results.relevance_high', lang)
            else:
                quality = t('results.relevance_medium', lang)

            print(f"\n[{rank}] {citation}{type_indicator}")
            print(f"    {t('label.relevance', lang)}: {similarity:.3f} ({quality})")

            # Show page number warning if applicable
            if page_warning:
                print(f"    ?? {page_warning}")

            # Show context snippet with query terms (if available)
            if query_text:
                snippet = self._get_context_snippet(text, query_text)
            else:
                snippet = text[:300] + ('...' if len(text) > 300 else '')

            print(f"    {t('results.text', lang)}: {snippet}")

        print("\n" + "=" * 80 + "\n")
