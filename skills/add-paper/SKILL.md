---
name: add-paper
description: Add an academic paper to the papertrail library. Accepts a DOI, arXiv ID, SSRN URL, paper URL, or title search. Runs the full pipeline: find, download, convert, summarize, tag, and store.
disable-model-invocation: true
context: fork
---

# Add Paper to Library

You are adding a paper to the papertrail library. The user provided: $ARGUMENTS

Follow these steps in order.

## Step 1: Find and ingest the paper

If the argument looks like a DOI (contains "10."), arXiv ID (like "2301.12345"),
SSRN URL/ID, or other URL, call `ingest_paper` directly with that identifier.

If the argument is a title or description, first call `find_paper` to search for it.
Pick the best match and call `ingest_paper` with its DOI or arXiv ID.

If `find_paper` returns no results, use web search to find the paper, then try
`ingest_paper` with a DOI or URL from the search results.

## Step 2: Wait for conversion

After ingesting, poll `conversion_status` with the bibtex key every 10 seconds
until the status is "summarizing" (or "error"). Do not poll more than 30 times.

If status is "error", report the error and stop.

## Step 3: Read and summarize

Once status is "summarizing":

1. Call `read_paper` with the bibtex key to read the full markdown
2. Generate a structured JSON summary with these keys:
   - `main_contribution`: 2-3 sentences on the paper's primary contribution
   - `methodology`: Description of the methods used
   - `findings`: Key results and findings
   - `limitations`: Noted limitations or caveats
   - `section_summaries`: Object mapping section headers to 1-2 sentence summaries
   - `key_tables`: Array of objects with `table` and `description` for important tables
   - `key_figures`: Array of objects with `figure` and `description` for important figures
3. Generate 3-8 descriptive keywords for the paper

## Step 4: Assign tags

1. Call `list_tags` to see the current tag vocabulary
2. Choose relevant existing tags for this paper
3. If the paper covers topics not in the vocabulary, call `add_tags` with new tags
   (include a short description for each new tag)
4. Call `tag_paper` with the bibtex key and chosen tags

## Step 5: Store the summary

Call `store_summary` with the bibtex key, the JSON summary string, and the keywords list.

## Step 6: Report

Provide a brief report to the user:
- Paper title and bibtex key
- Main contribution (1-2 sentences)
- Tags assigned
- Confirmation that the paper is ready in the library
