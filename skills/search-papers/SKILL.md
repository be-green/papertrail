---
name: search-papers
description: Search the papertrail library for papers by topic, author, method, or keyword. Returns matching papers with summaries and tags.
---

# Search Papers

Search the papertrail library for: $ARGUMENTS

## Instructions

1. **Decide whether to browse by field or search directly.**
   If `$ARGUMENTS` is a single broad term like "macroeconomics" or "finance",
   or a phrase like "papers in X field" / "all papers on Y", first call
   `list_tags(kind="field")` to surface the available field tags with paper
   counts. Either:
   - Call `list_papers(field=<matching-field>, limit=50)` if the user's
     query corresponds to a single field tag, and present that list.
   - Otherwise proceed to step 2 with the targeted query.

   For specific queries ("term structure", "HANK models", "Acemoglu
   automation"), skip straight to step 2.

2. Call `search_library` with the query "$ARGUMENTS" to search over metadata,
   topics, keywords, and summaries.

3. If the search returns few or no results, try broader queries or
   `search_paper_text` to search the full text content of papers. You can
   also combine filters: `list_papers(field="finance", tag="term-structure")`
   returns finance papers that also carry the term-structure concept.

4. For each matching paper, call `get_paper_metadata` to retrieve the full
   metadata and summary. **Launch these calls in parallel** using the Task
   tool with `general-purpose` subagents (one per paper) if there are 3 or
   more matches. For fewer matches, call `get_paper_metadata` directly in
   parallel.

5. Present results clearly:
   - Paper title, authors, year
   - BibTeX key (for reference in future commands)
   - Tags rendered as `[field1, field2 | concept1, concept2, ...]` so the
     reader can see the field(s) at a glance before the concepts. The
     `list_papers` tool already formats tags this way in its output.
   - Summary highlights (main contribution and key findings)
   - Relevance to the search query

6. If no papers match in the local library, let the user know they can add
   papers with `/add-paper`.
