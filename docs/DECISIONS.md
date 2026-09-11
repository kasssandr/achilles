# ARCHILLES – Entscheidungsarchiv

**Dokumenttyp:** Lebende Referenz für strategische und technische Entscheidungen
**Erstfassung:** 13. Februar 2026
**Letzte Überarbeitung:** 11. September 2026 (ADR-032 Naht Scriptor → Archilles; Nachtrag zu ADR-004)
**Zweck:** Jede neue Claude-Session, jeder künftige Contributor und Tom selbst in drei Monaten sollen verstehen, *warum* ARCHILLES so gebaut ist, wie es gebaut ist.

---

## Was dieses Dokument ist und was nicht

Dieses Dokument hält die wesentlichen Entscheidungen fest, die das Projekt geformt haben – nicht als lückenlose Chronik, sondern als destillierte Begründungssammlung. Es erklärt Architekturentscheidungen, Marktpositionierung, bewusst aufgeschobene Optionen und verworfene Alternativen. Die technische Implementierung wird separat in ARCHITECTURE.md beschrieben; hier geht es um das *Warum*.

Die Entscheidungen sind nach inhaltlicher Logik gruppiert, nicht chronologisch. Wo es zum Verständnis beiträgt, sind Zeitpunkte angegeben.

---

## I. Marktpositionierung und Validierung

### Die Grundthese: Humanities-Forscher sind unterversorgt

Im November 2025 wurde der RAG-Markt parallel über fünf verschiedene KI-Modelle (ChatGPT, Claude Opus, Google Gemini, Grok 4.1 und LMArena) analysiert. Die Modelle konvergierten auf mehreren Befunden, die zur strategischen Grundlage des Projekts wurden.

Der Gesamtmarkt für RAG-Systeme wächst von ca. 300 Mio. USD (2024) auf projizierte 2,5 Mrd. USD (2030). Innerhalb dieses Marktes existiert eine strukturelle Lücke: Geisteswissenschaftler, Historiker und Forscher mit großen persönlichen Textsammlungen werden von existierenden Lösungen nicht bedient. Cloud-basierte Systeme wie Elicit, Consensus oder Scite setzen auf ihre eigenen Korpora oder institutionelle Zugänge; lokale Open-Source-Frameworks wie LlamaIndex, LangChain oder AnythingLLM sind generisch und liefern keine zitierfähigen Quellenangaben in akademischem Format.

Der europäische Markt zeigt dabei eine 2-3x höhere Zahlungsbereitschaft für datenschutzkonforme Lösungen als der US-Markt, weil die DSGVO-Anforderungen das Bedürfnis nach lokaler Datenverarbeitung verstärken.

### Warum RAG und nicht Fine-Tuning

Eine Analyse im Januar 2026 bestätigte, dass echtes LLM-Fine-Tuning mit Kosten von 50-90 Mio. USD für Training von Grund auf und erheblichem ML-Engineering-Aufwand selbst für domänenspezifische Anpassungen fest im Enterprise-Bereich verankert bleibt. RAG ist kein Kompromiss, sondern die technisch angemessene Lösung für individuelle Forscher, die ihre vorhandenen Bibliotheken mit LLM-Fähigkeiten verbinden wollen, ohne Machine-Learning-Infrastruktur betreiben zu müssen. Diese Erkenntnis gibt ARCHILLES ein Zeitfenster von mindestens 12-18 Monaten, bevor günstigere Fine-Tuning-Methoden die Nische bedrohen könnten.

### Calibre 8.16: Validierung durch Wettbewerbsanalyse

Im Dezember 2025 führte Calibre Version 8.16 eigene AI-Features ein: KI-gestützte Buchdiskussionen, Ähnlichkeitsempfehlungen und lokale Modellunterstützung über LM Studio/Ollama. Systematische Tests mit verschiedenen Dokumenttypen ergaben:

Lokale Modelle wie Gemma3 produzierten bei einem PDF, das nur ein Inhaltsverzeichnis enthielt (Hans Blumenberg, *Die Genesis der kopernikanischen Welt*), umfangreiche Halluzinationen – erfundene Kapitel und Inhalte. Googles Gemini-Modelle lieferten bessere Ergebnisse, stützten sich dabei aber auf Web-Grounding: Sie durchsuchten externe Quellen wie dandelon.com, einen europäischen Bibliothekskatalog-Anreicherungsdienst. Bei einem unveröffentlichten Manuskript (Skriptum zur Deutschen Rechtsgeschichte) konnte Gemini nur generische Beschreibungen liefern, weil kein Web-Grounding verfügbar war.

Die Schlussfolgerung: Calibres AI-Features lösen ein fundamental anderes Problem als ARCHILLES. Calibre bietet ein Gesprächsinterface für einzelne Bücher, das auf externem Wissen basiert. ARCHILLES ermöglicht semantische Suche über die gesamte persönliche Bibliothek mit verifizierbaren Quellenangaben aus den tatsächlichen Dokumenten. Die beiden Ansätze sind komplementär, nicht konkurrierend.

### Direkte Konkurrenz: überschaubar und schwach

Die Wettbewerbsanalyse zum Jahreswechsel 2025/26 identifizierte als direktesten Konkurrenten das Projekt calibre-rag-mcp-nodejs von ispyridis (veröffentlicht Dezember 2025, FAISS + Xenova Transformers, Windows-optimiert). Es fehlen exakte Zitationen, Annotationssuche und hybrides Retrieval; die Adoption lag bei 2 GitHub-Stars. Im Zotero-Ökosystem existieren reifere Lösungen (zotero-mcp, PapersGPT, mcp-research), die aber auf Referenzverwaltung statt Volltextsuche spezialisiert sind.

ARCHILLES' Alleinstellungsmerkmale bleiben bestätigt: exakte Zitationsfähigkeit mit Seitenangaben, Annotations-Indexierung, hybrides Retrieval (semantisch + keyword) und vollständig lokaler Betrieb.

### ADR-022: Strategische Fokussierung — Infrastruktur-Layer, nicht Second Brain (April 2026)

**Kontext:** Im Frühjahr 2026 explodiert das Ökosystem für KI-gestütztes persönliches Wissensmanagement. MemPalace (Jovovich/Sigman, April 2026, 27k GitHub-Stars in 3 Tagen) adressiert Konversations-Memory mit räumlicher Metapher und lokalem ChromaDB-Backend. Claudian integriert Claude Code agentisch in Obsidian-Vaults. Nate B. Jones' Open Brain (Februar 2026) und Andrej Karpathys Konzeptualisierung haben das Second-Brain-Thema in den Mainstream gebracht. Parallel dazu hatte ARCHILLES mit der TWO_DB_VISION.md (März 2026) und dem Archilles Lab begonnen, in Richtung eines eigenen Second-Brain-Systems zu expandieren — mit Schreib-Tools (`add_note`, `link_insight`, `save_chat_excerpt`), Cross-Search-Brücke (Corpus callosum) und eigenständiger Knowledge-Base-Verwaltung.

**Analyse:** Das Second-Brain-Feld wird überfüllt, und jeder Akteur bringt mehr Entwicklerressourcen mit als ein Solo-Projekt aufbieten kann. Gleichzeitig löst *keiner* dieser Akteure das Problem, das ARCHILLES bereits löst: einen semantischen Layer zwischen heterogenen Bibliothekssystemen (Calibre, Zotero, Ordnerstrukturen) und LLMs zu legen, der Annotationen als erstklassige Objekte behandelt, exakte Quellenverweise liefert und die epistemische Integrität des Quellkorpus architektonisch schützt.

**Entscheidung:** ARCHILLES positioniert sich als semantische Infrastrukturschicht zwischen bestehenden Bibliothekssystemen und LLM-Ökosystemen. Die Expansion in Richtung Second Brain wird gestoppt. Das Archilles Lab bleibt als Referenzintegration erhalten (es zeigt, wie ein Obsidian-Vault über den Folder-/ObsidianAdapter an ARCHILLES andockt), wird aber nicht zu einem eigenständigen Wissensmanagement-Produkt ausgebaut.

**Was priorisiert wird:**
- Adapter-Pipeline als Produktkern (jeder neue Adapter erweitert den adressierbaren Markt)
- Annotation-Engine als USP (Anchor-Matching, Context-Enriched Embedding — das macht sonst niemand)
- MCP-Tools als saubere API für Drittanbieter-Anbindung
- HTTP/SSE-Transport für LLM-Agnostik
- Benchmark-Suite für Bibliotheks-Retrieval (eigener Problemraum, nicht LongMemEval)

**Was deprioritisiert wird:**
- Lab-Schreib-Tools (`add_note`, `link_insight`, `save_chat_excerpt`) — nicht gestrichen, aber nicht auf dem kritischen Pfad
- Corpus callosum (Cross-Search-Brücke zwischen Hemisphären) — geparkt
- Eigene Chat-UI und Desktop-App — nachrangig gegenüber Kern-Pipeline
- Konversations-Memory-Features — nicht unser Problem

**Begründung:** Fokussierung auf den Teil des Stacks, den sonst niemand baut. Die Adapter-Architektur (ADR-021) ist der technische Burggraben. Die MCP-Schnittstelle ist die API, über die jedes Second-Brain-System an die Bibliothek andocken kann — ob MemPalace, Claudian oder was immer sich durchsetzt. Die Positionierung ist klarer, verteidigbarer und als Solo-Projekt realisierbar.

**Konsequenzen:** Die Roadmap wird angepasst: v1.3 wird zur Lab-Referenzintegration statt zum eigenständigen Knowledge-Base-Meilenstein. Die Benchmark-Suite wird in v1.0 aufgenommen. Die Kommunikation schärft sich: "ARCHILLES ist die semantische Infrastruktur, die dein Second Brain an deine Bibliothek anschließt."

---

### ADR-029: Richtungsentscheidung nach Marktaufklärung — Kurs bestätigt, Flanken neu geordnet (Juli 2026)

**Kontext:** Anfang Juli 2026 wurde die Grundpositionierung einer strukturierten Prüfung unterzogen: Marktaufklärung über vier web-fähige Quellen (ChatGPT, Perplexity, Grok und ein Deep-Research-Bericht von Gemini), kondensiert nach Konsens vs. Einzelquelle, dazu ein Fragenkatalog zu Erfolgsdefinition, Ressourcen und Motivation. Kernbefunde der Aufklärung, hohe Konfidenz:

1. MCP hat sich als herstellerübergreifender Standard konsolidiert; ein rivalisierendes Protokoll ist kein Risiko.
2. RAG bleibt trotz gewachsener Kontextfenster für persönliche Bibliotheksgrößen essenziell (Kosten, Latenz, Quellenpräzision).
3. Die Nische „Tiefenerschließung heterogener persönlicher Bibliotheken mit seiten-/kapitelgenauen Zitationen" ist von keinem dominanten Produkt besetzt; der Markt ist fragmentiert — konkret in **App-gebundene RAG-Inseln**: ZotSeek und Beaver erschließen nur Zotero, Neural Composer (LightRAG) und Smart Connections nur den Obsidian-Vault (Einzelquellen-Konfidenz bei den Namen, Konsens beim Muster). Kein genanntes Werkzeug führt Calibre, Zotero und Obsidian in *einem* Index zusammen.
4. DEVONthink liefert seit v4.3 („Herschel") einen **nativen MCP-Server** — nach Detailbefund jedoch Basis-Retrieval und Navigation je Einzeldatenbank: keine akademischen Zitier-Workflows mit Seitenangaben, keine datenbankübergreifende Semantik, keine Event-Subscriptions (Einzelquellen-Konfidenz).
5. Der Zugang für lokale MCP-Server bleibt außerhalb von Claude Desktop gated: ChatGPT Desktop unterstützt *keinen* lokalen stdio-Transport, sondern verlangt eine remote HTTPS/SSE-Verbindung (Tunnel — für Nicht-Entwickler kaum zumutbar; „voller MCP-Support" in Community-Berichten meinte die Protokoll-, nicht die Transport-Ebene). Gemini erlaubt seit 30.06.2026 custom MCP in der macOS-App, aber nur als Beta für Ultra-Abonnenten und nur per remote-URL. Nativen lokalen stdio-Zugang bieten dagegen: Claude Desktop/Claude Code, Cursor, Codex CLI, Windsurf, Cline und Cherry Studio (letzteres in den Quellen ausdrücklich für Zotero-/Literatur-Bibliotheken genannt).
6. Die Annotation-USP-Behauptung aus ADR-022 („Annotationen als erstklassige Objekte — das macht sonst niemand") hält der Prüfung gegen das ~40-Quellen-Korpus stand: Kein genanntes Werkzeug indexiert Nutzer-Annotationen als eigenständige semantische Suchobjekte mit Quellsystem-Anbindung; ein „Sync-to-RAG"-Pfad für E-Reader-Marginalien existiert bei keinem Mitbewerber; DEVONthinks nativem Server fehlt Annotation-Zugriff ausdrücklich.

Projektseitig: Erfolg ist primär der eigene tägliche Nutzen (bereits real); OSS-Adoption erwünscht, kommerzielle Erwartung realistisch niedrig; keine Zeitbindung; begrenzte Mittel (Solo-Entwicklung, eine 4-GB-GPU); Zugkraft bisher pre-audience, Öffentlichkeitsarbeit bewusst zurückgehalten, bis das Produkt trägt. Neu artikuliert: ein **Souveränitäts-Motiv** — der Zugang zum eigenen Bibliothekswissen soll auch bei staatlicher KI-Reglementierung oder geopolitischer Zuspitzung gesichert bleiben, notfalls über lokale oder alternative Modelle.

**Entscheidung:** Die Grundpositionierung (ADR-022: semantische Infrastrukturschicht, kein Second Brain, kein Produkt-Frontend) **wird bestätigt**. Vier Präzisierungen:

1. **Provider-Unabhängigkeit wird neu begründet: als Souveränitäts-Versicherung, nicht als Markt-These.** Die zwei Unabhängigkeiten werden sauber getrennt: *Portabilität* (Protokoll: erreicht; Zugang: gated, de facto Claude-zentriert — akzeptiert, der ausgelieferte HTTP/SSE-Transport hält den Weg warm) und *Autonomie* (kein Anbieter nötig: als Exit-Fähigkeit vorgehalten, nicht als Alltagsbetrieb versprochen). Die ehrliche Privacy-Formel lautet „Daten und Index lokal; Reasoning per bewusster Wahl in der Cloud — oder notfalls lokal" und ersetzt jede „alles lokal"-Suggestion. Für die technisch anspruchsvolle Zielgruppe ist diese Präzision Stärkung, nicht Schwächung. Einordnung der Lokal-KI-Lage (Stand Mitte 2026, quellenübergreifend konsistent): Drei Hardware-Klassen. ~128 GB Unified Memory (M4-Max-Klasse) trägt große MoE-Modelle nahe 2024er-Frontier-Niveau; 16–32 GB RAM — laut Quellen die *typische* Ausstattung der technisch versierten Zielgruppe — trägt 32B-Modelle alltagstauglich für Dokument-Reasoning; unter 8 GB bleiben Extraktion und Klassifikation. Quellen-Konsens: Ab ~7B plus Reranking ist lokales Retrieval mit faktischen Antworten über persönliche Bibliotheken „gut genug"; die Grenze zur Cloud verläuft beim mehrstufigen Reasoning und der Synthese über viele Quellen. Das bestätigt die *Richtung* der Versicherungsthese und macht den Exit-Pfad für einen relevanten Teil der Zielgruppe schon heute alltagstauglich — auf der Entwicklungshardware bleibt er Notfall-Option, und Frontier-Parität besteht nirgends. Die Exit-Fähigkeit wird deshalb *nicht* als Kosten- oder Qualitätsversprechen vermarktet (dieselbe Präzisionsregel wie bei „100 % lokal"); sie gewinnt mit dem Hardware-Trend von selbst an Wert, ohne dass ARCHILLES mehr investieren muss als den dünnen Frage-Pfad.

2. **Client-/Protokoll-Schicht: minimale Investition.** Kein OpenAPI/Actions-Weg (die Zielgruppe sitzt nicht in Custom GPTs), kein eigenes GUI-Frontend (verletzt die ADR-022-Grenze Infrastruktur→Anwendung). Der kleinste Schritt aus der faktischen Claude-Abhängigkeit ist ein **dünner CLI-Frage-Pfad**: Retrieval + `PromptBuilder`-Prompt gegen einen konfigurierbaren OpenAI-kompatiblen Endpunkt (Cloud-API oder Ollama). Das macht Autonomie zur getesteten Fähigkeit statt zur Behauptung — zu Versicherungs-Kosten. Kommunikations-Regel: In Doku und Website strikt zwischen **„nativ lokal"** (Claude Desktop/Code, Cursor, Codex CLI, Windsurf, Cline, Cherry Studio) und **„via SSE-Brücke"** (ChatGPT, Gemini — Tunnel/remote-URL nötig) unterscheiden; „funktioniert mit ChatGPT" ohne diesen Zusatz wäre technisch wahr und praktisch irreführend. Cherry Studio als MCP-Client mit Literatur-Bibliotheks-Fokus ist ein neu identifizierter, natürlicher Zielclient und gehört in die Doku.

3. **DEVONthink-Adapter vertagt — aus Ressourcen-Gründen, nicht aus Markt-Logik.** Der native MCP-Server des Incumbents *absorbiert den Use-Case nicht*: Er liefert Basis-Retrieval je Einzeldatenbank, ohne akademische Zitationspräzision und ohne datenbankübergreifende Semantik. Die Vertagung ruht damit allein darauf, dass der Adapter die teuerste Stelle wäre, Untestbarkeits-Risiko auszugeben (Mac-only, kein Gerät, kein Tester, knappe Solo-Zeit). Die Positionierung ist entsprechend **nicht** „Ausweichprodukt für Windows/Linux", sondern plattformübergreifend: **der Spezialist für bibliographische Tiefe** — seiten-/kapitelgenaue Zitationen und bibliotheksübergreifende Erschließung (Calibre + Zotero + Obsidian in **einem** Index), die auch der Mac-Nutzer bei DEVONthink derzeit nicht bekommt. Windows/Linux bleibt Heimrevier als Entwicklungs-Realität (dort kann getestet werden), nicht als Identität. Wiedereintritts-Gates sind folglich Nachfrage und Testbarkeit — nicht die Schwäche des Incumbents, die ist bereits belegt: Inbound-Nachfragen nach einem Adapter, verfügbarer Mac + Beta-Tester; gegenläufig sinkt der Wiedereintritts-Wert, falls DEVONthink die Zitations- und Cross-Database-Lücken selbst schließt. Review Q1 2027; falls Wiedereintritt, ausschließlich Datei-Format-Pfad gegen Fixtures.

4. **Benchmark-first.** Das nächste Großvorhaben ist das Benchmark-Harness (ADR-030); die Parent-Child-Aktivierung wird dahinter gegated. Watchdog-Generalisierung Schritt B rückt hinter Benchmark und Annotation Phase 5 (der praktische Bedarf ist durch ADR-025 gedeckt).

**Zweitbeste Richtung und Ausschlussgrund:** Der DEVONthink-Adapter jetzt, über den Fixtures-Pfad, um den Idealnutzer (Mac-Geisteswissenschaftler) früh zu binden. Ausgeschlossen, weil der Incumbent genau diesen Use-Case soeben nativ ausgeliefert hat, das Vorhaben die knappste Ressource (Solo-Entwicklungszeit) an der am schwersten testbaren Stelle bindet und die Zielgruppen-Priorität („Zotero zuerst") ohnehin in die andere Richtung zeigt. Ein entfernter dritter Kandidat — Pivot Richtung Second-Brain-Produkt — bleibt aus den Gründen von ADR-022 verworfen; die Marktaufklärung hat die Überfüllung dieses Felds erneut bestätigt.

**Die teure Entscheidung (falls falsch):** Die Vertagung der Mac/DEVONthink-Flanke — und zwar *während* der Incumbent dort nachweislich schwach ist (Einzel-Datenbank, keine Zitationspräzision). Sitzt die adoptionswilligste Nutzerschaft auf dem Mac, wird ein offenes Zeitfenster nicht genutzt, das DEVONthink selbst schließen könnte. Die Entscheidung ist dennoch tragbar, weil die knappste Ressource an dieser Stelle den schlechtesten Wechselkurs hat und weil es eine **Vertagung mit definierten Wiedereintritts-Gates** ist, keine Amputation — die Adapter-Architektur hält die Tür offen. Frühindikatoren (beobachten, Review Q1 2027): Resonanz im DEVONthink-Forum auf den nativen MCP-Server (anhaltende Klagen über Zitationspräzision/bibliotheksweite Suche = das Fenster steht noch offen); Inbound-Nachfragen nach einem Adapter; ob DEVONthink die Lücken selbst schließt (= Fenster zu, Wiedereintritt entwertet); Verfügbarkeit eines Mac + Beta-Testers.

**Konsequenzen:** Roadmap-Anpassung — v1.0 re-sequenziert, der Abschnitt „Zugangswege und Provider-Unabhängigkeit" ergänzt, die DEVONthink-Vertagung in den langfristigen Horizont eingeordnet, das Leitprinzip „Souveränität als Versicherung" aufgenommen; umgesetzt in `docs/ROADMAP.md` mit Commit `a419773` (7. Juli 2026). ADR-030 spezifiziert das Benchmark-Harness. Öffentlichkeitsarbeit bleibt sequenziert wie gehabt (Website → Outreach, ein glaubwürdiger Schuss pro Community); das Benchmark wird das zentrale Launch-Asset. Kommunikation übernimmt die ehrliche Privacy-Formel. **Kill-Shot-Beobachtungsliste** (quartalsweise prüfen): (a) Baut Zotero selbst native semantische Suche mit Seitenzitaten ein? (b) Öffnet Google NotebookLM für lokale Dateisysteme/Zotero-Datenbanken (heute: isolierte Notebooks, 50/300-Quellen-Limit)? (c) Liefern ChatGPT oder Gemini nativen lokalen stdio-Transport (heute: nur remote/Tunnel)? (d) Erreichen Frontier-Modelle zuverlässige Seiten-/Kapitelzitate allein über Long-Context, ohne Retrieval-Schicht? Solange alle vier Fragen „nein" lauten, steht der Existenzgrund; Stand 05.07.2026 lauten alle vier „nein" — die Anbieter absorbieren generischen Datei-Zugriff, nicht bibliographische Tiefe.

---

## II. Technische Architekturentscheidungen

### ADR-001: LanceDB statt ChromaDB (Februar 2026)

**Kontext:** ARCHILLES lief produktiv mit ChromaDB und 46.354 Chunks aus ca. 87 Büchern. Die Analyse ergab, dass ChromaDB ab ca. 100.000 Chunks Performance-Degradation zeigt. Bei durchschnittlich 533 Chunks pro Buch bedeutet das ein Maximum von ca. 188 Büchern – weit unter dem Ziel von 500-1.000 Leit-Titeln aus einer Gesamtbibliothek von 8.000+.

**Entscheidung:** Migration zu LanceDB.

**Begründung:** LanceDB bringt native Hybrid-Search (dense + sparse Vectors) mit. Die IVF-PQ-Indexstruktur ist für Millionen von Chunks optimiert und optional GPU-beschleunigbar. Die Migration wurde bewusst früh durchgeführt, als die Datenbank noch klein und ein Re-Indexing unkompliziert war.

**Konsequenzen:** Der gesamte Storage-Layer, Indexer und Retriever mussten umgeschrieben werden. Alle ca. 87 Bücher wurden aus den Quelldateien neu indexiert – nicht aus ChromaDB exportiert –, was zwei Vorteile brachte: Erstens erhielten auch die älteren Bücher die seit Januar 2026 verfügbare Section-Metadata (Front Matter / Hauptinhalt / Back Matter Klassifikation), zweitens entfiel die Abhängigkeit vom alten ChromaDB-Format. Der Indexstand stieg durch Re-Indexierung und neue Bücher auf über 78.000 Chunks.

Im selben Zug wurde der bibliography/index-Rauschfilter architektonisch gelöst: Eine 118-Zeilen-Text-Heuristik (`_is_bibliography_or_index()`), die bei Tests 0 von 4 echten Rausch-Chunks erkannte und dafür Fußnoten als False Positives produzierte, wurde komplett entfernt. Stattdessen filtert das System nun auf DB-Ebene über `section_type`: Default `section_filter='main'` schließt Anhang, Register und Inhaltsverzeichnis automatisch aus. Das Ergebnis: minus 159 Zeilen netto bei besserem Ergebnis – ein Musterbeispiel für das Leitprinzip "weniger Code, mehr Architektur".

Das Architekturprinzip dabei: "Wir bauen ein Chassis, in das wir später bessere Motoren einbauen können – und verlegen jetzt schon Kabel zu Steckplätzen, an denen wir künftig erwartbare neue Geräte einstecken können." Die Parameter-Ebene im Code wurde von Beginn an auf Diversifizierung und Erweiterbarkeit ausgerichtet.

**Technisches Detail zur Hybrid-Search:** LanceDB implementiert intern eine eigene Variante der Fusion von Vektor- und Keyword-Ergebnissen. Die zuvor selbst implementierte BM25- und RRF-Logik konnte daher stark vereinfacht werden, ist aber nicht vollständig entfallen – der Retriever nutzt LanceDBs native Hybrid-Search-API statt eigener Fusionsalgorithmen.

### ADR-002: BGE-M3 als Embedding-Modell

**Kontext:** Für ein System, das Texte in Deutsch, Englisch, Latein, Altgriechisch und weiteren Sprachen verarbeiten muss, ist ein multilinguales Embedding-Modell entscheidend. Die Zielgruppe arbeitet mit historischen und modernen Quellen in wechselnden Sprachen.

**Entscheidung:** BGE-M3 von BAAI als primäres Embedding-Modell (1024 Dimensionen, multilingual).

**Begründung:** BGE-M3 wurde in der Marktanalyse über mehrere KI-Modelle hinweg als einer der Spitzenreiter für multilinguales Retrieval identifiziert. Es bietet native Unterstützung für Dense-, Sparse- und ColBERT-Retrieval in einem einzigen Modell. Die Chunking-Intelligence-Analyse (parallel über Gemini, Grok und ChatGPT durchgeführt) bestätigte die Eignung für wissenschaftliche Texte.

**Offene Frage:** Evaluation von multilingual-e5 und jina-embeddings-v3 als Alternativen für den Mid-term (Q2 2026).

### ADR-003: PyMuPDF als primärer PDF-Extraktor

**Kontext:** Die Qualität der Textextraktion bestimmt die Qualität der Suchergebnisse. Verschiedene PDF-Extraktionsbibliotheken wurden evaluiert.

**Entscheidung:** PyMuPDF (fitz) als primärer Extraktor, mit Multi-Tier-Fallback-System.

**Begründung:** PyMuPDF bietet die beste Kombination aus Geschwindigkeit und Extraktionsqualität für die Mehrzahl der Dokumente. Es liefert zuverlässiges Seitenzahlen-Mapping, das für zitierfähige Quellenangaben unerlässlich ist. Die ursprünglich als primärer Extraktor vorgesehene Bibliothek pdfplumber wurde auf eine Fallback-Rolle zurückgestuft. Für problematische PDFs (historische Scans, komplexe Layouts) steht ein Fallback-System bereit, das bei Qualitätsproblemen alternative Extraktoren einschaltet.

**Verworfene Alternativen:** Marker (LLM-gestützter Korrekturmodus) wurde als bedarfsgesteuertes Feature für die Zukunft notiert, nicht als aktive Planung. Die Entscheidung fällt nach Beta-Feedback über die tatsächliche Extraktionsqualität.

**Ergänzung (21. Februar 2026): Markdown als Extraktionsziel.** Die aktuelle Extraktion liefert Plaintext-Chunks, deren Zeichenkodierung für menschliche Inspektion schwer lesbar ist (Encoding-Artefakte, fehlende Struktur). Parallel dazu hat sich im Feld eine Best Practice etabliert: PDF → strukturiertes Markdown → Chunking entlang von Heading-Hierarchien, statt blindem Token-Splitting auf flachem Text. Markdown erhält Teil/Abschnitt/Unterabschnitt-Hierarchien und ermöglicht strukturorientiertes Chunking – für historische Fachtexte mit komplexer Gliederung ein qualitativer Gewinn.

Als konkrete Implementierungsoption wird **Docling** (IBM, Open Source, lokal lauffähig) notiert: Es produziert strukturierten Markdown-Output mit erhaltenen Heading-Pfaden und ist CPU-fähig. Evaluierbar als Ergänzung oder Ersatz des PyMuPDF-Extraktors, sobald Beta-Feedback zur Extraktionsqualität vorliegt. Für die Historical Special Edition ist strukturorientiertes Chunking auf Markdown-Basis ohnehin Voraussetzung für eine sinnvolle LightRAG-Integration.

**Offene Frage:** Ob Markdown-Output bereits für das MVP sinnvoll ist (Verbesserung der menschlich inspizierbaren Chunk-Qualität) oder erst für die Special Editions, hängt vom tatsächlichen Aufwand ab und wird nach erster Docling-Evaluation entschieden.

### ADR-004: Modulare Pipeline-Architektur

**Kontext:** ARCHILLES soll verschiedene Parser, Chunker und Embedder unterstützen können – sowohl für verschiedene Dateiformate als auch für künftige Special Editions mit disziplinspezifischen Optimierungen.

**Entscheidung:** Modulare Pipeline-Architektur, auf ein Registry-Pattern hin angelegt. Parser, Chunker und Embedder sind als austauschbare Komponenten mit klar definierten Schnittstellen implementiert.

**Begründung:** Das Pattern ermöglicht die spätere Erweiterung um neue Extraktoren (etwa für DJVU, OCR-intensive Dokumente oder proprietäre Formate), neue Chunking-Strategien (semantisch vs. fixed-size vs. hybrid) und neue Embedding-Modelle, ohne den Kern des Systems zu modifizieren. Es ist zudem die technische Voraussetzung für das Freemium-Modell: Die Basisversion nutzt Standard-Komponenten, Special Editions können optimierte Varianten einsetzen.

**Implementierungsstand (aktualisiert Juni 2026, P2-Etappe 5):** Das Registry-Pattern wird dort eingesetzt, wo Selektion ein echter Dispatch ist — nicht flächendeckend. Im Code existieren zwei formale Registries auf der generischen Basis `BaseRegistry[T]` (`src/archilles/registry.py`): `ParserRegistry` (Dispatch nach Dateiformat: `PyMuPDFParser`, `EPUBParser`) und `AnnotationProviderRegistry` (Annotation-Quellen). Chunker und Embedder haben **keine** Registry mehr: Der Chunker wird per Frontmatter-Strategie direkt selektiert (`pipeline._select_chunker`), der Embedder per Hardware-Profil (`pipeline._create_embedder_from_profile`). Ihre Offenheit kommt aus den ABCs `TextChunker`/`TextEmbedder` — eine neue Variante ist eine einzige Klasse.

Diese Korrektur geht auf das Code-Review (10. Juni 2026, Befund 3.13) zurück: Die ursprünglich angelegten `ChunkerRegistry` und `EmbedderRegistry` wurden als „tote Infrastruktur, die Erweiterbarkeit vortäuscht" identifiziert und in P2-Etappe 5 entfernt. Das Leitprinzip „weniger Code, mehr Architektur" gilt auch für Registries: Offenheit gehört dorthin, wo sie real genutzt wird, nicht als Zeremonie über jede Komponente. Die `ModularPipeline` (`pipeline.py`) orchestriert weiterhin den Dreischritt Parser → Chunker → Embedder. Parallel dazu existieren die Extractors (`src/extractors/`) als eigenständige Schicht für die Rohtextextraktion, koordiniert durch `UniversalExtractor` mit `FormatDetector`.

**Nachtrag (September 2026): Der Modularpfad ist stillgelegt, nicht der Produktionspfad.** Am 4. Juli 2026 wurde die `ModularPipeline` als experimentell markiert (`f010b5f`). Sie läuft nur hinter `--use-modular-pipeline`, und keine Routine setzt das Flag. Die Begründung im Commit: Der Pfad ist nicht in `ExecutionPlan`, hierarchisches Chunking und Spracherkennung integriert, lädt einen zweiten Embedder neben den schon geladenen und überspringt `metadata_hash` und Annotations-Chunks, bleibt also für die Änderungserkennung des Watchdogs unsichtbar. Ausdrücklich: *„do NOT attempt full integration."* Produktiv indexiert `Indexer.index_book`/`prepare_book` (`src/archilles/engine/indexing.py`): `UniversalExtractor` → formatspezifischer Extraktor, der selbst chunkt (`BaseExtractor`) → BGE-M3. Eine Messung vom 9. September 2026 fand in keinem der drei Stores eine einzige Zeile, die der `DialogueChunker` geschrieben hätte. Die Extraktor-Schicht ist damit der Erweiterungspunkt, den neue Formate tatsächlich benutzen; der Scriptor-Import ist bewusst dort gebaut (ADR-032). Weiter produktiv sind `embedders/` (externer Embedding-Pfad, Annotationen) und `registry.py` (Basis der `AnnotationProviderRegistry`). Ob `pipeline.py`, `parsers/` und `chunkers/` entfernt werden, ist offen (Naht-Schritt S11). Die Frage dahinter ist inhaltlich, nicht architektonisch: ob Dialog-Exporte an Sprecherwechseln geschnitten werden sollen.

### ADR-005: Keine direkte Modifikation von Calibres metadata.db

**Kontext:** Metadaten-Anreicherung durch LLM-Extraktion aus Volltexten wurde als Feature diskutiert – etwa fehlende Autoren, Erscheinungsjahre oder Schlagworte automatisch ergänzen.

**Entscheidung:** Calibres metadata.db wird nie direkt modifiziert.

**Begründung:** Calibre-Nutzer verlassen sich auf die Integrität ihrer Datenbank. Direkte Modifikation birgt das Risiko von Datenbeschädigung und verletzt das Vertrauen der Nutzer. Stattdessen wird der `.archilles`-Ordner als definierte Erweiterungszone genutzt. Externe Metadaten können in einer separaten JSON- oder SQLite-Datei gespeichert und zur Laufzeit mit Calibre-Metadaten zusammengeführt werden.

### ADR-006: Hybride Suche mit Reciprocal Rank Fusion

**Kontext:** Rein semantische Suche findet konzeptionell verwandte Passagen, versagt aber bei exakten Begriffen – Eigennamen, Jahreszahlen, Fachterminologie. Reine Keyword-Suche findet exakte Treffer, versteht aber keine Bedeutung.

**Entscheidung:** Hybride Suche, die BGE-M3-Vektorembeddings mit BM25-Keyword-Matching über Reciprocal Rank Fusion kombiniert.

**Begründung:** Geisteswissenschaftler suchen sowohl nach Konzepten ("Legitimation von Herrschaft im Mittelalter") als auch nach konkreten Referenzen ("Eusebius von Caesarea" oder "325 n. Chr."). Die hybride Suche bedient beide Suchmodi. RRF als Fusionsmethode wurde in der Wettbewerbsanalyse (Dezember 2025) als algorithmisch einfach, ohne neue Dependencies und mit messbarer Qualitätsverbesserung bewertet.

**Evolutionspfad:** Die Implementierung hat sich mit der Datenbank-Migration weiterentwickelt. In der ChromaDB-Phase war RRF als eigener Algorithmus implementiert; seit der LanceDB-Migration nutzt `LanceDBStore.hybrid_search()` LanceDBs `RRFReranker` (aus `lancedb.rerankers`) für die native Fusion von Vektor- und Keyword-Ergebnissen. Die Suchlogik ist über zwei Ebenen verteilt: `LanceDBStore` (DB-Level: Hybrid-Suche, Filterung) und `archillesRAG` in `scripts/rag_demo.py` (App-Level: Modus-Auswahl, Tag-Filterung, Diversifizierung, Kontext-Expansion). Es existiert keine separate `hybrid.py`-Datei; die Vereinheitlichung läuft über den Service-Layer.

### ADR-007: OCR-Strategie – Tesseract als Basis, modularer Ausbau

**Kontext:** Ein erheblicher Teil akademischer Bibliotheken besteht aus gescannten PDFs, für die Textextraktion nur über OCR möglich ist. Die Qualitätsanforderungen sind hoch, weil fehlerhafte OCR-Ergebnisse das gesamte Retrieval kompromittieren.

**Entscheidung:** Tesseract als Basismodul, mit vorbereitetem Ausbau auf bessere Modelle.

**Begründung:** Tesseract ist frei verfügbar, gut etabliert und ausreichend für moderne Druckschriften. Für die anspruchsvolleren Fälle – historische Frakturschrift, handschriftliche Marginalien, schlecht gescannte Vorlagen – wird der modulare Ausbau vorbereitet, ohne dass die Basisversion davon abhängt. Die strategische Analyse (Februar 2026) ergab, dass sich die OCR-Landschaft rasant entwickelt und eine zu frühe Festlegung auf ein spezifisches Premium-System riskant wäre. Besser: die Schnittstelle sauber definieren und das beste verfügbare Modell einsetzen, wenn es soweit ist.

**Implementierungsstand:** `ocr_extractor.py` existiert in `src/extractors/` mit `OCRExtractor`- und `TesseractExtractor`-Klassen. Die Dataclass in `models.py` ist mit einem `output_format`-Feld vorbereitet, das verschiedene OCR-Backend-Ausgabeformate unterstützen kann. Der `ArchillesService` exponiert `ocr_backend`-Konfiguration (auto/tesseract/lighton/olmocr) zur Backend-Auswahl.

### ADR-008: Zwei-Datenbanken-Architektur → Konsolidierung in LanceDB (November 2025, aktualisiert Februar 2026)

**Kontext:** ARCHILLES verarbeitet zwei fundamental verschiedene Texttypen: den Buchinhalt selbst und alles, was *über* ein Buch geschrieben wurde – Verlagstexte, Kritiken, NotebookLM-Analysen und persönliche Exzerpte im Calibre-Kommentarfeld. Hinzu kommen Annotationen: Highlights und Notizen, die der Nutzer direkt in seinen Büchern hinterlässt (Calibre-Viewer für EPUBs, Adobe Reader für PDFs).

**Ursprüngliche Entscheidung (November 2025):** Getrennte Datenbanken: `archilles_books` für Volltext-Chunks aus den Buchdateien, `archilles_meta` für Calibre-Kommentare und Annotationen.

**Begründung:** Für Geisteswissenschaftler ist die Unterscheidung zwischen "was steht im Buch" und "was habe ich oder andere darüber geschrieben" fundamental. Eine monolithische Datenbank hätte diese Grenze verwischt. Die Trennung ermöglicht gezielte Suchmodi: nur in Quellentexten suchen, nur in eigenen Notizen suchen, oder beides mit Gewichtung.

**Konsequenzen:** Der MCP-Server exponiert beide Suchräume als separate Tools (`search_books_with_citations` für Buchinhalte, `search_annotations` für Nutzerdaten).

**Aktualisierung (Februar 2026): Annotationen in LanceDB integriert.**

Die ursprünglich als Zwischenlösung in ChromaDB (`annotations_indexer.py`, `all-mpnet-base-v2`, 384 Dim.) gespeicherten Annotationen wurden in die LanceDB-`chunks`-Tabelle migriert. Annotationen werden jetzt als `chunk_type='annotation'` gemeinsam mit Buchtext-Chunks gespeichert und mit denselben BGE-M3-Embeddings (1024 Dim.) indiziert.

Die semantische Unterscheidung zwischen Buchinhalt und Nutzernotizen bleibt über das `chunk_type`-Feld erhalten: `'content'` für Buchtext, `'calibre_comment'` für Calibre-Metadaten, `'annotation'` für Highlights und Notizen. Die Suchfilterung auf DB-Ebene ist damit weiterhin möglich, und die gezielte Suche in nur einem Datentyp funktioniert über einfache WHERE-Clauses.

Vorteile der Konsolidierung:
- **Ein Embedding-Modell statt zwei:** BGE-M3 für alles eliminiert die semantische Inkompatibilität zwischen `all-mpnet-base-v2` (384 Dim.) und BGE-M3 (1024 Dim.), die Cross-Suchen zwischen Buchtext und Annotationen erschwerte.
- **Annotationen profitieren von Hybrid-Search:** LanceDBs native Fusion aus Vektor- und Keyword-Matching (ADR-006) steht jetzt auch für Annotationen zur Verfügung.
- **Eine Dependency weniger:** ChromaDB ist für die Annotation-Suche nicht mehr erforderlich. Der bestehende ChromaDB-Index (`annotations_indexer.py`) bleibt als Fallback erhalten, wird aber nicht mehr aktiv gefüllt.
- **Einheitliche Änderungserkennung:** Annotationen werden über denselben Hash-Mechanismus wie Metadaten auf Änderungen geprüft (siehe ADR-011).

Die konzeptionelle Zwei-Datenbanken-Architektur ist damit technisch als Filterung innerhalb einer einzigen LanceDB-Tabelle realisiert – einfacher, performanter und wartungsärmer als zwei physisch getrennte Datenbanken.

### ADR-009: Service-Layer-Architektur (Februar 2026)

**Kontext:** Das Web-UI (`web_ui.py`), der MCP-Server (`server.py`) und das CLI (`rag_demo.py`) importierten alle die RAG-Klasse direkt. Jede Änderung an der Suchlogik musste an drei Stellen nachgezogen werden – ein wachsendes Konsistenzproblem.

**Entscheidung:** Einführung eines Service-Layers (`archilles_service.py`) als zentrale Geschäftslogik-Schicht.

**Begründung:** Das ist kein glamouröses Feature, sondern Architekturhygiene. Der Service-Layer kapselt alle Operationen – `search()`, `index_book()`, `get_index_status()`, `get_book_list()` – und wird von allen drei Clients einheitlich genutzt. Änderungen an der Suchlogik, etwa die Integration von Cross-Encoder-Reranking, müssen nur noch an einer Stelle erfolgen. Der Service-Layer ist zudem die Voraussetzung dafür, dass das in ADR-004 formulierte Prinzip der modularen Erweiterbarkeit tatsächlich funktioniert: Neue Backends, neue Suchstrategien oder neue Filter werden im Service implementiert und stehen sofort überall zur Verfügung.

**Verzeichnisstruktur nach Refactoring (Stand Februar 2026 — historisch):**

> **Hinweis (Juni 2026):** Der folgende Baum zeigt den Stand zum Zeitpunkt von ADR-009 und ist seitdem an mehreren Stellen überholt — die Engine ist nach `src/archilles/engine/` umgezogen (ADR-026), die `adapters/`-Schicht ist hinzugekommen (ADR-021), `chunkers/`/`embedders/` haben keine Registry mehr (ADR-004, P2-Etappe 5), und `annotations_indexer.py` ist entfallen. Die **maßgebliche, aktuelle Verzeichnisstruktur steht in [ARCHITECTURE.md](ARCHITECTURE.md#directory-structure)**. Der Baum hier bleibt als historischer Beleg des damaligen Refactoring-Stands erhalten.

```
src/
├── archilles/                     # Modulare Pipeline-Infrastruktur
│   ├── pipeline.py                # ModularPipeline (Parser → Chunker → Embedder)
│   ├── profiles.py                # Hardware-Profile (minimal/balanced/maximal)
│   ├── hardware.py                # Hardware-Erkennung (GPU, VRAM)
│   ├── parsers/                   # ParserRegistry + PyMuPDFParser, EPUBParser
│   ├── chunkers/                  # FixedSize, Semantic, Dialogue (Auswahl per Strategie, keine Registry)
│   ├── embedders/                 # BGEEmbedder (Auswahl per Profil, keine Registry)
│   └── indexer/checkpoint.py      # Checkpoint-Resume für Batch-Indexierung
├── service/
│   └── archilles_service.py       # Zentrale Geschäftslogik-Fassade
├── extractors/
│   ├── universal_extractor.py     # Delegiert an formatspezifische Extractors
│   ├── pdf_extractor.py           # PyMuPDF + pdfplumber-Fallback
│   ├── epub_extractor.py          # ebooklib mit TOC-Parser
│   ├── ocr_extractor.py           # Tesseract-Integration
│   ├── txt_extractor.py           # Plaintext-Extraktion
│   ├── html_extractor.py          # HTML-Dokumente
│   ├── calibre_converter.py       # Calibre ebook-convert Bridge
│   ├── format_detector.py         # Formaterkennung
│   ├── language_detector.py       # Lingua-basierte Spracherkennung
│   ├── models.py                  # Gemeinsame Dataclasses
│   └── exceptions.py              # Fehlertypen-Hierarchie
├── storage/
│   └── lancedb_store.py           # LanceDBStore (Vektor-DB-Backend)
├── retriever/
│   └── reranker.py                # Cross-Encoder Reranking (optional)
├── calibre_mcp/
│   ├── server.py                  # CalibreMCPServer (13 MCP-Tools, Stand Juni 2026)
│   ├── annotations.py             # Annotation-Extraktion, Hash-Mapping
│   └── calibre_analyzer.py        # Bibliotheks-Statistiken
└── calibre_db.py                  # Read-only Calibre-Metadaten-Zugriff
```

Entry-Point: `mcp_server.py` im Projekt-Root (von Claude Desktop aufgerufen).
CLI-Skripte: `scripts/rag_demo.py`, `scripts/batch_index.py`, `scripts/web_ui.py`.

### ADR-010: Eine Datei pro Buchordner (Januar 2026)

**Kontext:** Calibre-Buchordner können neben der Hauptdatei weitere Dateien enthalten: Konvertierungen in verschiedenen Formaten, Cover-Bilder, manchmal auch vom Nutzer abgelegte Exzerpte, Notizen oder ergänzende Materialien in Unterordnern.

**Entscheidung:** Bei der Indexierung wird pro Buchordner genau eine Datei verarbeitet, mit strikter Priorität: PDF > EPUB > sonstige Formate. Unterordner werden in Version 1 bewusst ignoriert.

**Begründung:** Die Beschränkung auf eine Datei verhindert doppelte Indexierung desselben Inhalts in verschiedenen Formaten. PDF hat Vorrang wegen des zuverlässigen Seitenzahlen-Mappings, das für zitierfähige Quellenangaben entscheidend ist. EPUBs liefern dafür bessere Strukturinformationen (TOC-Parsing, Section-Metadata) und werden schneller verarbeitet.

Das Ignorieren von Unterordnern ist eine bewusste Produktentscheidung, keine technische Limitierung. Gut organisierte Nutzer lagern dort oft eigene Exzerpte und Texte, die sie durchaus indexiert haben möchten – und die sie aus guten Gründen nicht ins Calibre-Kommentarfeld schreiben. Statt sie zur Umorganisation zu nötigen, wird die Fein-Indexierung mit Wahl- und Einstelloptionen für eine spätere Version oder die Paid-Version reserviert. Das schafft einen natürlichen Upgrade-Pfad, ohne die Basis-Version zu verkomplizieren.

**Aktualisierung (März 2026):** Die statische Priorität PDF > EPUB wurde durch eine qualitätsbasierte Auswahl ergänzt (siehe ADR-014). Die Grundentscheidung "eine Datei pro Buchordner" bleibt bestehen; was sich ändert, ist wie diese eine Datei bei Mehrfachvorkommen ausgewählt wird.

### ADR-011: Smart Metadata & Annotation Update mit Hash-basierter Änderungserkennung (Februar 2026)

**Kontext:** Bei 670+ indexierten Büchern mit je durchschnittlich 360 Chunks dauert eine vollständige Neu-Indexierung ca. 90 Sekunden pro Buch (Textextraktion, Chunking, BGE-M3-Embedding, LanceDB-Insert). Das ist akzeptabel für die Erstindexierung, aber inakzeptabel für Routinesituationen: Der Nutzer ergänzt ein Schlagwort in Calibre, korrigiert einen Autorennamen oder fügt ein Highlight in einem PDF hinzu – und soll dafür nicht 90 Sekunden warten.

**Entscheidung:** Hash-basierte Änderungserkennung mit differenziellem Update. Zwei unabhängige Hashes pro Buch:
- `metadata_hash` (MD5 über Calibre-Felder: `comments`, `tags`, `title`, `author`, `publisher`)
- `annotation_hash` (MD5 über alle Annotationstexte eines Buchs, sortiert für Determinismus)

**Begründung:** Die vier Datentypen eines indexierten Buchs – Volltext-Chunks, Calibre-Kommentar-Chunk, Metadaten-Felder in allen Chunks, Annotation-Chunks – haben fundamental verschiedene Änderungszyklen:

| Datentyp | Ändert sich... | Häufigkeit |
|----------|---------------|------------|
| Volltext | Nie (Datei ist immutabel) | — |
| Calibre-Kommentar | Selten (Verlagstext, Klappentext) | ~1-2× pro Buch |
| Metadaten (Tags, Titel, Autor) | Gelegentlich (Kuratierung) | ~10-50× über Bibliotheksleben |
| Annotationen | Laufend (Lesefortschritt) | Kontinuierlich |

Statt für jede Änderung alles neu zu indexieren, erkennt das System jetzt via Hash-Vergleich, *was* sich geändert hat, und aktualisiert nur den betroffenen Teil:

```
Entscheidungsbaum in index_book() (force=False, Content-Chunks vorhanden):
1. metadata_hash UND annotation_hash geändert → beides updaten (~2-3s)
2. metadata_hash geändert, annotation_hash gleich → nur Metadaten updaten (~1s)
3. metadata_hash gleich, annotation_hash geändert → nur Annotationen updaten (~2s)
4. beide gleich → komplett überspringen (~0.1s)
```

**Implementierung:** `metadata_hash` wird in jedem Chunk gespeichert (ermöglicht Batch-Updates via `LanceDBStore.update_metadata_fields()`). `annotation_hash` wird nur in Annotation-Chunks gespeichert. Bei Änderung werden alte Annotation-Chunks via `delete_by_book_id_and_type()` gelöscht und neue mit frischen BGE-M3-Embeddings eingefügt.

**Konsequenz für Batch-Indexierung:** `batch_index.py --skip-existing` überspringt Bücher nicht mehr blind, sondern leitet alle Bücher an `index_book()` weiter, das die Hash-Prüfung durchführt. Ein Batch-Lauf über 670 Bücher, bei dem sich nichts geändert hat, dauert damit ~67 Sekunden statt ~16 Stunden.

### ADR-012: Annotation-Indexierung in LanceDB (Februar 2026)

**Kontext:** Annotationen – Highlights und Notizen, die der Nutzer in seinen Büchern hinterlässt – sind für Geisteswissenschaftler oft wertvoller als der Rohtext. Sie repräsentieren kuratiertes Wissen: die Passagen, die der Forscher als relevant markiert hat, und seine Gedanken dazu. ARCHILLES extrahierte Annotationen bereits über MCP-Tools (`get_book_annotations`, `search_annotations`), speicherte sie aber in einem separaten ChromaDB-Index mit einem anderen Embedding-Modell (siehe ADR-008).

**Entscheidung:** Annotationen werden als `chunk_type='annotation'` in der LanceDB-`chunks`-Tabelle gespeichert, mit BGE-M3-Embeddings, als Teil des regulären Indexierungslaufs (Phase 2).

**Annotation-Quellen:** Zwei Quellen werden automatisch zusammengeführt:
- **Calibre-Viewer-Annotations:** JSON-Dateien in `%APPDATA%\calibre\viewer\annots\`, erzeugt beim Lesen von EPUBs im Calibre-Viewer.
- **PDF-native Annotations:** Highlights und Kommentare aus Adobe Reader (oder anderen PDF-Readern), extrahiert via PyMuPDF (`fitz`).

Die bestehende Funktion `get_combined_annotations()` aus `src/calibre_mcp/annotations.py` übernimmt die Zusammenführung mit intelligenter Filterung (TOC-Marker-Erkennung, Mindestlänge 20 Zeichen, erste 5% des Buchs ausgeschlossen).

**Text-Format der Annotation-Chunks:**
- Highlight: `[ANNOTATION] {hervorgehobener Text}`
- Highlight mit Notiz: `[ANNOTATION] {hervorgehobener Text} | Note: {Notiz}`
- Reine Notiz: `[ANNOTATION_NOTE] {Notiz}`

Das `[ANNOTATION]`-Präfix sorgt dafür, dass BGE-M3 den semantischen Kontext "Nutzermarkierung" mit einbettet, was bei der Suche nach nutzerkuratierten Inhalten die Relevanz erhöht.

**Neue LanceDB-Felder:**
- `annotation_type` (str): `'highlight'`, `'note'`, `'bookmark'`
- `annotation_source` (str): `'calibre_viewer'` oder `'pdf'`
- `annotation_hash` (str): Hash für Änderungserkennung (siehe ADR-011)

**Schema-Migration:** Die neuen Felder werden bei der ersten Nutzung automatisch via `table.add_columns()` zur bestehenden Tabelle hinzugefügt. Dieser Mechanismus wurde allgemein für alle zukünftigen Schema-Erweiterungen implementiert, sodass bestehende Indizes nie inkompatibel werden.

**Nicht-fatale Fehlerbehandlung:** Annotation-Extraktion ist in einen try/except-Block eingebettet. Wenn die Extraktion für ein Buch fehlschlägt (z.B. kein Annotations-Verzeichnis, korrupte JSON-Datei), wird eine Warnung geloggt, aber die Buchindexierung läuft normal weiter.

### ADR-013: Crash-sichere Backup-Strategie für LanceDB (Februar 2026)

**Kontext:** Die LanceDB-Datenbank für 670+ Bücher umfasst ca. 243.000 Chunks und belegt ~13 GB auf der Festplatte. Batch-Indexierung läuft über Stunden bis Tage. Ein Abbruch durch Systemabsturz, Stromausfall oder CTRL+C darf nicht zum Datenverlust führen.

**Entscheidung:** `SafeIndexer` (`scripts/safe_indexer.py`) erstellt periodische Kopien der LanceDB und begrenzt die Anzahl aufbewahrter Backups.

**Ursprüngliche Konfiguration:** Backup alle 10 Bücher, maximal 5 Backups.

**Problem:** Bei ~13 GB pro Backup und 5 aufbewahrten Kopien können bis zu 65 GB Backup-Daten anfallen. In der Praxis füllte dies die Festplatte während eines 3-tägigen Batch-Laufs über 445 Bücher.

**Korrigierte Konfiguration:** Backup alle 50 Bücher, maximal 2 Backups (~26 GB Maximum).

**Begründung der neuen Werte:**
- **Intervall 50:** Ein Verlust von maximal 50 Büchern (~75 Minuten Arbeit) ist bei einem Non-Production-System akzeptabel. Die `progress.db` (SQLite) trackt den Fortschritt buchgenau, sodass ein Neustart exakt dort fortsetzt, wo der Abbruch war – die Backups schützen nur gegen Korruption der LanceDB selbst.
- **Maximum 2:** Das vorletzte Backup dient als Fallback, falls das letzte Backup selbst korrupt sein sollte (z.B. bei Abbruch während des Backup-Vorgangs). Mehr als 2 Generationen bringen keinen zusätzlichen Schutz.

**Konsequenz:** Der `SafeIndexer` bleibt als Sicherheitsnetz erhalten, ist aber kein Engpass mehr. Für die Zukunft wäre ein inkrementelles Backup-Konzept denkbar (nur geänderte Lance-Fragmente kopieren), aber bei der aktuellen Datenbankgröße ist die einfache Kopie-Strategie ausreichend.

### ADR-014: Quality-Based Format Selection statt statischer Priorität (März 2026)

**Kontext:** ADR-010 legte eine statische Priorität fest: PDF > EPUB > sonstige Formate. Diese Regel ist zu grob. In der Praxis gibt es EPUB-Konvertierungen aus Scan-PDFs, die massive Truncation-Fehler und inkohärente Chunks produzieren – während das Original-PDF strukturell sauber ist. Umgekehrt gibt es OCR-PDFs, bei denen das EPUB deutlich bessere Chunk-Qualität liefert. Eine statische Rangfolge wählt systematisch das falsche Format.

**Entscheidung:** Bei mehreren verfügbaren Formaten (PDF, EPUB, MOBI/AZW3) wird das qualitativ bessere automatisch ausgewählt. Das System bereitet beide Formate temporär vor und vergleicht anhand eines Scores (0–100). CLI-Flag: `--quality-select [--prefer-format pdf|epub]`.

**Score-Kriterien:**

| Kriterium | Gewicht | Beschreibung |
|-----------|---------|--------------|
| Truncation Rate | −30 | Chunks die mitten im Satz enden |
| Misplaced Back Matter | −25 | Bibliographie/Index vor den letzten 10% |
| Chunk Length Variance | −10 | Ungleichmäßige Chunk-Längen |
| Very Short Chunks | −15 | Chunks <50 Wörter |
| Section Coverage | +10 | Chunks mit chapter/section_title |
| Page Metadata | +5 | Seitenzahlen vorhanden (PDF-Bonus) |

**Begründung:** EPUB ist nicht grundsätzlich besser als PDF – die Qualität hängt von der Konvertierungshistorie des konkreten Titels ab. Ein score-basiertes System trifft diese Entscheidung datengetrieben statt dogmatisch. Der `--prefer-format`-Parameter ermöglicht es, bei Gleichstand eine Präferenz zu setzen (z.B. PDF für Seitenzahlen-Garantie).

**Implementierung:** `batch_index.py:124-288`. Beide Formate werden temporär verarbeitet; nur das gewählte Format wandert in die Pipeline. Die Qualitätsanalyse läuft vollständig CPU-seitig (kein Embedding erforderlich) und wird durch die Skip-Existing-Optimierung (s.u.) ohnehin nur bei Erstindexierungen ausgeführt.

**Weitere Fixes im März 2026 in diesem Kontext:**
- **MOBI/AZW3 einbezogen** (commit b4c0a7a): Auch diese Formate nehmen am Qualitätsvergleich teil.
- **Skip-Existing vor Quality-Vergleich** (commit 192593b): Wenn für ein Buch bereits ein JSONL existiert, wird die teure Qualitätsanalyse übersprungen. Performance-Gewinn bei Batch-Läufen über teilweise vorbereitete Bestände erheblich.
- **Back-Matter-Heuristik entfernt** (commit eabed12): Die positionsbasierte Heuristik, die Bibliographien und Indizes anhand ihrer Zeichenposition im Dokument erkannte, wurde vollständig entfernt. Sie war strukturell unzuverlässig und produzierte False Positives. Der Quality-Score berücksichtigt Misplaced-Back-Matter jetzt als Scoring-Kriterium statt als Filter – ein Befund fließt in die Formatwahl ein, blockiert aber keine Chunks mehr.

### ADR-015: Two-Phase Indexing Pipeline – Prepare/Embed (März 2026)

**Kontext:** Die bisherige Pipeline koppelte Textextraktion (CPU-intensiv, kein GPU erforderlich) und Embedding-Erzeugung (GPU-intensiv) in einem einzigen Durchlauf. Das erzeugte zwei praktische Probleme: Erstens musste bei jedem Neustart der gesamte Prozess für ein Buch wiederholt werden, selbst wenn die Textextraktion bereits erfolgreich war. Zweitens war die Pipeline nicht auf verteilte Setups vorbereitet, bei denen Extraktion und Embedding auf verschiedenen Maschinen laufen.

**Entscheidung:** Entkopplung in zwei explizite Phasen:
- **Phase 1 – Prepare** (`--prepare-only --output-dir ./prepared_chunks`): Textextraktion, Chunking, Metadaten-Anreicherung → JSONL-Dateien pro Buch (benannt nach Calibre-ID). Läuft vollständig CPU-seitig.
- **Phase 2 – Embed** (`python scripts/rag_demo.py embed --input-dir ./prepared_chunks --mode local|remote`): Liest JSONLs, erzeugt BGE-M3-Embeddings, schreibt in LanceDB. Progress-Tracking via `.embed_checkpoint.json` (Resume bei Abbruch). Remote-Modus für externen Embedding-Server.

**JSONL-Format:** Header-Zeile mit Buch-Metadaten, anschließend eine Zeile pro Chunk (Chunk-Daten ohne Vektoren). Dateinamen entsprechen der Calibre-ID (`{calibre_id}.jsonl`).

**Begründung:** JSONL als Zwischenformat ist menschenlesbar, inspizierbar und portierbar. Die Trennung ermöglicht:
- GPU-unabhängige Extraktion auf Systemen ohne dedizierte GPU
- Batch-Embedding auf einem Remote-Server (z.B. mit stärkerer GPU)
- Inspektion und manuelle Korrektur zwischen den Phasen
- Resume-Fähigkeit: Abgebrochene Embedding-Läufe setzen an der letzten erfolgreich verarbeiteten Datei fort, nicht am Anfang

**Companion-Tool – Chunk Inspector** (`scripts/chunk_inspector.py`): Mit der Two-Phase-Pipeline entstand ein Diagnostik-Tool, das JSONL-Dateien und LanceDB-Einträge analysiert:
- Metadaten-Abdeckung (chapter, section_title, page_label)
- Chunk-Statistiken (Wortanzahl min/max/mean/median)
- Truncation-Erkennung (abgeschnittene Sätze)
- TOC-Alignment (Kapitelzuordnung prüfen)
- Unterstützt LanceDB (`--calibre-id`) und JSONL (`--jsonl`)

Der Inspector ist kein Produktions-Feature, sondern ein Entwicklungs- und Debugging-Werkzeug. Er macht die Qualität des Zwischenformats sichtbar, bevor Embedding-Ressourcen investiert werden.

**Implementierung:** `rag_demo.py:1146-1463`, `batch_index.py`.

### ADR-016: Calibre-Kommentare in der Prepare-Only-Pipeline (März 2026, commit 221610c)

**Kontext:** Calibre-Kommentare (Klappentexte, Verlagsbeschreibungen, persönliche Exzerpte) wurden bislang als `calibre_comment`-Chunks nur in der vollständigen Indexierungspipeline erzeugt – also immer gemeinsam mit dem Embedding-Schritt. In der Two-Phase-Pipeline (ADR-015) fehlten Comment-Chunks daher in den JSONL-Dateien, wenn `--prepare-only` verwendet wurde.

**Entscheidung:** `_build_comment_chunks(embed=False)` – Comment-Chunks werden jetzt auch in der Prepare-Phase ohne GPU erzeugt und in das JSONL geschrieben. Die Embedding-Erzeugung erfolgt in Phase 2 gemeinsam mit den Content-Chunks.

**Konsequenzen:**
- `prepare_book()` erzeugt jetzt vollständige JSONLs mit `calibre_comment`-Chunks
- Scan-/OCR-Erkennung (Warnung bei unzureichender Textdichte) läuft ebenfalls im prepare-only-Pfad
- `scripts/patch_comments.py`: Nachträgliches Patchen bestehender JSONLs, die vor diesem Fix erstellt wurden. Erlaubt Migration des bestehenden Bestands ohne vollständige Neuextraktion.

**Begründung:** Die Vollständigkeit des JSONL-Formats ist eine Invariante der Prepare-Phase. Wenn Phase 1 abgeschlossen ist, soll Phase 2 keine Metadaten mehr aus Calibre nachlesen müssen – alle Entscheidungen über Chunk-Inhalt fallen in Phase 1. Das `patch_comments.py`-Skript ermöglicht den sanften Übergang für bestehende Datenbestände.

### ADR-017: Chunk-Splitting bei Calibre-Kommentaren (März 2026)

**Kontext:** Calibre-Kommentarfelder können sehr lang sein – insbesondere wenn der Nutzer ausführliche persönliche Exzerpte, NotebookLM-Ausgaben oder mehrseitige Verlagstexte ablegt. Ein einzelner `calibre_comment`-Chunk mit 2.000+ Wörtern beeinträchtigt die Retrieval-Qualität erheblich, weil BGE-M3's Kontext-Fenster dann nicht mehr optimal genutzt wird.

**Entscheidung:** Calibre-Kommentare werden bei >400 Wörtern an Satzgrenzen gesplittet. Jeder Teil-Chunk erbt die Buch-Metadaten und erhält einen `chunk_index`-Zähler zur Rekonstruktion der Reihenfolge.

**Begründung:** Die Retrieval-Qualität von BGE-M3 sinkt messbar bei Chunks >500 Wörtern. Die Grenze 400 Wörter gibt Puffer vor dem Qualitätsabfall und entspricht ungefähr dem Optimum für dichte akademische Prosa. Satzgrenzen statt willkürliche Zeichenpositionen (wie beim alten Fixed-Size-Chunker) verhindern semantisch zerrissene Chunks – dasselbe Prinzip wie beim Sentence-Aligned EPUB Chunking (ADR-018).

**Implementierung:** Splitter nutzt Regex-basiertes Sentence-Splitting (`re.split(r'(?<=[.!?])\s+', ...)`) an Satzgrenzen. Maximal-Chunk-Größe konfigurierbar, Default 400 Wörter.

### ADR-018: Sentence-Aligned EPUB Chunking (März 2026, commit 6982171)

**Kontext:** Der bisherige EPUB-Chunker teilte Text an Zeichenpositionen (Fixed-Size mit Overlap). Das produzierte regelmäßig Chunks, die mitten in einem Satz endeten – ein bekanntes Problem, das sich im Quality-Score (ADR-014) als "Truncation Rate" niederschlägt und bei geisteswissenschaftlichen Texten mit langen Satzgefügen besonders störend ist.

**Entscheidung:** EPUB-Chunks enden grundsätzlich an Satzgrenzen, nicht an Zeichenpositionen. Die Implementierung nutzt Regex-basiertes Sentence-Splitting an Interpunktionsgrenzen (`.`, `!`, `?`).

**Begründung:** Ein Chunk, der mitten in einem Satz endet, produziert zwei Schäden: Er kürzt die semantische Einheit des aktuellen Chunks, und er fügt dem folgenden Chunk ein syntaktisch verwaistes Satzfragment voran. Beide Effekte degradieren die Embedding-Qualität. Sentence-Alignment bei EPUB-Chunking ist die Analogie zu strukturorientiertem Chunking bei PDFs – es respektiert die natürlichen Grenzen des Textes statt arbiträrer Byte-Offsets.

**Konsequenz:** Die Chunk-Längen variieren jetzt leicht um den Ziel-Wert, sind aber semantisch kohärent. Der Quality-Score (ADR-014) zeigt nach dieser Änderung eine deutlich niedrigere Truncation Rate für EPUB-basierte Bücher. Auch das Truncation-Scoring im Quality-Select-Vergleich wurde im selben Commit verfeinert.

### ADR-019: Stop-Word-Entfernung für 12 Sprachen (Februar 2026)

**Kontext:** Hybrid-Search (ADR-006) kombiniert semantische und Keyword-Suche. Die Keyword-Komponente (BM25) wertet Terme nach Häufigkeit und Selektivität – Stop-Words wie "der", "the", "et" oder "και" tragen dabei nichts zum Retrieval bei, erzeugen aber Rauschen und verlangsamen die Indexierung.

**Entscheidung:** Explizite Stop-Word-Listen für 12 Sprachen werden vor der Keyword-Indexierung angewendet: Englisch, Deutsch, Französisch, Spanisch, Italienisch, Portugiesisch, Niederländisch, Latein, Russisch, Neugriechisch, Hebräisch, Arabisch.

**Begründung:** Die Sprachpalette deckt den realistischen Kern einer geisteswissenschaftlichen Bibliothek ab. Latein, Neugriechisch, Hebräisch und Arabisch sind für Altphilologen und Theologen unverzichtbar und in keiner Standard-NLP-Bibliothek als vollständige Liste enthalten – sie wurden manuell kuratiert. Die Stop-Word-Entfernung verbessert BM25-Precision und reduziert Index-Größe ohne jeden Recall-Verlust bei inhaltlich relevanten Termen.

**Implementierung:** `rag_demo.py:76-120`. Die Spracherkennung erfolgt über `language_detector.py` (Lingua-basiert); bei mehrsprachigen Büchern werden die Stop-Word-Listen der erkannten Sprachen vereinigt.

### ADR-020: Confidence Threshold (min_similarity) für semantische Ergebnisse (Februar 2026)

**Kontext:** BGE-M3 berechnet immer einen Similarity-Score, auch wenn ein Chunk thematisch völlig unpassend ist. Ohne Untergrenze werden bei semantischer Suche auch Ergebnisse mit Score 0.3–0.4 zurückgegeben – was bei geisteswissenschaftlichen Anfragen mit präziser Terminologie störend wirkt: Der Forscher bekommt vage assoziierte Passagen statt nichts.

**Entscheidung:** Ein konfigurierbarer `min_similarity`-Schwellwert filtert semantische Ergebnisse, die unterhalb der Qualitätsgrenze liegen. Default-Wert: 0.5 (empirisch ermittelt für wissenschaftliche Prosa in Deutsch/Englisch).

**Begründung:** "Kein Ergebnis" ist informativer als ein falsches Ergebnis. Der Schwellwert ist konfigurierbar, weil er von der Abfragesprache, dem Fachgebiet und der Schreibweise abhängt – für latinistische Texte ist 0.45 sinnvoller als für moderne Sozialwissenschaften. Die Einstellung ist über CLI (`--min-similarity`) und Web-UI zugänglich.

**Implementierung:** `rag_demo.py`, `web_ui.py`. Greift ausschließlich auf den semantischen Teil der Hybrid-Suche; Keyword-Matches werden nicht gefiltert.

### ADR-021: Source Adapter Architecture (Februar 2026)

**Kontext:** ARCHILLES wurde ursprünglich als Calibre-spezifisches System entwickelt. Nach dem ersten Beta-Feedback wurde klar, dass die Zielgruppe auch andere Wissensquellen integrieren möchte: Zotero-Bibliotheken, Obsidian-Vaults und schlichte Ordnerstrukturen. Eine monolithische Implementierung hätte jede neue Quelle zur Code-Änderung im Kern geführt.

**Entscheidung:** Einführung einer Source-Adapter-Schicht mit einheitlicher Schnittstelle. Aktuell implementiert: `CalibreAdapter`, `ZoteroAdapter`, `ObsidianAdapter`, `FolderAdapter`.

**Begründung:** Das Adapter-Pattern entkoppelt die Quellenlogik (wie Metadaten gelesen werden, wie Dateipfade aufgelöst werden) vom Indexierungs-Core. Neue Quellen können als eigenständige Adapter hinzugefügt werden, ohne Batch-Indexer oder Pipeline anzutasten. Die Schnittstelle definiert: `list_books()`, `get_metadata(book_id)`, `get_file_path(book_id)`. Der `FolderAdapter` ist zugleich die einfachste Implementierung und die Grundlage für alle nicht-Calibre-Setups.

**Implementierung:** `batch_index.py:720-772`. Der aktive Adapter wird über `--source calibre|zotero|obsidian|folder` gewählt. Calibre bleibt der Default.

**Folge-ADR:** → ADR-023: Structure-Aware PDF Chunking (implementiert März 2026)

---

### ADR-023: Structure-Aware PDF Chunking (März 2026)

**Kontext:** Eine Analyse mit dem Chunk Inspector (19. März 2026, `BRIEFING_STRUCTURE_AWARE_CHUNKING.md`) zeigte, dass PDF-Chunks 0% chapter/section_title-Abdeckung hatten, während EPUB-Chunks 96–100% erreichten. Der PDF-TOC wurde zwar extrahiert, aber nicht auf einzelne Chunks gemappt.

**Entscheidung:** TOC-zu-Seite-Mapping in `pdf_extractor.py` mit Junk-TOC-Filterung; `section_type` und `section_title` werden aus TOC-Titeln abgeleitet und in `ChunkMetadata` geschrieben.

**Begründung:** Strukturinformationen sind für zitierbare Retrievalergebnisse (Kapitelangabe, section_type-Filterung) unverzichtbar. Die EPUB-Pipeline hatte das Problem durch TOC-basiertes Parsing bereits gelöst; die PDF-Pipeline wurde auf denselben Stand gebracht. Die Filterung malformierter TOCs (zu kurze Titel, reine Zeichennummern) verhindert Rauschen in der Metadaten.

**Implementierung:** `src/extractors/pdf_extractor.py` — `_build_page_toc_map()`, `_section_type_from_toc_title()`, `_create_chunks_with_pages()`. Verifikation an Testbüchern (Eunapios, Golden Bough); Reports in `reports/chunk_inspection/`.

---

### ADR-025: Scheduled Routines — pragmatischer Schritt A vor Watchdog-Generalisierung (Mai 2026)

**Kontext:** Mit dem Unified MCP Server (April 2026) standen drei Quellen unter einem Dach: Calibre, Zotero und der Obsidian-Vault des Archilles Lab. Der echte Watchdog (Hash-Diff über Metadaten und Annotationen) existiert aber nur für Calibre — er liest direkt `metadata.db`. Das adapter-agnostische Interface (`compute_metadata_hash`, `compute_orphan_ids`) ist seit April vorbereitet, der `WatchdogScanner` selbst noch nicht generalisiert. Das MCP-Tool `watchdog_scan` ist im Unified-Server explizit über `_CALIBRE_ONLY_TOOLS` gegated. Für Lab und Zotero gab es keinen Routine-Pfad — manuelle `batch_index`-Aufrufe waren der einzige Weg.

**Entscheidung:** Statt den Watchdog selbst zu generalisieren („Schritt B"), wird zuerst eine pragmatische Lösung („Schritt A") ausgeliefert: ein Wrapper-Skript `scripts/run_routine.py`, das pro Source das passende Tool auswählt — `scripts/watchdog.py --json` für Calibre, `scripts/batch_index.py --all --skip-existing` für die übrigen Adapter. Throttling per Marker-Datei (`<library>/.archilles/last_routine_run.txt`), Frequenzen `daily` und `weekly`. Der Wrapper schreibt eine maschinenlesbare History (`routine_history.jsonl`), aus der ein Wochen-Status-Mailer (`scripts/weekly_status_mail.py`) automatisch sonntags eine Plaintext-Mail per Gmail SMTP versendet. Ergänzt um eine monatlich gegatete Vault-Linker-Routine (`scripts/run_link_vault.py`), die `link_vault.py --semantic --apply` ruft, sobald die Lab-Routine am selben Tag erfolgreich abgeschlossen ist.

**Warum nicht ein einheitliches Tool pro Source:** Die naheliegende Frage war „ein Tool für alle Quellen oder drei spezialisierte Tools?". Antwort: **eines mit `source`-Parameter**. Aggregiertes Verhalten ohne Argument, spezialisiertes mit Argument. So gibt es keine Code-Duplikate; die drei „spezialisierten Routinen" leben in drei Scheduler-Einträgen mit unterschiedlichem `--source`-Argument, nicht in drei separaten Skripten.

**Warum hartes Gating statt Delay beim Vault-Linker:** `link_vault.py --semantic` liest LanceDB-Embeddings, um semantische Nachbarn zwischen Notizen zu finden — die LanceDB muss frisch sein. Naheliegend wäre ein Trigger-Delay nach der Lab-Routine. Das skaliert aber nicht: bei einer kleinen GPU braucht ein voller Lab-Indexierungslauf Stunden, kein fester Delay-Wert deckt das robust ab. Stattdessen prüft der Vault-Linker den Lab-Marker direkt und überspringt sich selbst (mit dokumentiertem Grund), wenn die Lab-Routine an dem Tag noch nicht abgeschlossen ist. Beim nächsten Logon-Trigger versucht er es erneut. Skips sind kein Fehler, sondern werden in `vault_linker_history.jsonl` mit `reason` vermerkt und in der Wochen-Mail sichtbar.

**Konsequenzen:**
- Fünf Windows-Scheduler-Tasks (`Archilles-Routine-Calibre/Lab/Zotero`, `Archilles-Status-Mail`, `Archilles-Vault-Linker`), alle mit OnLogon-Trigger, registriert idempotent durch `scripts/install_scheduled_routines.ps1`
- Master-Config (`~/.archilles/config.json`) wird zur Quelle der Wahrheit — der Wrapper liest `library_path` und `adapter` pro Source
- Lab und Zotero finden so neue Dokumente, erkennen aber **keine** Metadaten- oder Annotation-Änderungen an bereits indexierten Dokumenten — das bleibt Schritt B vorbehalten
- Die Wochen-Mail liefert den Operationsbeleg ohne weitere Infrastruktur (keine Monitoring-Dashboards, kein externer Service, nur Plaintext über Gmail SMTP)
- Das MCP-Tool `watchdog_scan` bleibt vorerst Calibre-only; der „Jetzt-indexieren"-Knopf für Lab/Zotero im Web-Frontend ist explizit auf Schritt B vertagt

**Verworfen:** Den Watchdog jetzt schon zu generalisieren. Der Refactoring-Aufwand (Annotation-Cache pro Adapter, MCP-Tool-Routing, Test-Suite) lohnt sich erst, wenn der reale Bedarf nachweisbar ist. Für den 80%-Fall — neue Dokumente automatisch einsammeln und einen wöchentlichen Status-Beleg erhalten — reicht der pragmatische Wrapper.

---

### ADR-024: HTTP/SSE-Transport für den MCP-Server (April 2026)

**Kontext:** ARCHILLES kommunizierte ausschließlich über stdio mit MCP-Clients — das Modell von Claude Desktop und Gemini CLI, bei dem der Server als lokaler Subprocess läuft. ChatGPT Desktop, OpenAI Codex, Cursor und andere HTTP-basierte Clients erwarten dagegen einen HTTP/SSE-Endpunkt. Ohne diesen Transport ist ARCHILLES faktisch an Claude gebunden.

**Entscheidung:** Optionaler SSE-Transport über Starlette/Uvicorn, aktivierbar via `--transport sse [--host ...] [--port ...]` oder dem neuen `transport`-Block in `.archilles/config.json`. Stdio bleibt der Default; bestehende Claude-Desktop-Konfigurationen ändern sich nicht.

**Implementierungsdetails:**
- `mcp.server.Server` (low-level MCP SDK 1.21.2) übernimmt die Protokoll-Schicht (list_tools / call_tool)
- `mcp.server.sse.SseServerTransport` liefert die SSE-Verbindung; Starlette als ASGI-Framework, Uvicorn als Server
- Die bestehende `create_mcp_tools()`-Funktion bleibt unverändert; ihre dicts werden 1:1 in `mcp.types.Tool`-Objekte konvertiert
- Tool-Dispatch nutzt weiterhin `TOOL_MAP` → `CalibreMCPServer`-Methoden; weil diese blockierend sind, werden sie via `asyncio.get_running_loop().run_in_executor()` in einem Thread-Pool ausgeführt
- Optionaler Bearer-Token via `transport.auth_token`; Bind ausschließlich auf `127.0.0.1`, kein Remote-Access

**Warum nicht FastMCP:** FastMCP hätte die gesamte Tool-Registrierung auf Decorator-Basis umgeschrieben. `mcp.server.Server` erlaubt, `create_mcp_tools()` und `TOOL_MAP` unverändert zu lassen und nur den Transport-Layer hinzuzufügen.

**Konsequenzen:**
- Zwei Instanzen parallel möglich: eine stdio für Claude Desktop, eine SSE für ChatGPT (über `--port 8766`)
- Bei Port-Konflikt: klare Fehlermeldung mit Lösungshinweis, kein stummes Scheitern
- Windows-Firewall-Dialog erscheint beim ersten Start — für localhost kein Sicherheitsrisiko
- Event-Loop-Blocking bei langen RAG-Queries wird durch `run_in_executor()` verhindert

---

### ADR-031: Dual-era MCP — stdio spricht 2026-07-28 und alle Vorgängerversionen (August 2026)

**Kontext:** Die MCP-Spezifikation 2026-07-28 (final seit 28. Juli 2026) entfernt den `initialize`/`initialized`-Handshake und die `Mcp-Session-Id`. Protokollversion, Client-Identität und Capabilities reisen stattdessen in `_meta` bei *jedem* Request; das Protokoll ist damit ausdrücklich zustandslos definiert („A server processes each request independently; no state should be inferred from previous requests"). Zusätzlich wird `server/discover` für Server verpflichtend, Results tragen ein `resultType`, und es gibt neue Fehlercodes (`-32022` UnsupportedProtocolVersion, `-32021` MissingRequiredClientCapability); `-32002` ist zugunsten von `-32602` zurückgezogen.

Eine Bestandsaufnahme am 10. August 2026 ergab, dass ARCHILLES von den Deprecations praktisch nicht betroffen ist: weder Roots noch Sampling, MCP-Logging oder die experimentelle Tasks-API werden genutzt, `-32002` kommt im Code nicht vor, und es existiert kein Zustand über Call-Grenzen hinweg — Tools lesen Konfiguration und Dateien, nie vorangegangene Requests. Der Server ist also seit jeher zustandslos, ohne dass das je eine Entscheidung gewesen wäre. Zwei echte Lücken fanden sich dennoch: Der handgeschriebene stdio-Loop meldete eine fest verdrahtete `protocolVersion` `2024-11-05` ohne jede Verhandlung, und `server/discover` fehlte vollständig.

Der stdio-Pfad benutzt — anders als SSE und Streamable HTTP (ADR-024) — kein SDK, sondern spricht JSON-RPC direkt über stdin/stdout. Protokollkonformität ist dort Eigenverantwortung.

**Entscheidung:** Der stdio-Server wird **dual-era**: Ein Request mit `io.modelcontextprotocol/protocolVersion` in `_meta` wird nach 2026-07-28 bedient, ein `initialize` nach der ausgehandelten Legacy-Revision. Beides auf demselben Prozess, wie die Spec es unter „Backward Compatibility" vorsieht. Ein SDK-Upgrade auf `mcp` 2.0 unterbleibt vorerst.

**Implementierungsdetails:**
- `build_response()` als reine Funktion aus dem I/O-Loop herausgezogen — vorher war die Protokolllogik nicht testbar
- `negotiate_protocol_version()` echot die Client-Version, sonst die neueste Legacy-Revision; einem Legacy-Client wird nie eine moderne Version angeboten, weil er deren Handshake-Losigkeit nicht bedienen kann
- Moderne Results tragen `resultType: "complete"` (MUST) und `io.modelcontextprotocol/serverInfo` in `_meta` (SHOULD), Legacy-Results bewusst keins von beidem
- `server/discover` meldet alle unterstützten Versionen und `capabilities: {tools: {}}`
- Validierung: unbekannte Version → `-32022` samt `data.supported`, fehlende `clientCapabilities` → `-32602`; unbekannte `_meta`-Keys (`traceparent`, Vendor-Präfixe) werden ignoriert
- `tests/test_stdio_protocol.py` (34 Fälle) fixiert beide Ären

**Warum kein SDK-Upgrade auf `mcp` 2.0:** Der produktive Transport ist stdio, und der benutzt das SDK nicht — ein Upgrade würde dort nichts ändern. Die 1.x-Linie wird weiter gepflegt (1.29.0 erschien am selben Tag wie 2.0.0). Der handgeschriebene Loop ist hier ein Vorteil: Er ist von SDK-Breaking-Changes entkoppelt und übersteht den Wegfall des Handshakes, ohne dass eine Migration den produktiven Pfad gefährdet. Die Portierung von SSE/Streamable HTTP auf 2.0 bleibt aufgeschoben, bis der HTTP-Transport realen Bedarf hat (ChatGPT blockiert lokale Server weiterhin).

**Konsequenzen:**
- Claude Desktop und Claude Code funktionieren unverändert; verifiziert am 10.08.2026 gegen den laufenden Prozess in beiden Ären
- Die Kommunikationsaussage „built for stateless MCP" ist ab sofort belegbar — **für stdio**. SSE und Streamable HTTP sprechen weiter Legacy; jede öffentliche Aussage muss das qualifizieren, solange ADR-024s Pfade nicht nachgezogen sind
- Statelessness wird von einer stillschweigenden Eigenschaft zu einer zugesagten: Künftige Features dürfen keinen Zustand zwischen Tool-Calls aufbauen. Was über Requests hinweg leben muss, braucht einen expliziten Identifier im Tool-Parameter (Spec: „MUST be referenced by an explicit identifier the client passes on each request")
- Für ein perspektivisch gehostetes ARCHILLES entfällt der Session-Store als Architekturproblem — eine offengehaltene Option, keine Zusage

---

### ADR-026: Engine-Umzug nach src/archilles/engine mit Fassaden-Zerlegung (Juni 2026)

**Kontext:** Das vollständige Code-Review vom 10. Juni 2026 (~160 Befunde, `docs/internal/CODE_REVIEW_2026-06-10.md`) bestätigte eine Architektur-Inversion als strukturellen Hauptbefund (4.9/8.16): Die gesamte RAG-Engine — die Klasse `archillesRAG` mit ~2.600 Zeilen für Suche, drei Indexierungspfade, Smart-Update, Prepare/Embed, Prompt-Bau und Markdown-Export — lebte im CLI-Skript `scripts/rag_demo.py`. Produktionscode in `src/` (Watchdog, batch_index, Service) importierte aus `scripts/` (7.18); der MCP-Entry-Point legte zudem zwei Import-Wurzeln an, sodass dieselben Module unter zwei Identitäten geladen werden konnten (5.14), und der Unified-Server griff über `service._rag` auf private Interna durch (5.15).

**Entscheidung:** Die Engine zieht nach `src/archilles/engine/` um und wird entlang der natürlichen Nähte zerlegt: eine schlanke Fassade `ArchillesRAG` (`core.py`, 466 Zeilen) komponiert `Indexer` (`indexing.py`), `Searcher` (`search.py`) und `PromptBuilder` (`prompting.py`). `scripts/rag_demo.py` bleibt als dünner CLI-Wrapper (~480 Zeilen) mit Kompat-Alias `archillesRAG` erhalten. Begleitend: kanonische `src.*`-Import-Wurzel im Entry-Point, öffentliche Service-Methode `build_claude_prompt` statt `_rag`-Durchgriff, Bruch des Import-Zyklus service↔rag_demo über die neutrale Schicht `src/retriever/results.py`.

**Begründung und Vorgehensentscheidungen:**
- **Erst 1:1-Umzug, dann Zerlegung (getrennte Commits):** Der mechanische Move (2.591 Zeilen byte-identisch, verifiziert per Diff) trennt das Umzugsrisiko vom Zerlegungsrisiko; jeder Schritt ist einzeln bisect- und revertierbar.
- **Rückreferenz-Muster statt Dependency Injection:** Die Komponenten halten `self._rag` und lesen geteilten Zustand (`store`, `embedding_model`, …) über die Fassade. Damit bleiben Attribut-Mutationen durch Aufrufer wirksam (Tests ersetzen z. B. `rag.embedding_model` durch ein Fake) — eine saubere DI hätte den byte-treuen Move unmöglich gemacht und das Verhaltensrisiko vervielfacht.
- **Fassaden-Delegatoren mit 1:1-Signaturen:** Nur extern aufgerufene Methoden erhalten Delegatoren (keine `*args/**kwargs`-Abkürzungen). Die statischen Delegatoren `_compute_metadata_hash`/`_compute_annotation_hash` sind bewusst erhalten — sie sind die mock.patch-Targets der Watchdog-Tests und werden von `watchdog.py` klassenseitig aufgerufen.
- **Öffentliche API unverändert:** Konsumenten (MCP-Server, Web-UI, CLI, Watchdog, batch_index) rufen weiter dieselben Methoden mit denselben Signaturen.

**Konsequenzen:**
- `rg "from scripts" src/` ist leer — die Inversion ist beseitigt; ein Subprocess-Regressionstest (`tests/test_engine_move.py`) sichert das dauerhaft ab, ergänzt um Quelltext-Ratschen gegen Rückfälle (z. B. `service._rag` im Unified-Server)
- Suite nach Abschluss: 483 Tests grün; Umsetzung als PR #33 (16 Commits, Merge-Commit ohne Squash für Nachvollziehbarkeit), Subagent-Driven Development mit zweistufigem Review pro Task
- Bewusst zurückgestellt (P2-Folge-Etappen): Packaging via pyproject.toml, der vorbestehende Eager-Import von SentenceTransformer beim Engine-Import, der Schichtungs-Smell `engine → calibre_mcp.annotations` (aus dem Monolithen übernommen, kein Zyklus)

**Verworfen:** Die Engine als ein flaches Modul statt Subpackage zu verschieben (hätte den 2.600-Zeilen-Monolithen nur umgetopft, 8.16 verlangt die Zerlegung); die Zerlegung im CLI-Skript zu belassen; Delegatoren mit `*args/**kwargs` (hätte Signatur-Drift und Patch-Target-Brüche kaschiert).

---

### ADR-028: Hardware-Stufen 2.0 — „Ein Ziel, mehrere Wege" (Juni 2026)

**Kontext:** Die drei Indexierungsprofile `minimal`/`balanced`/`maximal` waren faktisch nur eine `batch_size`-Staffel (8/32/64). `embedding_model` war überall BGE-M3, `device` wurde ohnehin auto-detektiert, und `chunk_size`/`chunk_overlap`/`max_parallel_docs`/`max_tokens_per_chunk`/`embedding_dimension` waren tote Felder (Chunking hartkodierte 512/128). Die eigentlichen Qualitäts- und Durchsatz-Hebel — Parent-Child (`--hierarchical`, Parent-Budget 2048) und lokales vs. externes Embedding (Remote-Embedder über `config.json`) — lagen **komplett außerhalb** der Profile. Zugleich hatte sich ein Default-Wildwuchs angesammelt (512/128 vs. 1024/128 vs. 512/64 vs. 1000/200). Vor allem aber deckte die Profil-Philosophie „Qualität ist überall gleich, nur Geschwindigkeit variiert" die Realität nicht mehr: Schwache Hardware (hier real verfügbar: eine 4-GB-GPU) kann hierarchisches Embedding lokal nicht in vernünftiger Zeit erreichen.

**Entscheidung:** Sauberer Schnitt statt Erweiterung von `IndexingProfile`. Die vermischte Profilklasse wird durch drei getrennte Begriffe ersetzt: `HardwareCapabilities` (auto-detektiert, baut auf vorhandenem `HardwareProfile` auf) / `IndexRecipe` (hardware-**unabhängig**, eine Wahrheitsquelle für Modell, Dimension und Chunk-Schema) / `ExecutionPlan` (abgeleitet aus Capabilities + Recipe). Nach außen gibt es **keine** 10 Klassen, sondern „Auto-Erkennung + eine Variable": `mode` in `.archilles/config.json` (CLI-Override `--mode`) mit den Werten `auto | light | full-local | full-external`. Die fünf internen Hardware-Klassen (`cpu-only`, `apple-mps`, `gpu-small` <8 GB, `gpu-mid` 8–<16 GB, `gpu-large` ≥16 GB) sind reine Implementierungsdetails von `plan()` und kollabieren auf drei verständliche Wege: **light** (flach, lokal, gratis), **full-local** (hierarchisch, lokal) und **full-external** (hierarchisch, lokal vorbereitet + extern embeddet). Unter `auto` wählt `plan()` selbst: fähige HW → `full-local`, schwache HW → `light`; `auto` verlangt **nie** ungefragt externes Embedding.

**Begründung und Schichtung:** Leitprinzip ist nicht „schlechtere Qualität für schwache Hardware" (das erzeugt inkompatible DBs), sondern **unterschiedliche Wege zum selben Index**, über vier Schichten getrennt:
- **Identität** (Modell BGE-M3, Dimension 1024, Metrik) — variiert **nie**, sonst werden Vektoren maschinenübergreifend inkompatibel.
- **Index-Qualität** (flach ↔ hierarchisch) — *degradiert-kompatibel*: eine flach indexierte DB funktioniert, das Retrieval fällt für solche Chunks sauber auf `window_text` zurück (kein Datenverlust, nur kein Small-to-Big).
- **Such-Qualität** (Reranking an/aus + Device) — DB-neutral, daher überall Default an; CPU für schwache Klassen, GPU ab `gpu-mid` (BGE-M3 + bge-reranker je ~2,5 GB ⇒ ~6–7 GB Spitze gemeinsam, deshalb die 8-GB-Schwelle).
- **Durchsatz** (batch/device, lokal ↔ extern) — qualitätsneutral. Über den externen Weg entstehen **exakt dieselben Vektoren** wie auf einer starken GPU.

`remote` ist damit kein eigener Modus, sondern ein Querschnitt — das hält das System local-first (extern = Opt-in). Entscheidend für die Testbarkeit: `plan(capabilities, recipe, mode)` ist eine **reine Funktion** und mit synthetischen Specs vollständig CI-testbar, obwohl physisch nur `gpu-small` vorliegt.

**Inkrementeller Nachschub (full-external).** Bulk-`full-external` ist automatisch prepare-only (prepare → extern embedden → `rag_db` zurück). Der laufende Nachschub (Watchdog, manuelles `index_book`) kann nicht jedes Mal sofort extern embedden, würde aber ohne Behandlung durchs Raster fallen. Lösung (Trickle-Upgrade): neue Titel werden sofort **provisorisch light** indexiert (flach, lokal, sofort durchsuchbar) und per Marker `pending_external` an den Chunks vermerkt; ein späterer `--prepare-pending-external`-Lauf bereitet sie hierarchisch auf, und das bestehende `embed --mode remote` ersetzt sie automatisch (die frischen Chunks tragen keinen Marker → implizit gelöscht). Der Marker an den Chunks (statt einer Queue-Datei) hält die DB als einzige Wahrheitsquelle und nutzt den risikolosen Schema-Migrations-Mechanismus von `lancedb_store.py`; „bewusst light" (final) vs. „provisorisch light" (wartend) lässt sich über `chunk_type` allein nicht unterscheiden — dafür braucht es genau diesen Marker.

**Konsequenzen:**
- Umgesetzt in sechs Etappen, durchgängig TDD (Suite 672 → 752): E1 `IndexRecipe` (eine Chunk-Param-Quelle, tote Felder entfernt), E2 `classify_hardware` + reine `plan()`/`ExecutionPlan`, E3 `mode`-Verdrahtung (config + CLI, `core.py` konsumiert den Plan), E4 Watchdog-Angleichung (beide Scanner mode→plan) + full-external-Trickle-Queue (`pending_external`), E5 Doku (`docs/USAGE.md` § „Indexing mode"), E6 dieser ADR.
- `--profile minimal/balanced/maximal` bleibt als Legacy-/Fortgeschrittenen-Override erhalten (umgeht `--mode`/auto), damit bestehende Skripte und Power-User unberührt bleiben.
- Identität bleibt fix ⇒ Datenbanken bleiben maschinenübergreifend kompatibel und die Suche reproduzierbar, unabhängig vom gewählten Weg.

**Verworfen:** `IndexingProfile` erweitern statt sauber schneiden (hätte die Drei-Dinge-Vermischung zementiert); die 10 Kombinationen (5 HW × extern) nach außen zeigen (widerspricht „maximal einfach"); externes Embedding als Cloud-Feature ausbauen (bräche local-first und wäre volumenintensiv — Voll-Auslagerung bleibt ein dokumentiertes LAN-Szenario); das Embedding-Modell oder die Dimension je Hardware variieren (bräche die DB-Kompatibilität); eine separate Queue-Datei statt des Chunk-Markers (Drift-Risiko bei Löschung/Reindex). Offen (Umsetzungsdetail): Form des Fortgeschrittenen-Overrides (`--hardware-class` erzwingen?) und das exakte `rag_db`-Rückspiel-Vorgehen für das LAN-Szenario; der Modularpfad `chunkers/` wird vorerst nicht mitgezogen.

---

### ADR-030: Benchmark-Harness als Messinstrument und Parent-Child-Gate (Juli 2026)

**Kontext:** Die Roadmap führt seit ADR-022 eine „Benchmark-Suite" in v1.0, bisher unspezifiziert. Zugleich hinterließ die Parent-Child-Validierung vom 17. Juni 2026 (ADR-027, siehe Abschnitt IV) zwei offene Punkte: der `parent_id`-Lookup ist toter Pfad (bei gefülltem `window_text` gewinnt im `PromptBuilder` immer `window_text`; Parents werden embeddet, aber nie gesucht oder genutzt), und ob die Parent-Ebene ihre ~25–30 % Mehr-Vektoren rechtfertigt, ist ungeklärt. Die Kopplungs-Hypothese — *das Benchmark ist das Messinstrument, das die Parent-Child-Go/No-Go-Entscheidung empirisch statt spekulativ macht* — wird bestätigt: Ohne Messinstrument wäre die Default-Schaltung eine Wette mit 25–30 % Index-Aufblähung als Einsatz; mit Messinstrument ist sie eine Ablesung.

**Entscheidung:** Ein bewusst minimales, reproduzierbares Benchmark-Harness auf dem eigenen Problemraum. Vier Komponenten:

1. **Goldset** — versionierte JSONL-Dateien im Repo (`benchmarks/goldset/`), `schema_version` im Header. Pro Fall: Query, Sprache, Suchmodus, optionale Filter, Kategorie (`known_item` / `thematic` / `multilingual` / `annotation` / `citation` / `negative`), Akzeptanz-Spezifikation (Buch-Referenz, optional Seitenbereich ±1, optional Pflicht-Textstelle), Gewicht. Die Kategorie `negative` (angelehnt an den „Source-Swap-Test" aus Dritt-Benchmarks zu NotebookLM) enthält Fälle, deren Antwort *nicht* im Korpus liegt — Erfolg ist hier, dass kein Treffer über der Konfidenz-Schwelle (ADR-020, `min_similarity`) zurückkommt. Das misst die Fehlalarm-Neigung des Retrievals, die für die wissenschaftliche Glaubwürdigkeit ebenso zählt wie der Recall, und muss im Schema v1 enthalten sein (nachträglich wäre es ein Schema-Bruch). **Das Goldset-Schema ist die eine teure Entscheidung** — wird es später gebrochen, werden alle historischen Messungen unvergleichbar. Deshalb: Schema klein halten, versionieren, Erweiterung nur additiv. Die Fälle selbst sind Kurationsarbeit (Korpus-Kenntnis nötig); Startgröße ~10, Zielgröße 40–60, das Harness läuft ab N=1.
2. **Metrik-Modul** — Hit@k, Recall@k, MRR, nDCG@10, Citation-Accuracy (Seite im Toleranzbereich), als reine, CI-testbare Funktionen. Dazu **Citation Integrity** als Verifikations-Pass im Runner (nicht im Metrik-Modul): Für Treffer mit Seiten-Metadaten wird stichprobenartig geprüft, ob der Chunk-Text an der zitierten Stelle der *Quelldatei* tatsächlich steht (read-only über die vorhandenen Extraktoren). Das misst Metadaten-Drift der Extraktion — den von der Marktaufklärung identifizierten Hauptschmerzpunkt der Zielgruppe (halluzinierte bzw. falsche Zitationen) — und ist die Metrik mit der schärfsten Launch-Relevanz.
3. **Runner** — `scripts/benchmark.py` treibt `ArchillesService.search` (strikt read-only gegen die Produktions-DBs), schreibt versionierte JSON-Reports plus Markdown-Zusammenfassung; A/B-Vergleich zweier Reports (z. B. Reranker an/aus, flach vs. hierarchisch) mit Deltas pro Kategorie.
4. **Kontext-Pfad-Instrumentierung** — jedes Suchergebnis weist aus, woher der gelieferte Kontext stammt (`window_text` / `parent` / keiner). Nur so ist messbar, ob die Parent-Ebene überhaupt beiträgt — der Kern der ADR-027-Frage.

**Umsetzungsreihenfolge** (acht Schritte, je ein Schritt pro Session, TDD, direkt auf `main`): (1) Goldset-Schema und Loader, (2) Metrik-Modul, (3) Runner, (4) Report-Vergleich, (5) Kontext-Pfad-Instrumentierung, (6) hierarchischer Teilbestand für das A/B, (6b) Kandidaten-Embedder für dasselbe A/B, (7) Citation-Integrity-Pass, (8) unabhängig davon das `ask`-Kommando als Souveränitäts-Exit. Die Schritte 1–4 sind ab einem 10-Fälle-Goldset nützlich; Schritt 5 muss vor der Interpretation des A/B aus Schritt 6 stehen, sonst ist die Parent-Frage aus ADR-027 nicht beantwortbar.

**Parent-Child-Entscheidungsverfahren:** (1) Baseline flach auf dem Produktionsbestand messen. (2) Teilbestand (~100 Bücher, per Tag ausgewählt) hierarchisch in ein **separates** `rag_db`-Verzeichnis indexieren — kein Voll-Reindex vor Evidenz; die VRAM-Messung am 4-GB-Gerät fällt dabei ab (ADR-028: lokal vs. `full-external`). (3) A/B auf den Goldset-Fällen, die den Teilbestand treffen. Entscheidungsregel: Default-Schaltung nur, wenn Citation-Accuracy und Kontext-Qualität messbar gewinnen, ohne Recall-Regression; die Parent-*Vektor*-Ebene (Parents durchsuchbar embeddet) bleibt nur, wenn die Instrumentierung zeigt, dass Parent-Kontext den `window_text`-Pfad tatsächlich schlägt — andernfalls wird sie verschlankt (Parents als reine Kontext-Records ohne eigene Embeddings), was die 25–30 % Mehr-Vektoren einspart.

**Zweites Gate am selben Instrument: Embedding-Kandidaten.** Die Identitäts-Schicht (ADR-028: BGE-M3, 1024 Dim.) bekommt vor dem gebündelten Reindex genau ein Revisions-Fenster — danach ist sie für lange Zeit fixiert. Anlass: Qwen3-Embedding führt das multilinguale MTEB-Leaderboard (8B: 70,58 vs. BGE-M3 ~67; die 4B-Variante hält ~67 bei ~2,5 GB VRAM Q4 — potenziell 4-GB-tauglich) und bringt eine Instruction-Architektur (+1–5 % durch Task-Prefixe). Zwei Einordnungen dämpfen den Leaderboard-Reflex: Erstens sagt MTEB wenig über Deutsch/Latein/Griechisch auf geisteswissenschaftlichem Korpus — das Prinzip der Quellen selbst lautet „test on your own data", und genau dafür wird das Harness gebaut. Zweitens bindet BGE-M3s Alleinstellungsargument (dense + sparse in einem Modell) ARCHILLES *nicht*: Die Keyword-Seite der Hybrid-Suche läuft über LanceDBs BM25-FTS, BGE-M3s Sparse-Modus wird nicht genutzt. Verfahren: Der ~100-Bücher-Teilbestand wird zusätzlich mit dem/den Kandidaten (Qwen3-Embedding-4B quantisiert; 8B nur falls extern embeddet) in je ein eigenes Verzeichnis indexiert und im selben Goldset-A/B gemessen — besonders die multilingualen und `negative`-Fälle. Entscheidungsregel: Wechsel **nur bei deutlichem, kategorien-konsistentem Gewinn** auf dem eigenen Korpus (die Wechselkosten sind Voll-Reindex, Identitätsbruch aller bestehenden DBs, Embedder-Integration mit Instruction-Prefixen); bei Gleichstand gewinnt der Bestand (BGE-M3). Der gebündelte Reindex wartet auf **beide** Gates — Parent-Child *und* Embedder —, damit er einmal läuft, nicht zweimal.

**Randbedingungen:** Benchmark-Läufe sind retrieval-only (kein LLM-Judge in v1 — Hardware- und Token-Budget), CPU-tauglich, cross-platform (`pathlib`, UTF-8), Code Englisch. Die produktiven `rag_db`-Verzeichnisse der konfigurierten Bibliotheken sind strikt read-only; der hierarchische Teilbestand entsteht nur in einem frischen Verzeichnis.

**Konsequenzen:** Die Parent-Child-Default-Schaltung und der gebündelte Reindex (Duplikate, i18n-Präfixe, dt. EPUB-Sektionen) sind bis zur Messung blockiert. Das Benchmark wird vor dem Community-Release veröffentlicht und ist das zentrale Launch-Asset („beweisbar exzellentes Retrieval").

**Verworfen:** LongMemEval und andere öffentliche Benchmarks (falscher Problemraum: Konversations-Memory statt Bibliotheks-Retrieval); LLM-as-Judge in v1 (Kosten, Reproduzierbarkeit, Hardware); Voll-Reindex des Bestands vor der Teilbestand-Evidenz (teuerste Operation des Systems auf Verdacht); ein „publikationsreifes" akademisches Benchmark als Erstziel (Scope-Falle — erst Messinstrument, dann Politur); eine eigens gebaute „generisches RAG"-Vergleichs-Baseline in v1 (als Kommunikations-Asset attraktiv, aber ein zweites Retrieval-System zu bauen ist exakt die Scope-Falle — vertagt auf nach v1; bis dahin trägt der interne A/B-Vergleich flach vs. hierarchisch die Beweislast). Als Embedding-Kandidaten verworfen: nomic-embed-text (leichter, aber messbar unter BGE-M3-Qualität — der falsche Tausch für ein Präzisions-Produkt; als Empfehlung für CPU-only-*Nutzer* dokumentierbar, nicht als Identität) und Modell2Vec/Potion-Modelle (statische Embeddings, extrem schnell, aber die Qualitätsklasse passt nicht zum Kernversprechen).

**Umsetzungsstand (August 2026): nicht begonnen.** Es existiert weder `benchmarks/` noch ein Runner. Nicht zu verwechseln mit dem Evaluations-Harness in `archilles-scriptor` (`eval/`, `src/scriptor/eval/`): Der misst die *Aufbereitung* (Seitenlabels, Anker, Regionen, Zitate gegen handausgezeichnete Ground Truth je Band), dieser hier misst die *Abfrage*. Berührungspunkt ist allein die Citation Integrity aus Komponente 2 — dort ist Scriptor der Eigentümer der Wahrheit (siehe `WATCHDOG_AND_WIKI.md` §II.5/§II.6), und Archilles sollte sie konsumieren statt herleiten.

---

### ADR-032: Die Naht Scriptor → Archilles — Zulieferer mit reichem Kontrakt, Abhängigkeit in einer Richtung (September 2026)

**Kontext:** `archilles-scriptor` bereitet gedruckte Bücher aus PDF zu einem *Prepared Document* auf (`PREPARED_FORMAT_SPEC.md`, v0.3.0). Das ist ein Markdown-Master mit Seitenmarkern im gedruckten Label (`[p. 88]`), Regionsmarkern (`[region: bibliography]`) und gebundenen Fußnoten, dazu Sidecars; `<master>.pagination.json` trägt je Seite die physische Position und die Herkunft des Labels. Am 10. August 2026 hat Archilles selbst Regionen und ein Chunking-Profil angefordert (Anlass: Register, Anmerkungsapparate und Titelei in inhaltlichen Treffern). Scriptor hat am selben Tag geliefert. Einen Monat später las Archilles keine Zeile davon.

Der Befund vom 9. September 2026 (`archilles-scriptor/docs/internal/NAHT_SCRIPTOR_ARCHILLES_2026-09.md`) fand drei strukturelle Gründe, keinen Aufschub:
1. Die Anforderung zeigte auf `pipeline._select_chunker` im stillgelegten Modularpfad (ADR-004, Nachtrag). Der Produktionspfad las ein Scriptor-Markdown als TXT und warf das Frontmatter ungelesen weg; Seiten- und Regionsmarker gingen als gewöhnliche Zeichen ins Embedding.
2. Archilles hatte keinen Eingang, durch den ein aufbereiteter Band zu seinem Calibre-Buch fände (`.md` an sechster Stelle der Formatliste, Sidecars sind keine Calibre-Formate). Möglich war nur eine Dublette im Lab-Vault mit eigener ID.
3. Der Konsument für die Herkunft eines Seitenlabels existierte, war aber tot: `_resolve_page_info` las `printed_page`/`printed_page_confidence`, die niemand schrieb.

Die Dokumentation (`AGENTS.md`, `ARCHITECTURE.md`) beschrieb zudem den stillgelegten Pfad als den laufenden. Die Anforderung war aus ihr geschrieben.

**Entscheidung: Modell (a+).** Scriptor bleibt ein eigenständiges Repository mit eigener CLI und eigenem Vertrag; es hat drei Abnehmer (Retrieval, Übersetzung im Archillator, Markdown-Konversion), Archilles ist einer davon. Archilles liest das Bündel über einen `ScriptorExtractor` in der Extraktor-Schicht (`src/extractors/scriptor_extractor.py`) und nimmt `archilles-scriptor` als **harte Laufzeitabhängigkeit in einer Richtung** auf. Es importiert den Leser des Formats (`scriptor.document`), die Markergrammatik und das Regionsvokabular, statt sie zu kopieren. Scriptor importiert nichts aus Archilles. Die Formatwahl bleibt je Buch; einen Modus-Schalter „einfach/hochwertig" gibt es nicht.

**Kriterien, an denen die Entscheidung zu prüfen ist:**
- K1: Der Vertrag bleibt für den Archillator unverändert.
- K2: Scriptor ist ohne Archilles installier- und nutzbar.
- K3: Jede geteilte Funktion liegt in genau einem Repo, das andere importiert sie.
- K4: Jede Indexzeile sagt, wer sie erzeugt hat und nach welcher Spec-Version.
- K5: Ein Buch lässt sich auf den alten Pfad zurücksetzen, ohne andere Bücher anzufassen.
- K6: Die CPU-Kosten je Buch sind gemessen, die GPU-Kosten unverändert.
- K7: Keine Archilles-spezifische Syntax ist ins Markdown gewandert.

**Umsetzung (Schritte S1–S6, 9.–11. September 2026):**
- **Das Bündel ist Textquelle, kein Format.** Es liegt unter `<Bibliothek>/.archilles/scriptor/<key>/` (numerische Buch-IDs fünfstellig nullgefüllt, damit ein Verzeichnislisting in Bibliotheksordnung läuft). Es enthält genau einen Master, nicht `*.review.md`; zwei Kandidaten werden verweigert, nicht gewählt. `Indexer._text_source` tauscht in `index_book` und `prepare_book` nur die Textquelle (`d7cf012`). Die Buchdatei bleibt die Identität: Über sie findet Archilles die Calibre-Metadaten (Buchordner) und die Viewer-Annotationen (SHA256 des Dateipfads), auf sie verweisen Export-Link und `source_file` (`7f859c6`). Der Befund hatte das Bündel als weiteres Format in der Qualitätswahl (ADR-014) vorgesehen; das hätte dem Buch Autor, Titel, Tags und Annotationen genommen. Dieselbe Regel gilt für Zotero (`67f8bfd`).
- **Drei Spalten:** `region`, `label_source`, `producer_version`, Default `''` (`11cf1db`). Gespeichert wird das Signal neben dem Ergebnis, die Lehre aus der Anforderung vom August: Ein Reparaturversuch an einem Index, der nur `section_type` kannte, traf 50.670 statt 3.345 Chunks. Mit `region` ist eine geänderte Abbildung ein Metadatenlauf, kein Reindex. `producer_version` ist wie `pipeline_version` gegen `update_metadata_fields` gesperrt.
- **Die physische Seite reist über den Sidecar** (`pages[].pos`), nicht über eine neue Markerform. Grenze dieser Entscheidung: Eine Seite ohne gedrucktes Label hinterlässt im Text keinen Marker, ihr Text steht unter der Adresse der Vorseite. Die Antwort darauf ist eine Zulassungsbedingung (siehe unten, `inherited`), keine neue Syntax.
- **Region → `section_type`:**

  | Region | `section_type` |
  |---|---|
  | `front-matter`, `contents` | `front_matter` |
  | `preface` | `main_content` (Entscheidung vom 10.9.2026: Vorworte werden suchbar, Danksagungen darunter hingenommen) |
  | `main`, unmarkiert | `main_content` |
  | `bibliography`, `index`, `abbreviations`, `notes`, `appendix` | `back_matter` |
  | unbekannter Name | `main_content`, der Name verbatim in `region` |

- **Der Ausgang liest `label_source`** (`a7cfcb8`, `a54a1e5`). Keine Warnung bei `printed`, `link`, `toc`, `catalogue` und bei Zeilen ohne Scriptor. Bei `computed` und unbekannten Werten erscheint eine Warnung, im Prompt steht dann `Page: 88 (inferred)`, und der System-Prompt verpflichtet das Abnehmer-LLM, den Umstand weiterzugeben. Beide MCP-Server liefern Seite und `label_source` über eine gemeinsame Funktion. Der tote `printed_page`-Zweig ist entfernt.
- **Bündel entstehen durch `scripts/scriptor_prepare.py`, nicht im Watchdog** (`914a233`). Vier Zulassungsbedingungen je Band:
  1. Der Text jeder PDF-Seite steht im Master: mindestens 98 % der Seiten, gemessen seitenweise über Acht-Wort-Fenster ohne Ziffern; der Vorspann, den Scriptor neu setzt, ist ausgenommen (`c691801`).
  2. Mindestens so viele Fußnotendefinitionen wie sichere Fußnoten im Audit.
  3. Mindestens 20 % bezeugte Seitenlabels.
  4. Weniger als 5 % Text unter geerbter Seitenzahl (`profile.inherited`).

  Gebaut wird in `_work/`; an seinen Platz kommt das Bündel erst nach bestandener Prüfung, ein abgelehnter Lauf landet in `_rejected/<key>/` und lässt Bündel und Index unangetastet. Der Bearbeitungsstand (vorbereitet / bearbeitet / freigegeben zur Indexierung) wird aus dem Hash des Masters abgeleitet, nicht gesetzt. Ein späterer Lauf holt den Index nach, wenn der Master ihn noch nicht erreicht hat.
- **Abnahme (Goldene Kette):** Bauer [10593]: aufbereitet in 44 s, Deckung 100 %, 99 % bezeugt. Nach `index_book` zitiert die Suche nach einer Passage von S. 88 `S. 88` mit `label_source='printed'`. Titel, Autor, Tags, `calibre_id` und die fünf PDF-Annotationen blieben unverändert.

**Versionsregel:** Der Extraktor versteht `SUPPORTED_SPEC_MAJOR = 0`. Minor-Versionen sind additiv und werden mit den Toleranzregeln der Spec gelesen: unbekannte Region → Fließtext, unbekannter `source`-Wert → behauptet, unbekanntes Frontmatter-Feld → ignoriert. Eine neue Major-Version wird mit Meldung **verweigert**, nie still indexiert. `format_version` steht je Zeile in `producer_version`; mit `format='scriptor'` und `pipeline_version` ist jede Population des Index lokalisierbar (K4). Ändert der Extraktor seine Chunk-Komposition, steigt `PIPELINE_VERSION`. Ein neu gerendertes Bündel ist neuer Text und löst die Neuindexierung dieses Buchs aus; zuständig ist das Skript, nicht der Watchdog.

**Konsequenzen:**
- Archilles' eigener PDF-Pfad bleibt als **eingefrorener Rückfall** für Bücher ohne Bündel. Er wird nicht weiter ausgebaut und ist über `format` von den Bündelzeilen unterscheidbar. Er verschwindet erst, wenn Scriptor mit OCR den Rest trägt.
- Die Abhängigkeit koppelt die Releases: Ein Major-Sprung der Spec ist ein Ereignis für die ganze Familie (Spec §11). Ohne installiertes `archilles-scriptor` startet die Extraktion nicht; es gibt bewusst keinen Fallback-Code, denn Fallbacks wären Kopien.
- Ein Bündel ist dauerhaft. Es wird bei jeder Neuindexierung gelesen, und der Master ist das, was der Nutzer von Hand nacharbeitet. Er ist Quelle, nicht Ableitung; die Sicherungsregel nimmt `.archilles/scriptor` deshalb aus der sonst reproduzierbaren Zone `.archilles` aus.
- Der Parserpfad des Modularbaus hat keinen Kunden mehr (S11).

**Verworfen:**
- (b) ein `ScriptorParser` in der `ParserRegistry`: der stillgelegte Pfad, und eine statische Rangfolge statt der Messung je Buch.
- (c) die Verschmelzung: Archilles ingestiert über zwanzig Formate, Annotationen und Calibre-Konversionen; Spec und Benchmark sollen ein eigenständiges Asset bleiben.
- Eine neue Markerform für die physische Seite (`[p. ~7]`).
- Spalten für `label_confidence`, `attested` oder Regionskonfidenz, weil ihnen ein Konsument fehlt.
- Die Wortbilanz gegen den PDF-Pfad als Zulassungsbedingung: Scriptor entfernt Kolumnentitel und Fußzeilen absichtlich, jeder Band verliert dadurch 4–5 % Wörter, auch der fehlerfreie.

**Offen:**
- **Die Gliederung.** Durch die Naht gehen nur zwei flache Felder (`chapter`, `section_title`), und der EPUB-Pfad füllt sie nicht besser als der PDF-Pfad. Das gemeinsame Regionsvokabular für den EPUB-Rückfall (S7) geht deshalb in einem Gliederungsmodell auf, das seit dem 11. September 2026 entworfen wird (Messung `docs/internal/naht-2026-09/G1_ERGEBNIS_2026-09-11.md`).
- **OCR:** Das Tesseract-Backend auf Scriptors Seitenmodell (S8), danach Scans zu Scriptor (S9).
- **Seitenbereich je Chunk:** Ein Chunk zitiert die Seite seines ersten Zeichens; ob der Ausgang einen Bereich zeigen soll, entscheidet der Nutzer.
- **Spec-Sätze:** die Regel „Überschrift vor Marker" und `pages[].pos` als Kanal der physischen Seite gehen mit der nächsten Spec-Version.

---

## III. Produktstrategie und Geschäftsmodell

### Zielgruppe: Individuelle Forscher, keine Institutionen

**Entscheidung:** ARCHILLES adressiert primär einzelne Wissenschaftler mit persönlichen Calibre-Bibliotheken, nicht institutionelle Kunden.

**Begründung:** Die Primary Targets sind technisch versierte Akademiker aus den Geisteswissenschaften – Geschichte, Literatur, Philosophie –, die große, kuratierte Bibliotheken pflegen und Wert auf Privacy und lokale Datenkontrolle legen. Institutionelle Kunden (Universitätsbibliotheken, Forschungsinstitute) erfordern Compliance-Prozesse, Ausschreibungen und Support-Strukturen, die für ein Solo-Projekt in der Aufbauphase nicht leistbar sind. Die Tür wird offengehalten (Scoped Knowledge Bases als Feature-Option, institutionelle Lizenzen in der Roadmap), aber der Fokus bleibt auf dem individuellen Nutzer.

### Freemium mit Special Editions

**Entscheidung:** Freemium-Modell mit großzügigem Free Tier und kostenpflichtigen disziplinspezifischen Erweiterungen.

**Begründung:** Die Marktanalyse zeigt, dass DEVONthink (499 €) und Polar (299 $) als Einmalkauf-Modelle erfolgreich bei Wissenschaftlern sind. Abo-Müdigkeit ist in der Zielgruppe verbreitet. Das Free Tier bietet die komplette Basisfunktionalität ohne Bibliotheksbeschränkung, um eine Nutzerbasis aufzubauen. Die Premium-Erweiterungen sind inhaltlich differenziert:

Die **Historical Edition** als erste geplante Special Edition bringt LightRAG für Graph-basiertes Retrieval, Zeitreferenz-Extraktion, chronologische Visualisierung und spezialisierte Embeddings für historische Texte. Weitere geplante Editions sind Literary, Legal und Musical, jeweils mit disziplinspezifischen Optimierungen. Die modulare Pipeline-Architektur (ADR-004) ist die technische Voraussetzung für diese Trennung.

### ARCHILLATOR als Lead-Magnet

**Kontext (Januar 2026):** Parallel zu ARCHILLES entstand ARCHILLATOR, ein akademisches Übersetzungstool, das Bücher absatzweise über verschiedene LLM-Provider (Gemini, OpenAI, Claude) übersetzt und dabei EPUB-Formatierung erhält.

**Entscheidung:** ARCHILLATOR wird als eigenständiges, kostenloses Tool veröffentlicht, bevor ARCHILLES selbst bereit für den Community-Release ist.

**Begründung:** Das Tool löst ein konkretes, weit verbreitetes Problem (Bücher in Fremdsprachen schnell lesbar machen) und demonstriert die technische Kompetenz hinter dem ARCHILLES-Projekt. Es dient als Lead-Magnet für die Community-Phase: Nutzer, die den ARCHILLATOR schätzen, werden auf ARCHILLES aufmerksam. Das Tool unterstützt Checkpoint-basiertes Resume (Übersetzung kann unterbrochen und fortgesetzt werden, auch mit Provider-Wechsel), was die Robustheit für lange Dokumente sicherstellt.

**Abgrenzung:** ARCHILLATOR ist kein ARCHILLES-Feature, sondern ein separates Tool. Es nutzt keine RAG-Infrastruktur und teilt keinen Code mit ARCHILLES. Die Verbindung ist rein strategisch.

### Privacy als politisch neutrale Positionierung: Datensouveränität

**Entscheidung:** "Datensouveränität" als zentraler Wert, nicht als technisches Feature.

**Begründung:** Die Analyse der politischen Dimension ergab, dass Datenschutz als Wert überparteilich anschlussfähig ist: Linke sehen Überwachungskritik, Konservative Misstrauen gegenüber Tech-Monopolen, Liberale individuelle Autonomie. Die Positionierung als "Privacy by Design" (nicht durch nachträgliche Compliance) spricht die gesamte Zielgruppe an. ARCHILLES verarbeitet keine Nutzerdaten, betreibt keine Telemetrie und kommuniziert nicht mit externen Servern, sofern der Nutzer dies nicht explizit wählt. Der Nutzer ist sein eigener Datenverarbeiter – das vereinfacht die DSGVO-Compliance auf das Triviale.

### MCP-Native als strategische Wette

**Entscheidung:** Vollständige Implementierung als MCP-Server (Model Context Protocol) statt als standalone Anwendung mit eigener GUI.

**Begründung:** MCP wurde im November 2025 als der wichtigste Differenzierungsvorteil für 2025/26 identifiziert. Das Protokoll löst elegant das Kerndilemma der Zielgruppe: Sie wollen die besten Cloud-Modelle (Claude, GPT-4o) nutzen, aber ihre sensiblen Daten nicht hochladen. Ein lokaler MCP-Server exponiert die Bibliothek dynamisch für kompatible KI-Agenten, ohne dass ein Byte den Rechner verlässt. Ressourcen werden mit URIs referenziert, die automatisch in akademische Zitationsformate (BibTeX, APA, Chicago) umgewandelt werden können.

Das Risiko: MCP ist ein junger Standard, und seine Durchsetzung hängt von Anthropics und OpenAIs fortgesetzter Unterstützung ab. Die Wette ist, dass MCP zum Industriestandard für LLM-Tool-Integration wird – eine Wette, die durch die rasche Adoption (OpenAI im März 2025, wachsendes Ökosystem mit 80+ offiziellen Servern) gestützt wird.

**Ergänzende Interfaces:** Neben dem MCP-Server existieren ein Web-UI (Streamlit, für Nutzer ohne Claude Desktop) und ein CLI (für Batch-Operationen und Debugging). Beide sind bewusst als Companion-Tools positioniert, nicht als primäre Interfaces. Seit der Service-Layer-Refaktorierung (ADR-009) nutzen alle drei Clients denselben Code-Pfad.

---

## IV. Bewusst aufgeschobene Entscheidungen

### MCPB Desktop Extension: Erst nach stabilem MVP

**Kontext (Dezember 2025):** Anthropics neues Desktop Extension Format (.mcpb) verspricht Ein-Klick-Installation für Claude Desktop. Für ARCHILLES wäre das ein potenzieller Game-Changer, weil es die Einstiegshürde für nicht-technische Nutzer drastisch senken würde.

**Technische Analyse:** ARCHILLES' Python-Stack mit kompilierten Abhängigkeiten (LanceDB, PyTorch/Sentence-Transformers) lässt sich nicht portabel in ein .mcpb bündeln. Die realistische Lösung wäre ein Thin-Client-Ansatz: ein leichtgewichtiger Node.js MCP-Server als .mcpb, der mit einem separat installierten Python-Backend kommuniziert.

**Entscheidung:** Aufschub bis nach MVP-Fertigstellung und Beta-Test.

**Begründung:** Die Zielgruppe der Beta-Phase (technisch versierte Akademiker, Calibre-Power-User) kann manuelle JSON-Konfiguration handhaben. Eine vorzeitige Architekturspaltung in Node.js-Frontend und Python-Backend würde die Feature-Entwicklung bremsen, weil jede Änderung in zwei Codebases synchronisiert werden müsste. Die Einstiegshürde senken wir erst, wenn es etwas Stabiles gibt, in das man einsteigen kann.

### LightRAG / Graph RAG: Evaluation vor Implementation

**Kontext (Dezember 2025):** Für die Historical Special Edition wurde zunächst Neo4j als Graph-Datenbank erwogen, dann LightRAG als leichtere Alternative identifiziert.

**Entscheidung:** LightRAG wird als Graph-RAG-Ansatz vorgesehen, aber erst nach systematischer Evaluation (geplant Q2 2026) implementiert.

**Begründung:** LightRAG bietet Dual-Level Retrieval (Low-Level für Details, High-Level für Konzepte) und inkrementelle Updates ohne komplettes Graph-Rebuilding. Allerdings erfordert die Graph-Extraktion LLM-Aufrufe während der Indexierung, was API-Kosten verursacht. Vor der Implementation muss ein Testkorpus definiert und Metriken für den Vergleich mit reinem Vektor-RAG festgelegt werden. Bestehende Wissensgraphen (Wikidata, Wikipedia) könnten als Seed-Quellen dienen, statt vom Nutzer manuelle Entitätspflege zu verlangen.

### Uncertainty Quantification: Forschungsziel, keine aktive Planung

**Kontext:** Die Fähigkeit, widersprüchliche Aussagen in verschiedenen Quellen zu erkennen und transparent zu machen, passt ideal zur Projektphilosophie einer "eigenen, in Teilen unkonventionellen Geschichtsinterpretation", bei der das Nebeneinander verschiedener Deutungen produktiv sein soll.

**Entscheidung:** Als langfristiges Forschungsziel (2027+) dokumentiert.

**Begründung:** Technisch ambitioniert (erfordert Natural Language Inference, Entitätsabgleich über Quellen hinweg), möglicherweise als Kooperation mit akademischen Partnern (NFDI-Konsortien) realisierbar. Für den MVP und die erste Produktversion irrelevant.

### Kollaboration: Minimal, aber vorbereitet

**Kontext (Januar 2026):** Die Analyse kollaborativer Workflows in den Geisteswissenschaften ergab, dass sich Humanities-Kooperation fundamental von STEM unterscheidet: Einzelautorschaft dominiert, aber Betreuer-Studierende-Beziehungen und geteilte Literatursammlungen sind zentrale Kollaborationsmuster.

**Entscheidung:** Keine Echtzeit-Kollaborationsfeatures. Stattdessen minimale, aber nützliche Export- und Austauschfunktionen.

**Begründung:** Geisteswissenschaftliche Teams teilen Referenzen, Annotationen und kuratierte Sammlungen – sie brauchen keine Google-Docs-artige Echtzeitbearbeitung. Exportierbare Annotationssets und thematische Sammlungen als geteilte Bibliographien decken den realen Bedarf ab, ohne die Architektur zu verkomplizieren.

### Chunking-Intelligenz: Small-to-Big und Parent-Child

**Kontext (November 2025 – Februar 2026):** Die parallel über Gemini, Grok und ChatGPT durchgeführte Chunking-Intelligence-Analyse identifizierte hierarchisches Chunking als den größten einzelnen Qualitätshebel für RAG-Systeme. Die aktuelle Konfiguration (RecursiveCharacterTextSplitter mit 1000 Token / 200 Overlap) liefert solide Ergebnisse, verschenkt aber Potenzial bei langen argumentativen Passagen.

**Entscheidung:** Small-to-Big Retrieval und Parent-Child-Hierarchien werden implementiert, Semantic-Hybrid-Chunking mit dynamischen Thresholds wird evaluiert.

**Begründung:** Die Grundidee: Indexiere kleine Chunks (Absatzebene) für hohe Retrieval-Präzision, aber liefere dem LLM den größeren Kontext (Kapitel oder erweiterte Passage). Das löst das Kernproblem, das Geisteswissenschaftler an RAG-Systemen frustriert: Sätze, die mitten im Argument abreißen. Die Chunking-Intelligence-Analyse ergab, dass selbst einfaches Recursive Hierarchical Chunking bereits 80% des Qualitätsgewinns gegenüber flachem Chunking bringt, während Semantic-Hybrid-Varianten mit Agglomerative Clustering weitere 20-30% liefern, aber signifikant mehr Implementierungsaufwand erfordern. Die Reihenfolge ist daher: erst Parent-Child über bestehende Recursive-Struktur, dann optional Semantic-Hybrid als Upgrade-Pfad.

**Implementierungsstand (Juni 2026): end-to-end verdrahtet, noch nicht produktiv aktiviert.** Die Mechanik existiert vollständig und durchgängig: `_group_chunks_hierarchically()` (`src/extractors/base.py`) bildet die zweistufige Parent/Child-Hierarchie aus den bereits strukturbewusst extrahierten Chunks (Children = die Extractor-Chunks selbst, ~512 Token; aufeinanderfolgende Children einer Sektion zu Parents mit ~2048-Token-Budget gruppiert) mit `parent_id`-Verlinkung und `window_text`; `ChunkType.PARENT`/`CHILD` sind als Konstanten definiert (`src/archilles/constants.py`, beide in `HIERARCHICAL_TYPES`). Das Flag `--hierarchical` ist in `rag_demo.py` und `batch_index.py` vorhanden und wird über `ArchillesService` → `ArchillesRAG.hierarchical` → `Indexer._apply_hierarchical_chunking()` durchgereicht; im Retrieval lädt der `PromptBuilder` (`prompting.py`) bei vorhandenem `parent_id` den Parent-Chunk als Kontext, und `LanceDBStore` bezieht `child`-Chunks in die Suche ein.

**Was noch fehlt** (Stand Code-Review 16. Juni 2026, P4-Fazit): Das Flag ist per Default `False` — hierarchisches Chunking ist nicht standardaktiv. Der modulare `parser → chunker → embedder`-Pfad wurde in P4 nur *verdrahtet*, nicht hierarchisch gechunkt; das volle Small-to-Big/`window_text` auf diesem Pfad steht aus. Vor allem ist der Bestand nicht hierarchisch reindexiert und die Retrieval-Qualität nicht auf dem Korpus validiert. Der „Parent-Child-Refresh" ist im Code-Review-Gesamtfazit als **zentraler Knoten eines koordinierten Reindex** benannt, der mit drei weiteren anstehenden Reindex-Anlässen gebündelt werden sollte: Duplikat-Bereinigung (~27.479 Duplikat-IDs in 2.112 Büchern), i18n-Index-Präfix-Reindex (Befund 1.34/3.20) und die deutsche EPUB-Sektionserkennung. Diese Bündelung ist Voraussetzung, bevor Parent-Child als Default geschaltet wird.

**Validierung und Metadaten-Fix (17. Juni 2026, ADR-027):** Eine gezielte Validierung des hierarchischen Pfads gegen Echtdaten (GPU-frei) deckte einen Regress auf: die ursprüngliche Mechanik chunkte `extracted.full_text` mit minimaler `ChunkMetadata` neu und verwarf dabei die strukturbewussten Extractor-Chunks. Die `child`-Chunks trugen daher **kein** `section_type`/`page_label`/`chapter`/`section_title` (0 % Abdeckung im Test) — sie waren suchbar, aber nicht zitierfähig; der USP wäre bei Default-Schaltung verloren gegangen. Zusätzlich drifteten die char-Offsets auf realem Whitespace-Text (gestrippte Absatzlängen vs. ungestrippter `full_text`). **Entscheidung (Option A):** Die Hierarchie wird aus den bereits strukturbewusst extrahierten Chunks gebaut (`_group_chunks_hierarchically()`), sodass Children Metadaten **und** korrekte Offsets erben; der frühere `full_text`-Re-Chunking-Pfad (`_create_hierarchical_chunks()`) wurde entfernt. Embedding-freier Regressionstest: `tests/test_hierarchical_chunking.py` (16 Fälle, Suite 643 → 659). Aus der Validierung offen geblieben: (1) bei gefülltem `window_text` greift im `PromptBuilder` ausnahmslos der window_text-Pfad — der `parent_id`-Lookup ist toter Pfad, und Parent-Chunks werden zwar embeddet, aber im Default-Retrieval weder gesucht noch als Kontext genutzt; ob die Parent-Ebene ihre ~25–30 % Mehr-Vektoren rechtfertigt, ist vor der Default-Schaltung zu klären. (2) Die VRAM-Messung am 4-GB-Gerät steht aus (entscheidet lokal vs. remote für den Reindex).

---

## V. Branding und Kommunikation

### Rebranding: Achilles → ARCHILLES

**Zeitpunkt:** November 2025

**Begründung:** Der Name "ARCHILLES" verbindet die Archiv-Assoziation ("ARCH") mit dem mythologischen Helden. Subtile Schichten: "ARCH" referenziert sowohl "archive" als auch "research" aus dem Tagline; "ILLES" erscheint rückwärts gelesen in "intELLIgent"; "A" und "I" zusammen ergeben "AI". Der Tagline "Your Intelligent Research Archive" liefert alle Bestandteile. Domains archilles.de, archilles.net und archilles.org wurden gesichert.

### Tone of Voice: Intellektuell, aber nicht elitär

Das Kommunikationsprinzip "While others build snake games, we enable serious scholarship" positioniert ARCHILLES als Werkzeug für ernsthafte Wissensarbeit, ohne Gatekeeping zu betreiben. Die Formulierung respektiert sowohl die KI-Technologie als auch die akademische Arbeit der Nutzer. Der Kern-Claim lautet: "Other AI tools question books. ARCHILLES questions your library."

### Vendor-Neutralität in der Kommunikation

In der externen Kommunikation wird von "frontier models" gesprochen, nicht ausschließlich von "Claude" – obwohl die MCP-Integration aktuell primär auf Claude Desktop zielt. Das verhindert Vendor-Lock-in in der Wahrnehmung und hält die Tür offen für andere MCP-kompatible Clients.

---

## VI. Rechtliche Rahmenbedingungen

### EU AI Act: Wahrscheinlich nicht anwendbar

**Analyse (November 2025):** ARCHILLES ist ein lokales Tool für persönlichen Gebrauch. Es klassifiziert keine Personen, trifft keine automatisierten Entscheidungen und verarbeitet keine biometrischen Daten. Die Risikoklassifizierung des EU AI Act trifft auf ein lokales Retrieval-Tool nicht zu. Monitoring bleibt dennoch sinnvoll, weil Regulierung sich weiterentwickelt.

### Urheberrecht und Text & Data Mining

§ 60d UrhG (Deutschland) und die DSM-Richtlinie (EU) erlauben Text & Data Mining für Forschungszwecke. ARCHILLES ist ein Tool, kein Content-Provider – vergleichbar mit Calibre selbst oder VLC Media Player. Die Verantwortung für die Rechtmäßigkeit der indexierten Bibliothek liegt beim Nutzer. DRM-geschützte E-Books sind explizit Nutzerverantwortung.

### Lizenzierung: MIT, mit Optionen

Die Basisversion wird unter MIT-Lizenz veröffentlicht (maximal permissiv für Adoption). Spätere Versionen oder Special Editions können restriktivere Lizenzen nutzen, falls nötig. Dual Licensing (Open Source + Commercial) bleibt als Option für die Editions-Strategie vorbehalten.

---

## VII. Zusammenfassung: Leitprinzipien

Die Entscheidungen folgen konsistent einigen Grundprinzipien, die das Projekt prägen:

**Infrastruktur, nicht Anwendung (ADR-022).** ARCHILLES ist der semantische Layer zwischen Bibliotheken und LLMs — nicht ein Second Brain, nicht ein Chat-Memory, nicht ein Schreibwerkzeug. Die Adapter-Architektur ist das Produkt. Die MCP-Tools sind die API. Second-Brain-Systeme sind Kunden, nicht Konkurrenten.

**Privacy ist kein Feature, sondern die Architektur.** Daten bleiben lokal, Datensouveränität ist das Fundament, nicht ein Checkbox-Item.

**Modulare Erweiterbarkeit vor Featurefülle.** Die auf ein Registry-Pattern hin angelegte Architektur, die Plugin-fähigen Schnittstellen und die definierten Erweiterungszonen (`.archilles`-Ordner) sind wichtiger als jedes einzelne Feature.

**Akademischer Anspruch als Differenzierung.** Exakte Zitationen, transparentes Retrieval und disziplinspezifische Optimierungen unterscheiden ARCHILLES von generischen RAG-Lösungen – nicht die Menge der Features.

**Aufschub als bewusste Strategie.** MCPB, LightRAG, Uncertainty Quantification und institutionelle Features werden nicht vergessen, sondern zum richtigen Zeitpunkt implementiert. Ein funktionierendes MVP hat Vorrang vor einer vorzeitig aufgeblähten Architektur.

**Weniger Code, mehr Architektur.** Wo eine architektonische Lösung (wie Section-Filtering auf DB-Ebene) bessere Ergebnisse liefert als eine code-intensive Heuristik, wird die Architektur gewählt – selbst wenn das einen größeren initialen Umbau bedeutet.

---

*Nächste geplante Aktualisierungen:*
- *ADR für Cross-Encoder-Reranking (nach Benchmark gegen aktuelle Hybrid-Search)*
- *ADR für Übersetzungs-Pipeline (NLLB lokal / MADLAD-400 API)*
- *Ergebnis der LightRAG-Evaluation (geplant Q2 2026)*
- *Entscheidung über MCPB-Implementation (nach Beta-Feedback)*
- *Aktualisierung der Wettbewerbsanalyse (Calibre 8.x Weiterentwicklung, MCP-Ökosystem, Second-Brain-Landschaft)*
- *ChromaDB-Dependency bereinigen: annotations_indexer.py nicht mehr aktiv befüllt; Entscheidung über vollständige Entfernung oder Legacy-Fallback.*
- *Annotation-Suche im MCP-Server: search_annotations-Tool auf LanceDB umstellen (aktuell noch ChromaDB-Backend).*
- *Schema-Migrations-Framework: Der aktuelle add_columns()-Mechanismus funktioniert, ist aber ad-hoc. Bei wachsender Feldanzahl lohnt sich ein formales Migrations-System mit Versionsnummern.*
- *Parent-Child-Chunking: Entscheidung über Implementierungsreihenfolge nach Two-Phase-Pipeline-Stabilisierung.*
- *Docling-Evaluation: Ergebnis und ADR für/gegen Markdown-Extraktion als Pipeline-Stufe.*
- *CLI-Erfahrung verbessern: rag_demo.py liefert unbefriedigende Ergebnisse ohne Claude-Kontext-Interpretation. Ansätze: bessere Prompt-Templates, automatische Query-Expansion, lokales LLM als Interpretation-Layer.*
- *Lab-Schreib-Tools (add_note, link_insight): Deprioritisiert per ADR-022; Wiedervorlage nach Community-Feedback.*
