"""
Calibre-based format converter.

Uses Calibre's ebook-convert tool to normalize exotic formats to EPUB/PDF,
then extracts text using our native extractors.
"""

from pathlib import Path
import subprocess
import tempfile
from typing import Optional

from .models import ExtractedText
from .exceptions import ConversionError, CalibreNotFoundError
from .epub_extractor import EPUBExtractor
from .pdf_extractor import PDFExtractor


class CalibreConverter:
    """
    Convert ebooks using Calibre's ebook-convert tool.

    Supports all formats that Calibre can convert:
    - MOBI, AZW3 (Kindle)
    - DJVU (scanned books)
    - DOC, DOCX (Word documents)
    - RTF, ODT, FB2, LIT, PDB, CHM
    - And many more
    """

    # Formats that Calibre can convert
    CONVERTIBLE_FORMATS = {
        'mobi', 'azw', 'azw3', 'azw4',  # Kindle
        'djvu', 'djv',  # Scanned books
        'doc', 'docx',  # Microsoft Word
        'rtf',  # Rich Text Format
        'odt',  # OpenDocument
        'fb2',  # FictionBook
        'lit',  # Microsoft Reader
        'pdb', 'pml', 'prc',  # Palm
        'chm',  # Microsoft Help
        'cbr', 'cbz',  # Comic books
        'rb',  # RocketBook
        'snb',  # Shanda Bambook
        'tcr',  # Text Compression for Reader
    }

    def __init__(self, calibre_path: Optional[str] = None):
        """
        Initialize Calibre converter.

        Args:
            calibre_path: Path to ebook-convert executable.
                         If None, assumes it's in PATH.
        """
        self.calibre_path = calibre_path or 'ebook-convert'
        self._check_calibre_available()

    def _check_calibre_available(self):
        """Check if Calibre's ebook-convert is available."""
        try:
            result = subprocess.run(
                [self.calibre_path, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                raise CalibreNotFoundError(
                    f"Calibre ebook-convert not working: {result.stderr}"
                )
        except FileNotFoundError:
            raise CalibreNotFoundError(
                "Calibre's ebook-convert not found. "
                "Please install Calibre: https://calibre-ebook.com/download"
            )
        except subprocess.TimeoutExpired:
            raise CalibreNotFoundError("Calibre ebook-convert timeout")

    def supports(self, file_path: Path) -> bool:
        """Check if file format can be converted by Calibre."""
        ext = file_path.suffix.lower()[1:]  # Remove dot
        return ext in self.CONVERTIBLE_FORMATS

    def convert_and_extract(
        self,
        file_path: Path,
        target_format: str = 'epub'
    ) -> ExtractedText:
        """
        Convert file to target format, then extract text.

        Args:
            file_path: Path to source file
            target_format: Target format (epub or pdf)

        Returns:
            ExtractedText object

        Raises:
            ConversionError: If conversion fails
        """
        if not self.supports(file_path):
            raise ConversionError(
                f"Format {file_path.suffix} not supported by Calibre"
            )

        if target_format not in ['epub', 'pdf']:
            raise ValueError("target_format must be 'epub' or 'pdf'")

        # Create temporary file for conversion output
        with tempfile.NamedTemporaryFile(
            suffix=f'.{target_format}',
            delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Run ebook-convert
            self._run_conversion(file_path, tmp_path, target_format)

            # Extract text from converted file
            if target_format == 'epub':
                extractor = EPUBExtractor()
            else:  # pdf
                extractor = PDFExtractor()

            result = extractor.extract(tmp_path)

            # Add note about conversion in metadata
            result.metadata.warnings.append(
                f"Converted from {file_path.suffix} to {target_format} using Calibre"
            )
            result.metadata.extraction_method = f"Calibre→{target_format}→{extractor.__class__.__name__}"

            # Update source file reference
            for chunk in result.chunks:
                if 'metadata' in chunk:
                    chunk['metadata']['source_file'] = str(file_path)
                    chunk['metadata']['format'] = file_path.suffix[1:]

            return result

        finally:
            # Clean up temporary file
            try:
                tmp_path.unlink()
            except Exception:
                pass

    def _run_conversion(
        self,
        input_path: Path,
        output_path: Path,
        target_format: str
    ):
        """
        Run Calibre's ebook-convert.

        Args:
            input_path: Source file
            output_path: Output file
            target_format: Target format

        Raises:
            ConversionError: If conversion fails
        """
        cmd = [
            self.calibre_path,
            str(input_path),
            str(output_path),
        ]

        # Add format-specific options
        if target_format == 'epub':
            cmd.extend([
                '--chapter', '/',  # Don't split on chapters
                '--max-toc-links', '0',  # Keep full TOC
                '--preserve-cover-aspect-ratio',
                # Never split by file size. The default of 260 KB exists for
                # e-reader compatibility; we only read the EPUB once. Books
                # whose markup offers no split point (long chapters without
                # block-level structure) otherwise abort with SplitError.
                '--flow-size', '0',
            ])
        elif target_format == 'pdf':
            cmd.extend([
                '--paper-size', 'a4',
                '--pdf-serif-family', 'Liberation Serif',
            ])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minutes max
            )

            if result.returncode != 0:
                raise ConversionError(
                    f"Calibre conversion failed:\n{result.stderr}"
                )

            if not output_path.exists():
                raise ConversionError("Conversion produced no output file")

        except subprocess.TimeoutExpired:
            raise ConversionError("Conversion timeout (5 minutes)")
        except Exception as e:
            raise ConversionError(f"Conversion error: {e}") from e

    # Formats best converted to PDF (scanned/image-based documents)
    _PDF_TARGET_FORMATS = {'djvu', 'djv'}

    @classmethod
    def get_optimal_target_format(cls, source_format: str) -> str:
        """
        Determine best target format for conversion.

        Args:
            source_format: Source format (without dot)

        Returns:
            'epub' or 'pdf'
        """
        if source_format.lower() in cls._PDF_TARGET_FORMATS:
            return 'pdf'
        return 'epub'
