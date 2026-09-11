# ARCHILLES — Product Roadmap

> **Your Intelligent Research Archive**
> *Mein Korpus, meine Wahl.*

**Last updated:** September 2026

> **Spiegel-Notiz (Familie), 2026-07-19:** Scriptor S1 ist umgesetzt — das
> Übergabeformat der Familie ist als versionierte Spezifikation normiert:
> [PREPARED_FORMAT_SPEC v0.1](https://github.com/kasssandr/archilles-scriptor/blob/main/docs/PREPARED_FORMAT_SPEC.md)
> (Seitenlabel-Syntax, Fußnoten-Anker, Sidecars, `<dnt>`, Zitations-Spans,
> Robustheitsregeln). Betrifft hier den künftigen Scriptor-Import: Der Pfad
> wird gegen die Spec getestet — Abnahme ist ein Scriptor-aufbereiteter Band,
> dessen Treffer gedruckte Seitenlabels zitieren (Spec §12). Benennungshinweis:
> „prepared" ist in Archilles bereits durch die Zwei-Phasen-Pipeline
> (prepare/embed) belegt; der Import sollte als „scriptor"-Format laufen.

> **Spiegel-Notiz (Familie), 2026-08-15:** Scriptor liefert zum Seitenlabel
> jetzt auch dessen **Herkunft** — `label_source` je Seite (`printed`, `link`,
> `toc`, `catalogue`, `computed`), eine Frontmatter-Zeile `pagination:` je Band
> und ein Sidecar `<master>.pagination.json` als maschinenlesbaren Kanal. Für
> den Scriptor-Import heißt das: `label_source` gehört ins Chunk-Schema, und ein
> unbekannter Wert darf den Importer nicht scheitern lassen. Der Kontrakt steht
> normativ in [WATCHDOG_AND_WIKI.md §II.6](WATCHDOG_AND_WIKI.md); Begründung und
> Messwerte in Scriptors `docs/internal/2026-08-13-quellen-verbund-design.md`.

> **Erledigt, 2026-09-11:** Beide Spiegel-Notizen sind umgesetzt (Naht
> Scriptor → Archilles, Schritte S1–S6; [ADR-032](DECISIONS.md)). Der Import
> läuft als Format `scriptor` über einen eigenen Extraktor; das Bündel ersetzt
> nur die Textquelle eines Buchs, nicht seine Identität. `label_source`,
> `region` und `producer_version` stehen im Chunk-Schema, unbekannte Werte
> werden nach den Toleranzregeln der Spec gelesen. Die Abnahme nach Spec §12 ist
> bestanden: Bauer [10593] zitiert eine Passage von S. 88 als `S. 88`,
> `label_source = printed`. Offen aus der Naht: OCR (S8/S9, siehe v1.2) und das
> Gliederungsmodell, in dem das gemeinsame Regionsvokabular für den EPUB-Pfad
> (S7) aufgeht.

---

## Vision

ARCHILLES ist die semantische Infrastrukturschicht zwischen bestehenden Bibliothekssystemen und LLM-Ökosystemen. Das übergeordnete Ziel: Tausende Titel — mitsamt den Verlagstexten, Kritiken, KI-Analysen (z.B. aus NotebookLM) sowie eigenen Exzerpten und Gedanken — per KI erschließen, durchsuchen und in Beziehung setzen.

ARCHILLES ist kein Second Brain, kein Konversations-Memory-System und kein Schreibwerkzeug. ARCHILLES löst ein spezifisches Problem, das kein anderes System adressiert: die semantische Tiefenerschließung heterogener Bibliotheken mit zitierfähigen Quellenangaben über das Model Context Protocol.

Dabei gibt es zwei komplementäre Zugangswege. Der eine führt über Calibres eigene KI-Schnittstellen (seit v8.16: GitHubAI, GoogleAI, OllamaAI, OpenRouter), die ein Gesprächsinterface für einzelne Bücher bieten. Der andere — und das ist ARCHILLES' Domäne — ermöglicht die semantische Suche über die gesamte Bibliothek mit verifizierbaren Quellenangaben, verbunden mit der analytischen Kraft eines Frontier-Modells wie Claude über das Model Context Protocol (MCP).

Die beiden Ansätze sind komplementär, nicht konkurrierend: Calibres AI-Features nutzen externes Wissen, um über einzelne Bücher zu sprechen. ARCHILLES durchsucht die tatsächlichen Inhalte der gesamten Bibliothek und liefert zitierfähige Quellenangaben.

---

## Positionierung: ARCHILLES im Ökosystem

### ARCHILLES und Calibre AI

Calibre 8.16 führte im Dezember 2025 eigene AI-Features ein. Systematische Tests zeigten die unterschiedlichen Stärken und Grenzen: Lokale Modelle (z.B. Gemma3 über Ollama) halluzinierten bei unvollständigen Dokumenten, während Cloud-Modelle (z.B. Gemini) sich auf Web-Grounding stützten und bei unveröffentlichten Manuskripten versagten. ARCHILLES löst ein fundamental anderes Problem — nicht einzelne Bücher besprechen, sondern die gesamte Bibliothek semantisch erschließen.

Für Nutzer empfiehlt sich die Kombination: Calibres Ollama-Integration für schnelle Einzelbuch-Gespräche, ARCHILLES über MCP für bibliotheksweite Recherche mit Frontier-Modellen.

### ARCHILLES und Second-Brain-Systeme

Im Frühjahr 2026 entsteht ein lebhaftes Ökosystem für KI-gestütztes persönliches Wissensmanagement: MemPalace (Konversations-Memory mit räumlicher Metapher), Claudian (agentisches Schreiben in Obsidian), Nate B. Jones' Open Brain (MCP-basierte Supabase-Schicht), und weitere. Diese Systeme lösen ein reales Problem — das Verschwinden von Entscheidungskontext aus Chat-Sessions — und tun das zunehmend gut.

ARCHILLES konkurriert nicht mit diesen Systemen, sondern ergänzt sie. Die strategische Abgrenzung:

| | Second-Brain-Systeme | ARCHILLES |
|---|---|---|
| **Speichert** | Konversationen, Notizen, Gedanken | Bücher, Artikel, Annotationen |
| **Optimiert auf** | "Erinnere dich an alles, was ich gesagt habe" | "Finde die relevante Stelle in 3.000 Büchern" |
| **Quellenangaben** | Konversationsfragmente | Exakte Zitationen mit Seitenangaben |
| **Kernproblem** | KI-Amnesie | Bibliothekserschließung |

Die Adapter-Architektur (ADR-021) und die MCP-Schnittstelle machen ARCHILLES zur natürlichen Bibliotheks-Anbindung für jedes dieser Systeme: MemPalace für das Konversationsgedächtnis, ARCHILLES für den Zugriff auf den Forschungsbestand. Die MCP-Tools sind die API, über die jedes Second-Brain-System an die Bibliothek andocken kann.

**Kommunikation:** "Bring your own library. ARCHILLES makes it searchable." — "Read your library. Remember your research."

### Zugangswege und Provider-Unabhängigkeit

Mitte 2026 hat sich das Model Context Protocol als herstellerübergreifender Standard konsolidiert (Anthropic, OpenAI, Google, Microsoft). Damit ist die *Protokoll*-Frage entschieden — offen bleibt die *Zugangs*-Frage: Claude Desktop bindet lokale MCP-Server erstklassig ein; andere Consumer-Apps gaten den Zugang oder drängen zu gehosteten Endpunkten. ARCHILLES beantwortet das mit zwei bereits gebauten Wegen und einem dritten, bewusst dünnen:

Erstens **MCP-stdio** für Claude Desktop als Referenzweg. Zweitens der **HTTP/SSE-Transport** (seit April 2026) für ChatGPT Desktop, Cursor, Codex und künftige Clients — er liegt bereit und wird aktiviert, sobald die jeweiligen Apps lokalen Zugang tatsächlich öffnen. Drittens ein **direkter Frage-Pfad** (CLI): Retrieval-Ergebnis plus Prompt gegen einen beliebigen OpenAI-kompatiblen Endpunkt — Cloud-API oder lokales Modell via Ollama. Dieser Pfad ist kein Produkt-Frontend und wird keines (die Grenze aus ADR-022 gilt); er ist der Nachweis, dass der Zugang zum eigenen Bibliothekswissen an keinem einzelnen Anbieter hängt.

Die ehrliche Formel der Positionierung lautet: **Daten und Index bleiben lokal; das Reasoning geht per bewusster Wahl in die Cloud — oder, wenn es sein muss, an ein lokales Modell.** „Mein Korpus, meine Wahl" meint beides: die Wahl des Anbieters und die Freiheit, keinen zu brauchen.

---

## Aktueller Stand: v0.9 (Juni 2026)

**Status:** Kernfunktionalität produktionsreif, MCP-Server operativ.

Die Basis steht. Der gesamte Bibliotheksbestand ist indexiert; das System skaliert über die LanceDB-Architektur auf Millionen von Chunks. Der nächste Qualitätssprung ist die produktive Aktivierung von Parent-Child-Abhängigkeiten im Index: Die Mechanik ist end-to-end verdrahtet und gegen Echtdaten validiert (`--hierarchical`-Flag, Hierarchie aus strukturbewussten Chunks via `_group_chunks_hierarchically()`, Parent-Kontext im Retrieval; siehe ADR-027), bleibt aber standardmäßig deaktiviert — ein koordinierter Reindex über externes Embedding ist in Arbeit. Buchinhalte und Nutzerdaten (Kommentare, Annotationen, NotebookLM-Analysen, eigene Exzerpte) liegen seit der ChromaDB→LanceDB-Migration (Februar 2026) in **einer** LanceDB-`chunks`-Tabelle; die konzeptionelle Trennung läuft über das `chunk_type`-Feld, nicht über getrennte Datenbanken (siehe ADR-008/ADR-012).

**Abgeschlossen:**

Volltextindexierung über 30+ Formate via Calibre-Converter. Semantische Suche mit BGE-M3-Embeddings (multilingual, 75+ Sprachen). Keyword-Suche über BM25. Hybride Suche mit Reciprocal Rank Fusion. Calibre-Metadaten-Integration einschließlich Tags, Comments (mit HTML-Cleaning) und automatischer Custom-Field-Erkennung. Annotationsextraktion aus dem Calibre E-book Viewer (Highlights, Notes, Bookmarks). LanceDB als Vektordatenbank mit nativer Hybrid-Search und IVF-PQ-Indexing. Konsolidierte Speicher-Architektur: Mit der Migration von ChromaDB zu LanceDB (Februar 2026) nutzen Buchinhalte, Annotationen und Kommentare einheitlich BGE-M3-Embeddings in **einer** LanceDB-`chunks`-Tabelle, getrennt über das `chunk_type`-Feld — eine einzige Vektor-DB-Engine, keine externe Dependency mehr. Service-Layer-Architektur (`ArchillesService`) als zentrale Geschäftslogik-Fassade für MCP-Server, Web UI und CLI. Cross-Encoder Reranking (optional, BAAI/bge-reranker-v2-m3). MCP-Server mit 13 Tools für Claude Desktop und andere MCP-kompatible Clients. Bibliographie-Export in BibTeX, RIS, EndNote, JSON und CSV. Duplikaterkennung nach Titel+Autor, ISBN oder exaktem Titel. Streamlit-basiertes Web UI als Companion-Interface. Batch-Indexierung mit Tag-/Autoren-Filtern, Checkpoint-Resume und Hardware-Profilen.

**Neu in v0.9 (März–April 2026):**

Source Adapters: Neben Calibre werden jetzt auch Zotero-Bibliotheken, Obsidian-Vaults und einfache Ordnerstrukturen als Quellen unterstützt (`CalibreAdapter`, `ZoteroAdapter`, `ObsidianAdapter`, `FolderAdapter`). Structure-Aware PDF Chunking: PDF-Chunks tragen jetzt `chapter` und `section_title` aus dem TOC, mit Junk-TOC-Filterung für Scanner-PDFs. Running-Footer-Erkennung und -Entfernung für sauberere PDF-Chunks. EPUB-section_type-Fix: Klassifikation basiert nur noch auf semantischen Titeln, nicht auf Dateinamen. DialogueChunker für Chat-/Q&A-Exporte (ChatGPT, Gemini, Grok, NotebookLM). SemanticChunker verbessert: Markdown-Heading-Erkennung (H1/H2 erzwingen Chunk-Breaks), Splitting überlanger Absätze. Chunk Inspector als Diagnosetool (`scripts/chunk_inspector.py`). TXT-Extractor mit YAML-Frontmatter-Stripping für Obsidian-Dateien.

Annotation-Import-System: Provider-basierte Architektur für den Import von Annotations (Highlights, Notizen, Lesezeichen) aus externen Leseumgebungen. Drei Provider implementiert (PDF, Calibre Viewer, Kindle `My Clippings.txt`). Book-Matcher für fuzzy Titel+Autor-Zuordnung zur Calibre-Bibliothek (rapidfuzz). CLI-Command `import-annotations` mit `--dry-run`. Detailplan: [docs/plans/2026-04-06-annotation-import.md](plans/2026-04-06-annotation-import.md).

HTTP/SSE-Transport (April 2026): `mcp_server.py` unterstützt jetzt beide MCP-Transports. `--transport stdio` (Default, unverändert für Claude Desktop) und `--transport sse` für ChatGPT Desktop, OpenAI Codex, Cursor und andere HTTP-basierte Clients. Host, Port und optionaler Bearer-Token konfigurierbar via CLI oder `config.json`-Block `transport`. Zwei parallele Instanzen auf verschiedenen Ports möglich. 11 Tests. Dokumentiert in [MCP Integration Guide](MCP_GUIDE.md).

Scheduled Routines (Mai 2026): Eine Orchestrierungsschicht über die bestehenden Indexierungs-Tools, die für unbeaufsichtigten Multi-Source-Betrieb sorgt. `scripts/run_routine.py` ruft pro Source das passende Tool — `watchdog.py --json` für Calibre, `batch_index --all --skip-existing` für Zotero und Obsidian/Folder — und drosselt sich selbst über Marker-Dateien (täglich oder wöchentlich). `scripts/run_link_vault.py` führt den Vault-Cross-Linker als monatliche Maintenance, hart gegated darauf, dass die Lab-Routine am selben Tag erfolgreich abgeschlossen ist. `scripts/weekly_status_mail.py` versendet wöchentlich einen Plaintext-Digest aller Routine-Läufe per Gmail SMTP. Fünf Windows-Scheduler-Tasks werden idempotent durch `scripts/install_scheduled_routines.ps1` registriert. Begründung und Abgrenzung zur (vertagten) Watchdog-Generalisierung: [ADR-025](DECISIONS.md#adr-025-scheduled-routines--pragmatischer-schritt-a-vor-watchdog-generalisierung-mai-2026).

---

## v1.0 — Beweisbare Qualität und Konsolidierung (Ziel: Q3 2026)

**Fokus:** Die Retrieval-Qualität messbar machen, die Annotation-Engine vollenden, das Fundament für den Community-Release legen.

Die verbleibende Arbeit für v1.0 betrifft weniger neue Features als Konsolidierung — mit einer Ausnahme, die an die erste Stelle rückt:

**Benchmark-Harness für Bibliotheks-Retrieval (erste Priorität):** ARCHILLES braucht quantitative Belege für seine Retrieval-Qualität. Nicht LongMemEval (misst Konversations-Memory), sondern ein eigenes, reproduzierbares Benchmark auf dem eigenen Problemraum: Precision/Recall über heterogene Bibliotheksbestände, Annotation-Retrieval, Mehrsprachigkeit, Citation-Accuracy. Das Harness ist bewusst minimal gehalten (versioniertes Goldset im JSONL-Format, Metrik-Modul, A/B-Runner — siehe ADR-030) und erfüllt eine Doppelrolle: Es ist die externe Kommunikation des Differenzierers *und* das interne Messinstrument, das die Parent-Child-Entscheidung (v1.1) empirisch statt spekulativ macht. Das Goldset ist Kurationsarbeit des Nutzers und wächst inkrementell; das Harness läuft ab dem ersten Dutzend Fälle. Veröffentlichung vor dem Community-Release.

**Annotation-Import: Verankerung und Kontextanreicherung (Phase 5):** Annotations sind derzeit kontextlose Inseln — ein Kindle-Highlight enthält den markierten Text, aber nicht das Kapitel, den Argumentationsgang oder den umgebenden Absatz. Phase 5 verknüpft Annotations mit den Content-Chunks des annotierten Buchs: `anchor_chunk_id` verweist auf den Chunk mit dem größten Textüberlapp, das Embedding wird mit Kapitel/Seite/Kontext aus dem Anchor-Chunk angereichert, und bei der Suche wird der Anchor-Chunk automatisch mitgeliefert. Kobo-Provider als weitere Quelle. Die Annotation-Engine ist der Teil des Stacks, den sonst niemand baut (ADR-022) — sie bleibt auf dem kritischen Pfad. Detailplan: [docs/plans/2026-04-06-annotation-import.md](plans/2026-04-06-annotation-import.md).

**Direkter Frage-Pfad (klein):** Ein CLI-Kommando, das ein Retrieval-Ergebnis samt `PromptBuilder`-Prompt an einen konfigurierbaren OpenAI-kompatiblen Endpunkt sendet (Cloud oder Ollama). Kein Frontend, keine UI — eine dünne Versicherung gegen faktische Einzel-Anbieter-Abhängigkeit (siehe Positionierung).

**Calibre Watchdog (umgesetzt April 2026):** Automatische Synchronisation zwischen Calibre und LanceDB. Ein periodischer Scan-Prozess erkennt drei Änderungstypen: Metadaten-Änderungen (Comments, Tags, Rating — via `metadata_hash`-Vergleich, ADR-011), Annotations-Änderungen (via `annotation_hash`), und neu hinzugekommene Titel. Metadaten- und Annotations-Updates werden sofort via `index_book()` ausgeführt (~1–3s pro Buch); neue Bücher werden in eine Index-Queue geschrieben. Der Watchdog ist kein Daemon, sondern ein idempotenter Scan, aufrufbar via `scripts/watchdog.py` oder Windows Task Scheduler. Er ist die Voraussetzung für alle nachgelagerten Features, die auf aktuellem LanceDB-Bestand aufbauen. Spezifikation: [WATCHDOG_AND_WIKI.md](WATCHDOG_AND_WIKI.md).

**Watchdog-Generalisierung für Zotero und Obsidian (Schritt B — nach v1.0 verschoben):** Der praktische Bedarf ist seit Mai 2026 durch die Scheduled Routines gedeckt (ADR-025): Lab und Zotero werden über `batch_index --all --skip-existing` unbeaufsichtigt aktuell gehalten. Schritt B — `WatchdogScanner` adapter-agnostisch, `watchdog_scan` aus `_CALIBRE_ONLY_TOOLS` heraus, „Jetzt-indexieren"-Knopf in den Frontends — bleibt sinnvoll, ist aber Komfort, keine Adoptionsbedingung. Er wandert hinter Benchmark und Annotation Phase 5.

Weitere geplante Arbeiten: Umfassende Dokumentation einschließlich Installationsanleitung, Konfigurationsreferenz und Troubleshooting. Robuster Installationsprozess für Nutzer, die keine Entwickler sind.

---

## v1.1 — Chunking-Intelligenz und Retrieval-Qualität (Q2 2026)

**Fokus:** Die Qualität der Suchergebnisse substantiell verbessern.

**Teilweise umgesetzt (März 2026):** Structure-Aware Chunking ist implementiert — PDF-Chunks tragen jetzt `chapter`/`section_title` aus dem TOC, der SemanticChunker erkennt Markdown-Headings und erzwingt Chunk-Breaks an Kapitelgrenzen. Der DialogueChunker behandelt Chat-Exporte korrekt. Running-Footer-Entfernung verbessert die Chunk-Qualität bei PDFs. Die verbleibende Arbeit betrifft fortgeschrittene Chunking-Strategien.

**Small-to-Big Retrieval und Parent-Child-Hierarchien:** Indexiere kleine Chunks (Absatzebene) für hohe Retrieval-Präzision, liefere dem LLM aber den größeren Kontext (Kapitel oder erweiterte Passage). Das löst das Kernproblem, das Geisteswissenschaftler an RAG-Systemen frustriert: Sätze, die mitten im Argument abreißen. Bereits einfaches Recursive Hierarchical Chunking bringt ca. 80% des Qualitätsgewinns gegenüber flachem Chunking. **Stand Juni 2026:** Die Mechanik ist end-to-end implementiert. Eine Validierung (17.06.) deckte auf, dass der frühere `full_text`-Re-Chunking-Pfad die Struktur-/Seiten-Metadaten der `child`-Chunks verlor (nicht zitierfähig); seither baut `_group_chunks_hierarchically()` die Hierarchie aus den strukturbewusst extrahierten Chunks — Children erben Metadaten/Offsets, aufeinanderfolgende Children einer Sektion werden zu ~2048-Token-Parents gruppiert — mit `parent_id`/`window_text`. Das `--hierarchical`-Flag reicht sie durch Service und Indexer, und das Retrieval liefert bei vorhandenem `parent_id` den Parent-Chunk als Kontext. Was aussteht, ist nicht mehr die Implementierung, sondern die *Entscheidung und Aktivierung* — und die fällt gegen Messung, nicht gegen Intuition (ADR-030): Das Benchmark-Harness (v1.0) misst zuerst die flache Baseline, dann einen hierarchisch indexierten Teilbestand. Erst wenn der Qualitätsgewinn die ~25–30 % Mehr-Vektoren der Parent-Ebene rechtfertigt — und der in der Validierung vom 17.06. identifizierte tote `parent_id`-Pfad entweder produktiv genutzt oder die Parent-Ebene verschlankt wird —, folgt die Default-Schaltung und der hierarchische Reindex des Bestands. Dieser „Parent-Child-Refresh" wird mit drei weiteren anstehenden Reindex-Anlässen gebündelt (Duplikat-Bereinigung, i18n-Index-Präfixe, deutsche EPUB-Sektionserkennung) — als ein koordinierter Lauf, nicht vier Einzelaktionen. Die VRAM-Messung am 4-GB-Gerät entscheidet lokal vs. extern für diesen Lauf (ADR-028: `full-external`).

**Semantic-Hybrid-Chunking (Upgrade-Pfad):** Kombiniert semantisches Splitting via Embedding-Ähnlichkeit mit Agglomerative Clustering und dynamischen Thresholds, die sich automatisch pro Buch anpassen. Weitere 20–30% Qualitätsgewinn, aber signifikant mehr Implementierungsaufwand — daher als optionaler Upgrade-Pfad nach der Parent-Child-Grundlage.

**Beitragsautoren in Sammelbänden (Tiefenerschließung):** Ein Sammelband ist in Calibre ein Eintrag mit einem oder zwei Herausgebern. Die Verfasser der einzelnen Beiträge — bei geisteswissenschaftlichen Beständen ein erheblicher Teil dessen, was am Ende tatsächlich zitiert wird — stehen in keinem Metadatenfeld. `list_books_by_author` findet sie deshalb nicht, und eine Volltextsuche nach dem Namen liefert vor allem Literaturverzeichnisse: Wer oft zitiert wird, erscheint hundertfach als Fußnote und einmal als Verfasser. Das Ziel ist die Umkehrung — den eigenständigen Text finden, nicht die Erwähnung.

Vorgesehen ist ein Chunk-Feld `contributor` (mit `contribution_title` und der Seitenspanne des Beitrags). Gefüllt wird es dort, wo die Zuschreibung **erklärt** ist, nicht wo sie sich aus dem Drucksatz erraten lässt:

1. **Zotero `bookSection` — deklariert.** Ein so erfasster Beitrag trägt Verfasser, Beitragstitel und im Feld `pages` die Seitenspanne; der Adapter liest Creator-Typen bereits aus, und `bookSection` steht nicht auf der Ausschlussliste. Wer seinen Band in Zotero derart erschlossen hat, hat das Inhaltsverzeichnis von Hand digitalisiert — es ist nichts zu erkennen, nur zu verknüpfen. Der Ausbau besteht darin, die Beziehung Beitrag → Band aufzulösen und die Seitenspanne auf den Volltext des Bandes zu legen, auch wenn das PDF am Band hängt und nicht am Beitrag. Höchste Verlässlichkeit, kein Modell im Spiel.
2. **Das gedruckte Inhaltsverzeichnis — die primäre Erkennung.** Das TOC eines Sammelbandes ist per Konstruktion die vollständige Tabelle Verfasser → Titel → Anfangsseite. Entscheidend ist nicht der einzelne Name, sondern das **wiederholte Muster**: Treten über einen TOC-Block hinweg regelmäßig Personennamen neben Titel und Seitenzahl auf, ist die Sammelbandstruktur belegt und alle Zeilen werden gemeinsam ausgewertet — inklusive der Beitragsgrenzen, die sich aus der Anfangsseite des jeweils folgenden Eintrags ergeben. Zu parsen ist dabei das *gedruckte* Verzeichnis, nicht das PDF-Outline: Lesezeichen führen in aller Regel nur Titel. Scriptors `reflow/toc.py` ist der Anknüpfungspunkt.
3. **Der Beitragsanfang — Bestätigung und Rückfallebene.** Am Kapitelbeginn steht der Verfasser abgesetzt bei oder unter dem Beitragstitel. Wo TOC und Beitragskopf übereinstimmen, ist die Zuschreibung doppelt belegt; wo ein Verzeichnis fehlt oder unbrauchbar gesetzt ist, trägt der Kopf allein. Ein schema-beschränkter Kurzaufruf über die ersten Zeilen eines Kapitels genügt dafür — der Zuschnitt, bei dem auch kleine Modelle zuverlässig arbeiten.

Laufende Kolumnentitel bleiben ausdrücklich **keine** eigene Quelle. Sie führen den Verfasser oft, aber nicht verlässlich, und eine Erschließung mit halber Trefferquote ist schlimmer als keine: Man weiß hinterher nicht, ob ein fehlender Name Abwesenheit oder Erkennungsversagen bedeutet.

**Tags als Hinweis, nicht als Bedingung.** `Sammelband`, `Tagungsband`, `Festschrift` (im Englischen als Fremdwort ebenso in Gebrauch) und ihre englischen Entsprechungen priorisieren, welche Bände zuerst durchlaufen werden. Sie entscheiden nichts: Ein untaggter Band mit klarem TOC-Muster wird erschlossen, ein getaggter ohne Muster nicht — vermutet wird nie.

**Herausgeber sind auch Verfasser.** Wer den Band herausgibt und darin ein Geleitwort, eine Einleitung oder einen eigenen Beitrag schreibt, wird für diesen Text als `contributor` geführt, obwohl sein Name bereits im `author`-Feld des Bandes steht. Die Felder bedeuten Verschiedenes — Verantwortung für den Band gegenüber Verfasserschaft eines Textes — und nur das zweite macht die Einleitung als eigenständigen Text auffindbar. Trägt das Verzeichnis bei „Vorwort" keinen Namen, ist der Herausgeber ein Kandidat mit niedrigerer Konfidenz, keine gesetzte Zuschreibung.

Abgerufen wird das **als Feldsuche, nicht als Volltextsuche** — ein `include_contributions` an `list_books_by_author` oder ein eigenes Tool. Genau diese Trennung löst das Rauschproblem strukturell: Ein Feld enthält den Verfasser oder nicht; Fußnoten und Bibliographien liegen ohnehin außerhalb von `section_type == "main"`. Wie überall gilt die Scriptor-Disziplin: Kandidaten mit Konfidenz, keine stille Behauptung — ein falsch zugeschriebener Beitrag ist teurer als ein fehlender.

**Embedding-Evaluation:** Vergleich von BGE-M3 mit multilingual-e5 und jina-embeddings-v3. Domain-spezifische Optimierungsmöglichkeiten evaluieren.

---

## v1.2 — OCR und erweiterte Formate (Q3 2026)

**Fokus:** Gescannte PDFs und historische Dokumente erschließen.

Viele geisteswissenschaftliche Bibliotheken enthalten gescannte PDFs — ältere Fachbücher, Dissertationen, historische Quelleneditionen. Ohne OCR bleiben diese unsichtbar.

Für gewöhnlich bringt der größere Teil der PDFs eines gewachsenen Bestands jedoch bereits einen Textlayer mit, gesetzt von FineReader, Acrobat Paper Capture, LuraDocument oder Google Books. Der Regelfall ist deshalb **kein OCR-Problem**, sondern die Frage, wie gut dieser fremde Layer ist. Drei Zustände sind zu unterscheiden, und nur der dritte verlangt eine Engine:

1. **Brauchbarer Textlayer.** Er wird gelesen, nicht ersetzt. Nachmessung an neun Bänden (Juli 2026) zeigte, dass Bibliotheken hier stillschweigend Schaden anrichten: PyMuPDF4LLM erkennt eine Scanseite mit sichtbarem Textlayer, OCRt das Seitenbild zusätzlich und liefert jeden Absatz doppelt; bei unsichtbarem Layer löscht es ihn und liest ihn neu — mit der Standardsprache, über einen französischen Text. Scriptor liest den Layer seit Juli 2026 direkt über `page.get_text("dict")` und OCRt nie von sich aus.
2. **Schlechter, aber vorhandener Textlayer.** Erneutes OCR ist die Ausnahme, nicht die Regel — es lohnt bei einzelnen Bänden, nicht beim Erschließen einer Bibliothek.
3. **Kein Textlayer.** Hier, und nur hier, wird eine Engine gebraucht.

**Die Ausgabe entscheidet die Engine, nicht die Zeichengenauigkeit.** Was Archilles von einem OCR-Backend braucht, ist nicht Markdown, sondern Messwerte: Bounding Boxes je Zeile und, wo möglich, Konfidenz je Zeichen. Ohne Boxen lassen sich die Druckzeilen nicht rekonstruieren, die manche Textlayer in Wortfragmente zerlegen; ohne Seitenzahl kein `page_label`, und damit kein zitierfähiger Anker (siehe [WATCHDOG_AND_WIKI.md](WATCHDOG_AND_WIKI.md) §II.5). Genau diese Messwerte liefert das Seitenmodell, das Scriptor im Juli 2026 als Backend-Schnittstelle definiert hat (`SourcePage`/`Line`/`Span`, eine JSON-Datei pro Seite). Sie wird einmal entworfen und in beiden Repos benutzt.

**Tesseract (primär):** hOCR liefert Boxen und Konfidenzen, lokal, schnell, ressourcenschonend — und als einzige der verbreiteten Engines direkt auf das Seitenmodell abbildbar. Deutsch und Englisch tragen den Bestand; `fra` folgt mit deutlichem Abstand und ist dennoch Bedingung, weil dort Kernquellen liegen, nicht Beiwerk. Italienisch und Spanisch sind ein Modell-Download, kein Architekturthema. Griechisch, Hebräisch und Latein erscheinen fast nie als eigene Bände, sondern als Zitatpassagen innerhalb deutscher und englischer Werke — sie verlangen keine andere Engine, sondern allenfalls Tesseracts Mehrsprachenmodus, und Hebräisch zusätzlich die RTL-Behandlung, die Scriptor aus dem Zuckerman-Referenzfall bereits kennt. Fraktur (`deu_latf` aus `tessdata_best`) ist ebenfalls ein Modell-Download, kein Architekturargument: nice to have, und kein Grund, die Engine-Wahl daran auszurichten.

**Vision-Language-OCR (Sonderfall, nicht Primärpfad):** Modelle wie olmOCR-2 oder LightOnOCR-2 lesen Layout und Lesereihenfolge beeindruckend gut — und liefern Markdown. Keine Boxen, keine Zeichenkonfidenz. olmOCR-2 führt olmOCR-Bench mit 82,4 Punkten, fällt auf allgemeinen historischen Scans aber auf 47,7 %; und der Benchmark prüft in der Kategorie *Headers & Footers* ausdrücklich, ob Kopf- und Fußzeilen im Ergebnis **fehlen**. Dort steht die Seitenzahl. Was die Benchmark-Konstruktion belohnt, kostet Archilles den Zitationsanker. VLM-OCR bleibt deshalb der Kanal für Bände, die anders keinen lesbaren Text hergeben — mit dem ausdrücklichen Vermerk, dass Seitenanker und Fußnotenmarker dabei verloren gehen.

Die `ocr_backend`-Konfiguration des `ArchillesService` (heute `auto/tesseract/lighton/olmocr`) spiegelt noch die frühere Annahme und wird mit der Implementierung nachgezogen.

**Umsetzung über die Scriptor-Naht (Stand September 2026).** Die Engine wird nur einmal gebaut, in Scriptor. Der Zuschnitt ist gemessen (Messung M5 an drei gescannten Bänden).
- **S8 (Scriptor):** Das Tesseract-Backend schreibt das Seitenmodell über hOCR, mit Zeilenklassen und der Schriftgröße je Zeile (`x_size`; die Wortboxhöhe verfehlt zwei Drittel der Fußnoten). Fehlende Sprachpakete meldet es laut, denn Tesseract lässt sie sonst still weg.
- **S9 (Archilles):** `scriptor_prepare.py` erkennt Scans und reicht sie mit der Calibre-Sprache des Buchs an Scriptor weiter. Start sind die rund 173 Bände ohne Textlayer, eine Nacht CPU.
- **Danach:** Der Tesseract-Klartextpfad in Archilles wird eingefroren, die VLM-Platzhalter und die Konfigwerte `lighton`/`olmocr` entfallen.

Gewinn: Ein gescannter Band zitiert gedruckte Seitenlabels statt physischer Nummern, und `label_source` sagt, welche bezeugt sind.

Die strategische Entscheidung: Die OCR-Landschaft entwickelt sich rasant, die Anforderung an ihre *Ausgabe* nicht. Die Schnittstelle wird sauber definiert — als Seitenmodell, nicht als Markdown-Kanal — und das beste Modell zum Implementierungszeitpunkt dahinter gehängt.

---

## v1.3 — Archilles Lab als Referenzintegration (Q3–Q4 2026)

**Fokus:** Zeigen, wie ein Second-Brain-System an ARCHILLES andockt — nicht ein eigenes bauen.

Das Archilles Lab (Obsidian-Vault über den ObsidianAdapter) bleibt als funktionale Referenzintegration erhalten. Es demonstriert den kanonischen Pfad: ein Obsidian-Vault mit KI-Chats, Exzerpten und Forschungsnotizen wird über den Folder-/ObsidianAdapter indexiert und via MCP durchsuchbar gemacht. Die bestehenden Import-Pipelines (ChatGPT, Gemini, Grok) und der DialogueChunker bedienen diesen Pfad bereits.

**Was das Lab ist:** Die Referenzimplementierung, die zeigt, wie ARCHILLES als Bibliotheks-Layer für beliebige Wissensmanagement-Systeme dient. Dokumentation und Tutorials für Drittanbieter-Anbindung.

**Was das Lab nicht ist:** Ein eigenständiges Second-Brain-Produkt. Die in der TWO_DB_VISION.md skizzierten Schreib-Tools (`add_note`, `link_insight`, `save_chat_excerpt`) und das Corpus callosum (Cross-Search-Brücke) sind weiterhin als Erweiterung denkbar, stehen aber nicht auf dem kritischen Pfad. Sie werden implementiert, wenn die Kern-Pipeline (Adapter, Annotation-Verankerung, HTTP/SSE, Benchmark) steht und die Community danach fragt.

---

## v1.5 — Community-Release und Open Source (Q4 2026)

**Fokus:** ARCHILLES in die Hände der Zielgruppe bringen.

Open-Source-Veröffentlichung unter MIT-Lizenz. Domains sind gesichert (archilles.org, archilles.net, archilles.de). Die Zielgruppe sind technisch versierte Einzelforscher aus den Geisteswissenschaften — Geschichte, Literatur, Philosophie —, die große, kuratierte Calibre-Bibliotheken pflegen und Wert auf Privacy und lokale Datenkontrolle legen.

**Wiki-Generator (neu):** Ein LLM-gestützter Generator, der aus dem indexierten Bestand ein navigierbares Markdown-Wiki destilliert. Pro Buch (ab konfigurierbarem Rating-Schwellwert) werden Metadaten, Comments, Annotationen und die semantisch dichtesten Content-Chunks zusammengeführt und vom LLM in strukturierte Buch-Seiten, Entitäten-Seiten (Personen, Konzepte, Orte) und Themen-Seiten mit `[[Wikilinks]]` destilliert. Obsidian-kompatibel, aber nicht Obsidian-abhängig. Inkrementelle Updates via Watchdog-Änderungsprotokoll. Der Wiki ist ein leichtgewichtiger Wissensgraph in Textform — die menschenlesbare Vorstufe des formalen Graph RAG in v2.0. Spezifikation: [WATCHDOG_AND_WIKI.md](WATCHDOG_AND_WIKI.md).

Das Architekturmuster ist inzwischen von außen bestätigt: Anthropics Claude Science (Juni 2026) setzt für die Lebenswissenschaften dieselbe Zwischenschicht ein — Sub-Agenten extrahieren aus tausenden Papers zentrale Claims und Kernbefunde in eine *evidence state database*, aus der Reviews sektionsweise generiert und von einem Reviewer-Agenten auf Zitattreue geprüft werden (Referenzfall: Lecoq/Allen Institute, Reviews in einem Zehntel der bisherigen Zeit). Der Wiki ist das geisteswissenschaftliche Pendant dieser Schicht: strukturierte, quellenverankerte Destillate zwischen Retrieval und Prosa. Daraus folgen zwei Anforderungen an die Spezifikation. Erstens trägt jede destillierte Aussage ihren Beleganker (`chunk_id`, Seite) — Auditierbarkeit ist Gattungsmerkmal, nicht Komfort; was in den Naturwissenschaften Reproduzierbarkeit heißt, heißt hier Belegbarkeit. Zweitens ein optionaler Verifikationspass nach der Generierung: Ein zweiter LLM-Durchlauf prüft gegen den LanceDB-Index, ob zitierte Passagen tatsächlich an der behaupteten Stelle stehen (Actor-Critic-Muster; zugleich Vorstufe eines allgemeinen `verify_citation`-MCP-Tools). Als quellenverankerte Claim-Sammlung ist der Wiki außerdem das natürliche Substrat für die Behandlung widersprüchlicher Quellen in v2.0.

Community-Aufbau über akademische Kanäle: r/DigitalHumanities, r/AskHistorians, GitHub Discussions, DH-Discord-Server und spezialisierte Foren (MobileRead als Priorität wegen der Calibre-Community). Der ARCHILLATOR (Browser-basierter akademischer Textübersetzer) dient als Lead Magnet.

Freemium-Modell: Kostenlose Basisversion ohne Bibliotheksbeschränkung. Premium-Features und disziplinspezifische Erweiterungen (Special Editions) finanzieren die Weiterentwicklung.

---

## v2.0 — Graph RAG und Wissensvernetzung (2027)

**Fokus:** Vom Suchen zum Verstehen — Beziehungen zwischen Entitäten, Ideen und Texten sichtbar machen.

Hier wird ARCHILLES mehr als ein Suchwerkzeug. Graph RAG ermöglicht die Extraktion von Entitäten (Personen, Orte, Konzepte, Ereignisse), das Mapping ihrer Beziehungen und die Visualisierung als Netzwerkgraphen und Zeitleisten. Für Historiker bedeutet das: Prosopographie über die gesamte Bibliothek — wer kannte wen, wer zitierte wen, welche Ideen wanderten wohin.

**Evaluierung:** LightRAG als potenzielles Backend, Wikidata und Wikipedia als Seed-Quellen für Entity-Disambiguierung. Evaluation geplant für Q2 2026, Implementation frühestens 2027.

---

## Special Editions (ab v2.0)

Disziplinspezifische Erweiterungen als kostenpflichtige Add-ons, die die Weiterentwicklung finanzieren:

**Historical Edition:** Zeitleisten-Visualisierung, Prosopographie (Personennetzwerke), chronologiebewusste Suche, Primärquellenverarbeitung, mittelalterliche Datierungssysteme.

**Literary Edition:** Motivverfolgung, intertextuelle Verbindungen, Erzählstrukturanalyse, Figurennetzwerke, stilometrische Werkzeuge.

**Legal Edition:** Zitationsnetzwerke, Präzedenzfall-Tracking, jurisdiktionsbewusste Suche, Fallrecht-Verarbeitung.

**Musical Edition:** Partituranalyse-Integration, musiktheoretische Terminologie, Komponistennetzwerke, epochenbewusste Suche.

Detaillierte Pläne: siehe [EDITIONS.md](EDITIONS.md).

---

## Langfristiger Horizont

**Multi-Library-Support / Unified MCP Server:** Verwaltung mehrerer Bibliotheken (Calibre, Zotero, Obsidian) in einem einzigen MCP-Server, bibliotheksübergreifende Suche, gemeinsames Dokumentenmodell mit `calibre_id` und `zotero_key`. Darauf aufbauend: Zotero als Matching-Brücke für den Annotation-Import (ISBN/DOI-basiertes Matching, ZoteroAnnotationProvider).

**DEVONthink-Adapter (vertagt, mit definierten Wiedereintritts-Bedingungen):** DEVONthink liefert seit Version 4.3 („Herschel") einen nativen MCP-Server und deckt damit den Kern-Anwendungsfall im eigenen Ökosystem selbst ab. Ein externer Adapter wäre zugleich die teuerste Stelle, um Untestbarkeits-Risiko auszugeben (Mac-only, kein Testgerät, keine Live-Bibliothek). ARCHILLES' Heimrevier ist stattdessen **Windows und Linux** — Plattformen, auf denen es kein DEVONthink gibt — plus die Fähigkeit, die DEVONthinks MCP-Server nicht hat: **bibliotheksübergreifende Suche** über Calibre, Zotero und Obsidian in einem Index mit seitengenauen Zitationen. Wiedereintritts-Signale (Review: Q1 2027): dokumentierte Unzufriedenheit der DEVONthink-Community mit Retrieval-Qualität oder Zitationspräzision des nativen Servers; Inbound-Nachfragen nach einem Adapter; verfügbare Mac-Hardware und ein Beta-Tester. Falls Wiedereintritt: ausschließlich der Datei-Format-Pfad (Fixtures, read-only, auf Windows testbar) — der AppleScript-Pfad ist für ein Windows-Solo-Projekt dauerhaft ausgeschlossen.

**Wikidata-Integration:** Entity-Disambiguierung für präzisere Wissensgraphen.

**Erweiterte Plattformunterstützung:** Desktop-Anwendungen, Linux-Paketmanager (apt, yum, AUR), Mobile Companion App (Suche).

**Institutionelle Features (optional):** Scoped Knowledge Bases, institutionelle Lizenzen — nur wenn die Nachfrage es rechtfertigt. Der Fokus bleibt auf dem individuellen Forscher.

---

## Leitprinzipien

**Infrastruktur, nicht Anwendung.** ARCHILLES ist der semantische Layer zwischen Bibliotheken und LLMs — die Wasserleitung, nicht die Küche. Second-Brain-Funktionalität, Chat-Interfaces und agentische Schreibwerkzeuge werden von spezialisierten Systemen besser gelöst. ARCHILLES liefert die Bibliotheks-Anbindung, die diese Systeme brauchen.

**Privacy ist die Architektur, nicht ein Feature.** Keine Netzwerk-Calls im normalen Betrieb, keine Telemetrie, alle Daten lokal. Wenn der Nutzer sich mit einem Cloud-LLM verbindet, ist das seine bewusste Entscheidung — „Mein Korpus, meine Wahl."

**Souveränität als Versicherung.** Provider-Unabhängigkeit ist keine Marketing-These, sondern eine Versicherungspolice: Der Zugang zum eigenen Bibliothekswissen darf an keinem Anbieter, keiner Plattform-Politik und keiner Jurisdiktion hängen. Die Prämie dieser Versicherung wird bewusst klein gehalten — ein dünner Frage-Pfad gegen beliebige OpenAI-kompatible Endpunkte genügt als Exit; ein eigenes Frontend würde die Grenze Infrastruktur→Anwendung verletzen und wird nicht gebaut.

**Weniger Code, mehr Architektur.** Wo eine architektonische Lösung bessere Ergebnisse liefert als eine code-intensive Heuristik, wird die Architektur gewählt.

**Modulare Erweiterbarkeit vor Featurefülle.** Registry-Pattern, Plugin-fähige Schnittstellen und definierte Erweiterungszonen sind wichtiger als jedes einzelne Feature. Die Adapter-Architektur ist das Produkt.

**Akademischer Anspruch als Differenzierung.** Exakte Zitationen mit Seitenangaben, transparentes Retrieval, disziplinspezifische Optimierungen — das unterscheidet ARCHILLES von generischen RAG-Lösungen und von Konversations-Memory-Systemen.

**Aufschub als bewusste Strategie.** Graph RAG, OCR, institutionelle Features und Lab-Schreib-Tools werden zum richtigen Zeitpunkt implementiert. Ein funktionierendes Produkt hat Vorrang vor einer vorzeitig aufgeblähten Architektur.

**Core bleibt frei und Open Source** (MIT-Lizenz). Special Editions finanzieren die Weiterentwicklung. Keine Breaking Changes ohne Migrationspfad.

---

## Community

Die Roadmap wird durch Nutzerbedürfnisse geformt.

Feature Requests und Bug Reports: [GitHub Issues](https://github.com/archilles/archilles/issues)
Diskussionen: [GitHub Discussions](https://github.com/archilles/archilles/discussions)
Beta-Testing: Programm in Vorbereitung für v1.0.

---

*Für die technischen Architekturentscheidungen hinter dieser Roadmap siehe [DECISIONS.md](DECISIONS.md).*
*Für die Systemarchitektur siehe [ARCHITECTURE.md](ARCHITECTURE.md).*