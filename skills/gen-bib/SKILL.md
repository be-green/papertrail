---
name: gen-bib
description: Generate a combined references.bib file from all citation.bib entries in the papertrail library. Optionally filter by tag.
context: fork
---

# Generate references.bib

You are generating a combined BibTeX file from the papertrail library.

Arguments: $ARGUMENTS

## Step 1: Determine output path and optional tag filter

- If the user specified an output path, use that. Otherwise default to `references.bib`
  in the current working directory.
- If the user specified a tag (e.g., `/gen-bib climate-risk`), use it to filter papers.

## Step 2: Collect citation entries

1. Call `list_papers` (with `tag` filter if specified) to get all papers.
2. Use Bash to read all citation.bib files:
   ```
   cat ~/.papertrail/papers/*/citation.bib
   ```
   If filtering by tag, read only the specific paper directories from the list.
3. Track which papers from the list are missing a `citation.bib` file.

## Step 3: Write the output file

1. Sort the BibTeX entries alphabetically by cite key.
2. Separate entries with a blank line.
3. Write the combined content to the output path.

## Step 4: Report

- Number of entries written
- Output file path
- Any papers missing `citation.bib` (list their bibtex keys so the user can
  run `fetch_bibtex` for them)
