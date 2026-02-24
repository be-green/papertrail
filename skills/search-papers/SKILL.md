---
name: search-papers
description: Search the papertrail library for papers by topic, author, method, or keyword. Returns matching papers with summaries and tags.
---

# Search Papers

Search the papertrail library for: $ARGUMENTS

## Instructions

1. Call `search_library` with the query "$ARGUMENTS" to search over metadata,
   topics, keywords, and summaries.

2. If the search returns few or no results, try broader queries or
   `search_paper_text` to search the full text content of papers.

3. For each matching paper, call `get_paper_metadata` to retrieve the full
   metadata and summary. **Launch these calls in parallel** using the Task tool
   with `general-purpose` subagents (one per paper) if there are 3 or more
   matches. For fewer matches, call `get_paper_metadata` directly in parallel.

4. Present results clearly:
   - Paper title, authors, year
   - BibTeX key (for reference in future commands)
   - Tags
   - Summary highlights (main contribution and key findings)
   - Relevance to the search query

5. If no papers match in the local library, let the user know they can add
   papers with `/add-paper`.
