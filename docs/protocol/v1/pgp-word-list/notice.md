# REM protocol v1 PGP Word List data notice

## License boundary

The repository code remains MIT licensed. Only `even.txt` and `odd.txt` in this directory, and their byte-identical packaged resource copies, are distributed under the GNU Free Documentation License (GFDL), version 1.2. `gfdl-1.2.txt` contains the full applicable license. This notice records provenance and does not change the license of the repository's code, tests, schemas, vectors, or other documentation.

The source notice does not identify a GFDL version, Invariant Sections, Front-Cover Texts, or Back-Cover Texts. GFDL section 10 permits selecting any published version when no version is specified; this redistribution selects version 1.2. The two extracted lists are transparent ASCII copies with no cover texts or invariant sections.

## Title, authorship, and attribution

Title of this extracted document: **REM protocol v1 PGP Word List data**.

The archived source states that the incorporated material is copyrighted by **PGP Corporation** and licensed under the **GNU Free Documentation License**, per **Jon Callas**, CTO and CSO of PGP Corporation, 4 January 2007. It credits **Patrick Juola** and **Philip Zimmermann** with designing the list in 1995, records later refinement contributions by Jon Callas, and credits Zhahai Stewart with suggesting alternating lists. The Remote Energy Meter maintainers performed and published only the mechanical extraction described below; they claim no additional copyright in the extracted words.

## Normative source and retrieval record

- RFC lineage: RFC 6189 section 5.1.6, reference `[pgpwordlist]`, identifies Wikipedia revision `oldid=400752943`.
- Immutable archived capture: <https://web.archive.org/web/20200228011544id_/https://en.wikipedia.org/w/index.php?title=PGP_word_list&oldid=400752943>
- Archive timestamp: `2020-02-28T01:15:44Z` (`20200228011544`).
- Retrieval time: `2026-11-10T18:48:48Z`.
- Source response media type: `text/html; charset=UTF-8`.
- Fetched source byte length: `52808`.
- SHA-256 of fetched source response bytes: `cdf0421415db5b3bd428004c237371543f7c70964fa82741ee24f1c8ad69fa71`.

The immutable archived capture is the redistribution source. PGPfone and the thblt implementations below are independent comparison inputs only; none of their source code or license basis was copied into these files.

## Extraction and output integrity

The fetched source bytes were decoded as UTF-8. The extractor selected table rows whose first cell was an uppercase two-digit hexadecimal value and whose next two cells each contained one ASCII alphabetic word. It required exactly 256 rows with labels `00` through `FF` in sequence. It wrote the second column to `even.txt` and the third column to `odd.txt`, preserving spelling, case, and order, joining entries with LF (`0x0A`) and adding exactly one final LF. It performed no Unicode, locale, or case normalization.

Each result was then checked for exactly 256 unique, non-empty entries, ASCII-only bytes, no carriage returns, no leading or trailing whitespace, and one final newline.

- `even.txt`: 2,024 bytes; SHA-256 `37a65f88512467edd12a1ab3eeb5f4328230e86711a8efde6333361ce10f4fcf`.
- `odd.txt`: 2,407 bytes; SHA-256 `c2f23c2233d4d7291107e8f796c374d0cb1fb30c1391bcb4f6480243de8ebf5a`.

## Independent cross-checks

The extracted 512 indexed values were compared byte-for-byte, after parsing only each implementation's list literals, with these pinned public repositories:

1. PGPfone public source archive, `ysangkok/pgpfone`, commit `ba8cfdd06ca2680fc46881a16e0149ff3a669f05`, `src/common/HashWordList.cpp`: all 512 values match. The repository has no detected SPDX license. Its signed `README-src.txt` says the source remains copyrighted by Network Associates and cannot be used without permission. It was therefore used only as a comparison source, never as the redistribution source or license basis.
2. thblt Python implementation, `thblt/pgp-words`, commit `0c95b82de3cfda2e189cf334e948c701f61fb752`, GPL-3.0-or-later in the file header and repository license: 511 of 512 values match. Odd index `0xFF` is UTF-8 `Yucatán`, while the normative archived source is ASCII `Yucatan`.
3. thblt Rust implementation, `thblt/pgp-words.rs`, commit `69ed843a4a11820fb6647f06228ca982e5e1777f`, GPL-3.0-or-later in the file header and repository license: the same 511 values match and the same odd-index-`0xFF` accent differs.

The thblt discrepancy was resolved in favor of the RFC-cited immutable revision, as the protocol requires. The PGPfone archive independently agrees with that revision's ASCII `Yucatan`. No PGPfone or thblt code, comments, or licensing terms were copied.

## History

- 1995: Patrick Juola and Philip Zimmermann designed the PGP Word List.
- 4 January 2007: the archived source attributes the incorporated list's GFDL authorization to Jon Callas for PGP Corporation.
- 1 December 2010: Wikipedia revision `oldid=400752943`, later identified by RFC 6189, was published.
- 28 February 2020: the immutable source capture used here was archived.
- 10 November 2026: Remote Energy Meter maintainers mechanically extracted and integrity-pinned the two protocol data files.
