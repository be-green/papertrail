---
name: verify-summary
description: Verify that a paper's stored summary is grounded in its actual text. Checks for hallucinated claims not supported by the paper content. Can verify a single paper or all papers in the library.
context: fork
---

# Verify Summary

Verify paper summaries are grounded in actual paper text: $ARGUMENTS

## Instructions

### Determine scope

- If the argument is a bibtex key (e.g., "smith_2024_causal"), verify that single paper.
- If the argument is "all" or empty, verify all papers with status "ready" by running
  each verification as a parallel subagent (Step 2).

### Step 1: Get paper list

If verifying all papers, call `list_papers` with `status="ready"` to get all papers
that have summaries. Extract the bibtex keys.

### Step 2: Verify each paper

For each paper, launch a `general-purpose` subagent using the Task tool. Launch
**all subagents in a single message** so they run concurrently.

Each subagent prompt should be (fill in bibtex_key):

> Verify that the stored summary for paper "{bibtex_key}" is grounded in its text.
>
> 1. Call `get_paper_metadata` with bibtex_key "{bibtex_key}" to get the stored
>    summary and keywords.
> 2. Call `read_paper` with `bibtex_key`, `start_line=1`, `end_line=500` to get
>    the first chunk and the total line count.
> 3. You MUST read the ENTIRE paper. Call `read_paper` for ALL remaining chunks
>    **in parallel** (e.g., 501-1000, 1001-1500, etc., using 500-line chunks up
>    to the total line count). Do not skip any chunks. Specific numbers and
>    results often appear in tables, results sections, and appendices -- you
>    cannot verify a summary without reading those.
> 4. Once you have the full text, check every claim in the summary
>    (main_contribution, methodology, findings, limitations, section_summaries,
>    key_tables, key_figures) against the paper text. Search the full text for
>    the relevant numbers, quotes, or assertions. Only flag a claim if you have
>    read the entire paper and the claim is genuinely absent or contradicted.
> 5. Return a verification report with:
>    - `bibtex_key`: the paper's key
>    - `total_lines_read`: the total number of lines you read (must match the
>      paper's total line count)
>    - `status`: "pass" if all claims are grounded, "fail" if any are not,
>      "partial" if the paper text is incomplete or illegible in places
>    - `issues`: array of objects, each with:
>      - `field`: which summary field has the issue (e.g., "findings")
>      - `claim`: the specific claim that is not grounded
>      - `reason`: why it appears ungrounded (e.g., "not mentioned anywhere in
>        the paper", "paper says X but summary says Y", "section garbled by
>        PDF conversion")
>    - `notes`: any other observations (e.g., "PDF conversion quality is poor",
>      "some tables are garbled")
>
> IMPORTANT: "not found in the portion of text examined" is NOT a valid reason
> to flag a claim. You must read the entire paper before flagging anything.
> Only flag claims that are genuinely absent from or contradicted by the full
> paper text. General paraphrasing is fine as long as the meaning is faithful.

### Step 3: Report results

Once all subagents return, compile a report:

1. **Overall**: X of Y papers passed, Z had issues
2. **Issues found**: For each paper with issues, list:
   - Paper title and bibtex key
   - Each flagged claim and why it failed
3. **Recommendations**: If any papers have issues, suggest re-summarizing them
   with `/add-paper` (which will re-read and re-summarize from the paper text).

If a paper's summary needs to be regenerated, the user can:
1. Read the paper with `read_paper`
2. Generate a new summary
3. Store it with `store_summary`
