---
name: read-paper
description: Read a paper from the papertrail library. Finds the paper, shows its summary, and reads relevant sections into context.
disable-model-invocation: true
---

# Read Paper

Read paper from the library: $ARGUMENTS

## Instructions

1. **Find the paper:**
   - If the argument looks like a bibtex key (lowercase with underscores, e.g.
     "smith_2024_causal"), call `get_paper_metadata` directly with that key.
   - Otherwise, call `search_library` to find matching papers and pick the
     best match.

2. **Show the summary:**
   Present the paper's metadata and summary clearly: title, authors, year,
   tags, main contribution, methodology, findings, limitations.

3. **Read relevant content:**
   Based on the user's interest (if they mentioned a specific topic, result,
   or section) or the paper's structure from the summary, identify which
   sections would be most useful to read in detail.

   Call `read_paper` with appropriate line ranges to read those sections.
   Present the content with section headers noted.

4. **Offer to continue:**
   Let the user know they can ask about specific sections, methodology
   details, tables, figures, or related papers in the library.
