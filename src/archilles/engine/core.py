"""ARCHILLES RAG engine core.

Until 2026-06 this class lived as ``archillesRAG`` inside
``scripts/rag_demo.py`` (code review 2026-06-10, findings 4.9/8.16).
``scripts/rag_demo.py`` is now a thin CLI wrapper around this module.
"""
import threading
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import numpy as np
from sentence_transformers import SentenceTransformer

from src.archilles.constants import ChunkType, SectionType
from src.archilles.execution import ExecutionPlan
from src.archilles.i18n import get_ocr_language, get_stopwords
from src.archilles.recipe import IndexRecipe, default_recipe
from src.extractors import UniversalExtractor
from src.storage import LanceDBStore

from src.archilles.engine.search import Searcher
from src.archilles.engine.prompting import PromptBuilder
from src.archilles.engine.indexing import Indexer


class LanceDBError(Exception):
    """Raised when LanceDB operations fail."""
    pass


# Process-wide cache of loaded SentenceTransformer instances, keyed by
# (model_name, device). The unified MCP server builds one ArchillesRAG per
# source; without sharing, every source loads its own copy of the same
# ~2.3 GB model (3x BGE-M3 in RAM, 3x load time on first search).
_shared_embedding_models: dict = {}
_shared_models_lock = threading.Lock()


def _get_shared_embedding_model(model_name: str, device: str) -> SentenceTransformer:
    """Return a process-wide shared SentenceTransformer for (model_name, device).

    On CUDA the model is converted to FP16 and its ``encode()`` patched to
    return FP32 numpy (LanceDB expects FP32). Both happen once, inside this
    factory, so a shared instance is never converted or patched twice.
    """
    key = (model_name, device)
    with _shared_models_lock:
        model = _shared_embedding_models.get(key)
        if model is not None:
            return model

        model = SentenceTransformer(model_name, device=device)
        if device == "cuda":
            # FP16: halves VRAM pressure, ~1.3-1.8x faster on GPUs without
            # tensor cores (T1000). encode() then returns FP16 numpy —
            # LanceDB expects FP32, hence the encode patch.
            model = model.half()
            _orig_encode = model.encode

            def _encode_fp32(*args, **kwargs):
                out = _orig_encode(*args, **kwargs)
                if isinstance(out, np.ndarray) and out.dtype != np.float32:
                    out = out.astype(np.float32)
                return out

            model.encode = _encode_fp32

        _shared_embedding_models[key] = model
        return model


class ArchillesRAG:
    """
    Simple RAG system for academic books.

    Features:
    - BGE-M3 embeddings (1024 dimensions, multilingual)
    - LanceDB with native hybrid search
    - Exact page citations
    - Semantic + keyword search
    """

    # Fields to copy from Calibre book_data into chunk metadata
    _CALIBRE_FIELDS = ('author', 'title', 'year', 'publisher', 'language', 'isbn',
                       'calibre_id', 'tags', 'comments', 'custom_fields')

    @staticmethod
    def _format_tags(tags) -> str:
        """Format tags as a comma-separated string, whether input is a list or string."""
        return ', '.join(tags) if isinstance(tags, list) else tags

    @staticmethod
    def _resolve_book_id(book_id: str):
        """
        Resolve book_id to (resolved_book_id, calibre_id, source_id) tuple.

        If book_id is numeric, treat it as a calibre_id *and* source_id
        (backward compat).  Otherwise it's a plain book_id string.
        """
        if book_id and str(book_id).isdigit():
            return None, int(book_id), str(book_id)
        return book_id, None, None

    @staticmethod
    def _format_section_citation(metadata: Dict[str, Any]) -> str:
        """
        Build a section/chapter citation string from metadata.
        Returns empty string if no section info is available.
        """
        section = metadata.get('section')
        section_title = metadata.get('section_title')

        if section and section_title:
            return f"Section {section} - {section_title}"
        if section:
            return f"Section {section}"
        if section_title:
            return section_title
        if metadata.get('chapter'):
            return metadata['chapter']
        return ''

    @staticmethod
    def _format_section_meta(metadata: Dict[str, Any], label: str = "Chapter") -> str:
        """
        Build a section/chapter metadata string for XML/inline output.
        Returns empty string if no section info is available.
        """
        section = metadata.get('section')
        section_title = metadata.get('section_title')

        if section and section_title:
            return f"{label}: {section} - {section_title}"
        if section_title:
            return f"{label}: {section_title}"
        if section:
            return f"Section: {section}"
        if metadata.get('chapter'):
            return f"{label}: {metadata['chapter']}"
        return ''

    @staticmethod
    def _resolve_page_info(metadata: Dict[str, Any]):
        """
        Resolve the best page value and optional warning from metadata.
        Returns (page_value_or_None, is_pdf_page: bool, warning_or_None).

        page_value is the raw page number/label (e.g. "213", "xiv").
        is_pdf_page indicates whether this is a PDF page (vs. printed/label).
        """
        page_label = metadata.get('page_label')
        printed_page = metadata.get('printed_page')
        printed_conf = metadata.get('printed_page_confidence', 0.0)

        if page_label:
            return page_label, False, None

        if printed_page and printed_conf >= 0.8:
            warning = None
            if printed_conf < 0.9:
                warning = f"Seitenzahl-Konfidenz: {printed_conf:.2f} - bitte verifizieren"
            return printed_page, False, warning

        page = metadata.get('page') or metadata.get('page_number')
        if page:
            warning = None
            if printed_page:
                warning = f"Gedruckte Seitenzahl unsicher (Konfidenz: {printed_conf:.2f})"
            return page, True, warning

        return None, False, None

    _CHUNK_META_KEYS = [
        ('page', 'page_number'), ('page_label', 'page_label'),
        ('chapter', 'chapter'), ('section', 'section'),
        ('section_title', 'section_title'), ('section_type', 'section_type'),
        ('language', 'language'),
        ('region', 'region'), ('label_source', 'label_source'),
        ('producer_version', 'producer_version'),
    ]

    def __init__(
        self,
        db_path: str = "./archilles_rag_db",
        model_name: str = None,  # Will be set by profile or default to BGE-M3
        reset_db: bool = False,
        enable_ocr: bool = False,
        force_ocr: bool = False,
        ocr_backend: str = "auto",
        ocr_language: str | None = None,  # None → derived from `languages`
        languages: list[str] | None = None,  # corpus languages (OCR, stop words)
        profile: str = None,  # 'minimal', 'balanced', 'maximal', or None (auto-detect)
        use_modular_pipeline: bool = False,  # Future: use modular architecture
        hierarchical: bool = False,  # Enable parent-child chunking
        adapter=None,  # Optional SourceAdapter for metadata lookup
        skip_model: bool = False,  # Skip loading embedding model (for prepare-only mode)
        prepare_chunk_size: int | None = None,  # Phase-1 chunk size (tokens); None → recipe child size
        prepare_overlap: int | None = None,      # Phase-1 chunk overlap (tokens); None → recipe child overlap
        recipe: IndexRecipe | None = None,  # Index identity/chunk schema; None → default_recipe()
        execution_plan: "ExecutionPlan | None" = None,  # Hardware-Tiers-V2: drives batch/device
    ):
        """
        Initialize RAG system.

        Args:
            db_path: Path to LanceDB storage
            model_name: Sentence transformer model (overrides profile if set)
            reset_db: If True, delete and recreate the database
            enable_ocr: Enable OCR for scanned PDFs (auto-detect)
            force_ocr: Force OCR even for digital PDFs
            ocr_backend: OCR backend (auto, tesseract, lighton, olmocr)
            ocr_language: Language codes for Tesseract
            profile: Hardware profile (minimal/balanced/maximal) - auto-detects if None
            use_modular_pipeline: Use ModularPipeline architecture (future)
            hierarchical: Enable parent-child chunking (parents ~2048, children ~512 tokens)
        """
        self.hierarchical = hierarchical
        self.use_modular_pipeline = use_modular_pipeline
        self.profile_name = profile
        self._adapter = adapter  # SourceAdapter (or None for legacy CalibreDB path)
        # Corpus-language settings (finding 2.33 / 8.3): OCR language and the
        # query stop-word set are derived from the configured `languages` list.
        # An explicit `ocr_language` (e.g. from --ocr-language) still wins.
        self.languages = languages
        if ocr_language is None:
            ocr_language = get_ocr_language(languages)
        self.stop_words = get_stopwords(languages)
        # Index recipe: single source of truth for the chunk schema (child/
        # parent sizes) and identity (model/dimension) of the main path.
        self.recipe = recipe or default_recipe()
        # Phase-1 chunk settings (used in prepare_book(), not in index_book()).
        # Default to the recipe's child sizes so prepare and the live path share
        # one schema; an explicit prepare_chunk_size/overlap (CLI) still wins.
        self._prepare_chunk_size = (
            prepare_chunk_size if prepare_chunk_size is not None
            else self.recipe.child_chunk_size
        )
        self._prepare_overlap = (
            prepare_overlap if prepare_overlap is not None
            else self.recipe.child_overlap
        )
        # Determine model and settings from profile
        import torch
        cuda_available = torch.cuda.is_available()
        try:
            mps_available = torch.backends.mps.is_available()
        except AttributeError:
            mps_available = False

        if execution_plan is not None:
            # Hardware-Tiers-V2: the ExecutionPlan owns the throughput knobs
            # (batch size + device). Identity (model/dimension) still comes from
            # the recipe — never from the plan — so vectors stay compatible.
            self.batch_size = execution_plan.batch_size
            if model_name is None:
                model_name = self.recipe.embedding_model
            want = execution_plan.embedding_device
            if want == "cuda" and cuda_available:
                self.device = "cuda"
            elif want == "mps" and mps_available:
                self.device = "mps"
            else:
                self.device = "cpu"
                if want in ("cuda", "mps"):
                    print(f"  ⚠️  {want.upper()} not available, falling back to CPU")
            print(
                f"Initializing ARCHILLES RAG "
                f"(mode: {execution_plan.mode}, class: {execution_plan.hardware_class})..."
            )
        elif profile:
            from src.archilles.profiles import get_profile
            profile_config = get_profile(profile)
            if model_name is None:
                model_name = profile_config.embedding_model
            self.batch_size = profile_config.batch_size
            # Auto-detect: use CUDA if profile wants it AND it's available
            if profile_config.embedding_device == "cuda" and cuda_available:
                self.device = "cuda"
            else:
                self.device = "cpu"
                if profile_config.embedding_device == "cuda" and not cuda_available:
                    print(f"  ⚠️  CUDA not available, falling back to CPU")
            print(f"Initializing ARCHILLES RAG (profile: {profile})...")
        else:
            # Default to the recipe's model and auto-detect device
            if model_name is None:
                model_name = self.recipe.embedding_model
            self.batch_size = 8  # Conservative default for 4GB GPUs
            self.device = 'cuda' if cuda_available else 'cpu'
            print(f"Initializing ARCHILLES RAG...")

        print(f"  Database: {db_path}")
        print(f"  Model: {model_name}")

        # Map string backend to enum (Befund 2.16: canonical converter)
        from src.extractors import OCRBackend
        ocr_backend_enum = OCRBackend.from_string(ocr_backend)

        # Initialize extractor with OCR options. Child chunk size/overlap come
        # from the recipe (single source) — unifies the live path with prepare.
        self.extractor = UniversalExtractor(
            chunk_size=self.recipe.child_chunk_size,
            overlap=self.recipe.child_overlap,
            enable_ocr=enable_ocr,
            force_ocr=force_ocr,
            ocr_backend=ocr_backend_enum,
            ocr_language=ocr_language
        )

        if enable_ocr or force_ocr:
            print(f"  OCR: {'force' if force_ocr else 'auto-detect'} ({ocr_backend})")

        # Initialize embedding model (skip for prepare-only mode). Instances
        # are shared process-wide per (model_name, device) — see
        # _get_shared_embedding_model.
        if skip_model:
            self.embedding_model = None
            print(f"  Embedding model: skipped (prepare-only mode)")
        else:
            print(f"  Loading embedding model... (first time: ~500 MB download)")
            self.embedding_model = _get_shared_embedding_model(model_name, self.device)
            if self.device == "cuda":
                print(f"  FP16 active (CUDA) — halved VRAM, faster inference")
            print(f"  Model loaded: {model_name} (device: {self.device})")

        # Handle database reset if requested
        self.db_path = Path(db_path)
        if reset_db:
            print(f"  Resetting database (deleting existing data)...")
            import shutil
            if self.db_path.exists():
                shutil.rmtree(self.db_path)
                print(f"    Deleted {db_path}")

        # Initialize LanceDB
        try:
            self.store = LanceDBStore(db_path=str(self.db_path))
            print(f"  LanceDB ready")

            # Count chunks (cached — refreshed via _refresh_chunk_count())
            self._chunk_count = self.store.count()
            print(f"  Current index: {self._chunk_count} chunks")

        except Exception as e:
            raise LanceDBError(
                f"LanceDB initialization failed.\n"
                f"Error: {e}\n\n"
                f"To recover, run with --reset-db flag:\n"
                f"  python scripts/batch_index.py --tag \"YourTag\" --reset-db\n\n"
                f"WARNING: This will delete the entire index. You'll need to re-index all books."
            )

        print(f"  Native hybrid search ready (vector + full-text)\n")

        self.searcher = Searcher(self)
        self.prompt_builder = PromptBuilder(self)
        self.indexer = Indexer(self)

    # ------------------------------------------------------------------
    # Indexing delegators (Indexer component)
    # ------------------------------------------------------------------

    def index_book(self, book_path: str, book_id: str = None, force: bool = False, phase: str = 'phase2') -> Dict[str, Any]:
        return self.indexer.index_book(book_path, book_id=book_id, force=force, phase=phase)

    def prepare_book(self, book_path: str, book_id: str = None,
                     output_dir: str = "./prepared_chunks") -> Dict[str, Any]:
        return self.indexer.prepare_book(book_path, book_id=book_id, output_dir=output_dir)

    def embed_prepared(self, input_dir: str, mode: str = 'local',
                       host: str = None, port: int = 8000, token: str = None,
                       batch_size: int = 100, use_gzip: bool = True,
                       profile: str = None, force: bool = False) -> Dict[str, Any]:
        return self.indexer.embed_prepared(
            input_dir, mode=mode, host=host, port=port, token=token,
            batch_size=batch_size, use_gzip=use_gzip, profile=profile, force=force,
        )

    def _update_metadata_only(self, book_id: str, book_metadata: Dict[str, Any],
                               new_hash: str, state: Dict[str, Any],
                               annotations: Optional[List[Dict[str, Any]]] = None,
                               annotation_hash: Optional[str] = None,
                               book_path: Optional[Path] = None) -> Dict[str, Any]:
        return self.indexer._update_metadata_only(
            book_id, book_metadata, new_hash, state,
            annotations=annotations, annotation_hash=annotation_hash, book_path=book_path,
        )

    def _extract_calibre_metadata(self, file_path: Path) -> Dict[str, Any]:
        return self.indexer._extract_calibre_metadata(file_path)

    def _build_comment_chunks(
        self,
        book_metadata: Dict[str, Any],
        book_id: str,
        book_format: str,
        metadata_hash: str,
        embed: bool = True,
    ) -> tuple:
        return self.indexer._build_comment_chunks(
            book_metadata, book_id, book_format, metadata_hash, embed=embed,
        )

    @staticmethod
    def _compute_metadata_hash(book_metadata: Dict[str, Any]) -> str:
        # Keep the signature identical; test_watchdog + batch_index call it on the class
        return Indexer._compute_metadata_hash(book_metadata)

    @staticmethod
    def _compute_annotation_hash(annotations: List[Dict[str, Any]]) -> str:
        # IMPORTANT: test_watchdog patches ArchillesRAG._compute_annotation_hash (8x) and
        # watchdog.py calls it on the class — this delegator is the patch target.
        return Indexer._compute_annotation_hash(annotations)



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
        return self.searcher.query(
            query_text,
            top_k=top_k,
            mode=mode,
            language=language,
            book_id=book_id,
            exact_phrase=exact_phrase,
            tag_filter=tag_filter,
            section_filter=section_filter,
            chunk_type_filter=chunk_type_filter,
            max_per_book=max_per_book,
            min_similarity=min_similarity,
        )

    def _exact_phrase_search(
        self,
        query_text: str,
        top_k: int,
        language: str = None,
        book_id: str = None,
        chunk_type_filter: str = None
    ) -> List[Dict[str, Any]]:
        return self.searcher._exact_phrase_search(
            query_text,
            top_k,
            language=language,
            book_id=book_id,
            chunk_type_filter=chunk_type_filter,
        )

    def print_results(self, results: List[Dict[str, Any]], query_text: str = "",
                      lang: str = "en"):
        return self.searcher.print_results(results, query_text=query_text, lang=lang)

    @staticmethod
    def _apply_min_similarity(results: List[Dict[str, Any]], min_similarity: float,
                              mode: str) -> List[Dict[str, Any]]:
        return Searcher._apply_min_similarity(results, min_similarity, mode)

    def export_to_markdown(
        self,
        results: List[Dict[str, Any]],
        query_text: str,
        output_file: str = None,
        lang: str = "en"
    ) -> str:
        return self.prompt_builder.export_to_markdown(results, query_text, output_file, lang=lang)

    def create_claude_prompt(
        self,
        results: List[Dict[str, Any]],
        query_text: str,
        expand_context: bool = False,
        expansion_chars: int = 400,
        citation_config=None,
    ) -> Dict[str, str]:
        return self.prompt_builder.create_claude_prompt(
            results,
            query_text,
            expand_context=expand_context,
            expansion_chars=expansion_chars,
            citation_config=citation_config,
        )


