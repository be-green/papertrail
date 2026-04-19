import asyncio
import json
import sqlite3
from pathlib import Path

from papertrail.models import PaperMetadata, Tag

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS papers (
    bibtex_key TEXT PRIMARY KEY,
    title TEXT,
    authors TEXT,
    year INTEGER,
    abstract TEXT,
    journal TEXT,
    doi TEXT,
    arxiv_id TEXT,
    ssrn_id TEXT,
    url TEXT,
    topics TEXT,
    keywords TEXT,
    fields_of_study TEXT,
    citation_count INTEGER,
    added_date TEXT,
    status TEXT DEFAULT 'downloading',
    summary TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    tag TEXT PRIMARY KEY,
    description TEXT,
    kind TEXT NOT NULL DEFAULT 'concept',
    paper_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paper_tags (
    bibtex_key TEXT REFERENCES papers(bibtex_key) ON DELETE CASCADE,
    tag TEXT REFERENCES tags(tag) ON DELETE CASCADE,
    PRIMARY KEY (bibtex_key, tag)
);

CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    bibtex_key, title, authors, abstract,
    topics, keywords, fields_of_study, summary
);

CREATE VIRTUAL TABLE IF NOT EXISTS fulltext_fts USING fts5(
    bibtex_key, content
);
"""

DROP_SQL = """
DROP TABLE IF EXISTS paper_tags;
DROP TABLE IF EXISTS tags;
DROP TABLE IF EXISTS papers;
DROP TABLE IF EXISTS papers_fts;
DROP TABLE IF EXISTS fulltext_fts;
"""


def _json_list_to_text(items: list[str]) -> str:
    """Convert a list of strings to space-separated text for FTS indexing."""
    return " ".join(items)


class PaperDatabase:
    def __init__(self, index_path: Path):
        self.index_path = index_path
        self._connection: sqlite3.Connection | None = None

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(self.index_path), check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.row_factory = sqlite3.Row
        return self._connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._sync_initialize)

    def _sync_initialize(self) -> None:
        conn = self._ensure_connection()
        conn.executescript(DROP_SQL)
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    async def rebuild_from_papers(self, papers: list[PaperMetadata], tags: list[dict]) -> None:
        await asyncio.to_thread(self._sync_rebuild_from_papers, papers, tags)

    def _sync_rebuild_from_papers(self, papers: list[PaperMetadata], tags: list[dict]) -> None:
        conn = self._ensure_connection()

        # Bulk insert tags
        for tag_data in tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (tag, description, kind, paper_count) VALUES (?, ?, ?, 0)",
                (
                    tag_data["tag"],
                    tag_data.get("description"),
                    tag_data.get("kind", "concept"),
                ),
            )

        # Bulk insert papers and FTS entries
        for paper in papers:
            summary_json = json.dumps(paper.summary) if paper.summary else None
            conn.execute(
                """INSERT OR REPLACE INTO papers
                (bibtex_key, title, authors, year, abstract, journal, doi, arxiv_id,
                 ssrn_id, url, topics, keywords, fields_of_study, citation_count,
                 added_date, status, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    paper.bibtex_key,
                    paper.title,
                    json.dumps(paper.authors),
                    paper.year,
                    paper.abstract,
                    paper.journal,
                    paper.doi,
                    paper.arxiv_id,
                    paper.ssrn_id,
                    paper.url,
                    json.dumps(paper.topics),
                    json.dumps(paper.keywords),
                    json.dumps(paper.fields_of_study),
                    paper.citation_count,
                    paper.added_date,
                    paper.status,
                    summary_json,
                ),
            )
            self._sync_update_fts(conn, paper)

            # Insert paper_tags from the paper's tags list
            for tag_name in paper.tags:
                conn.execute(
                    "INSERT OR IGNORE INTO paper_tags (bibtex_key, tag) VALUES (?, ?)",
                    (paper.bibtex_key, tag_name),
                )

        # Recompute tag paper_counts
        conn.execute(
            """UPDATE tags SET paper_count = (
                SELECT COUNT(*) FROM paper_tags WHERE paper_tags.tag = tags.tag
            )"""
        )

        conn.commit()

    async def rebuild_fulltext(self, paper_texts: list[tuple[str, str]]) -> None:
        await asyncio.to_thread(self._sync_rebuild_fulltext, paper_texts)

    def _sync_rebuild_fulltext(self, paper_texts: list[tuple[str, str]]) -> None:
        conn = self._ensure_connection()
        for bibtex_key, content in paper_texts:
            conn.execute(
                "INSERT INTO fulltext_fts (bibtex_key, content) VALUES (?, ?)",
                (bibtex_key, content),
            )
        conn.commit()

    async def upsert_paper(self, paper: PaperMetadata) -> None:
        await asyncio.to_thread(self._sync_upsert_paper, paper)

    def _sync_upsert_paper(self, paper: PaperMetadata) -> None:
        conn = self._ensure_connection()
        summary_json = json.dumps(paper.summary) if paper.summary else None
        conn.execute(
            """INSERT OR REPLACE INTO papers
            (bibtex_key, title, authors, year, abstract, journal, doi, arxiv_id,
             ssrn_id, url, topics, keywords, fields_of_study, citation_count,
             added_date, status, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper.bibtex_key,
                paper.title,
                json.dumps(paper.authors),
                paper.year,
                paper.abstract,
                paper.journal,
                paper.doi,
                paper.arxiv_id,
                paper.ssrn_id,
                paper.url,
                json.dumps(paper.topics),
                json.dumps(paper.keywords),
                json.dumps(paper.fields_of_study),
                paper.citation_count,
                paper.added_date,
                paper.status,
                summary_json,
            ),
        )
        self._sync_update_fts(conn, paper)

        # Update paper_tags from the paper's tags list
        conn.execute("DELETE FROM paper_tags WHERE bibtex_key = ?", (paper.bibtex_key,))
        for tag_name in paper.tags:
            conn.execute(
                "INSERT OR IGNORE INTO paper_tags (bibtex_key, tag) VALUES (?, ?)",
                (paper.bibtex_key, tag_name),
            )
            conn.execute(
                """UPDATE tags SET paper_count = (
                    SELECT COUNT(*) FROM paper_tags WHERE tag = ?
                ) WHERE tag = ?""",
                (tag_name, tag_name),
            )

        conn.commit()

    def _sync_update_fts(self, conn: sqlite3.Connection, paper: PaperMetadata) -> None:
        """Update the papers_fts table for a given paper."""
        conn.execute("DELETE FROM papers_fts WHERE bibtex_key = ?", (paper.bibtex_key,))

        summary_text = ""
        if paper.summary:
            summary_text = json.dumps(paper.summary)

        conn.execute(
            """INSERT INTO papers_fts
            (bibtex_key, title, authors, abstract, topics, keywords,
             fields_of_study, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper.bibtex_key,
                paper.title,
                _json_list_to_text(paper.authors),
                paper.abstract or "",
                _json_list_to_text(paper.topics),
                _json_list_to_text(paper.keywords),
                _json_list_to_text(paper.fields_of_study),
                summary_text,
            ),
        )

    async def get_paper(self, bibtex_key: str) -> PaperMetadata | None:
        return await asyncio.to_thread(self._sync_get_paper, bibtex_key)

    def _sync_get_paper(self, bibtex_key: str) -> PaperMetadata | None:
        conn = self._ensure_connection()
        row = conn.execute(
            "SELECT * FROM papers WHERE bibtex_key = ?", (bibtex_key,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_paper(row)

    async def list_papers(
        self,
        status: str | None = None,
        tag: str | None = None,
        field: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PaperMetadata]:
        return await asyncio.to_thread(
            self._sync_list_papers, status, tag, field, limit, offset
        )

    def _sync_list_papers(
        self,
        status: str | None,
        tag: str | None,
        field: str | None,
        limit: int,
        offset: int,
    ) -> list[PaperMetadata]:
        conn = self._ensure_connection()
        joins: list[str] = []
        where: list[str] = []
        params: list = []
        if tag:
            joins.append(
                "JOIN paper_tags pt_tag ON p.bibtex_key = pt_tag.bibtex_key"
            )
            where.append("pt_tag.tag = ?")
            params.append(tag)
        if field:
            joins.append(
                "JOIN paper_tags pt_field ON p.bibtex_key = pt_field.bibtex_key "
                "JOIN tags t_field ON pt_field.tag = t_field.tag"
            )
            where.append("pt_field.tag = ? AND t_field.kind = 'field'")
            params.append(field)
        if status:
            where.append("p.status = ?")
            params.append(status)
        query = "SELECT DISTINCT p.* FROM papers p"
        if joins:
            query += " " + " ".join(joins)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY p.year DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        return [self._row_to_paper(row) for row in rows]

    async def update_status(self, bibtex_key: str, status: str) -> None:
        await asyncio.to_thread(self._sync_update_status, bibtex_key, status)

    def _sync_update_status(self, bibtex_key: str, status: str) -> None:
        conn = self._ensure_connection()
        conn.execute(
            "UPDATE papers SET status = ? WHERE bibtex_key = ?",
            (status, bibtex_key),
        )
        conn.commit()

    async def store_summary(self, bibtex_key: str, summary: dict) -> None:
        await asyncio.to_thread(self._sync_store_summary, bibtex_key, summary)

    def _sync_store_summary(self, bibtex_key: str, summary: dict) -> None:
        conn = self._ensure_connection()
        summary_json = json.dumps(summary)
        conn.execute(
            "UPDATE papers SET summary = ? WHERE bibtex_key = ?",
            (summary_json, bibtex_key),
        )
        # Re-index FTS with updated summary
        paper = self._sync_get_paper(bibtex_key)
        if paper:
            paper.summary = summary
            self._sync_update_fts(conn, paper)
        conn.commit()

    async def update_keywords(self, bibtex_key: str, keywords: list[str]) -> None:
        await asyncio.to_thread(self._sync_update_keywords, bibtex_key, keywords)

    def _sync_update_keywords(self, bibtex_key: str, keywords: list[str]) -> None:
        conn = self._ensure_connection()
        conn.execute(
            "UPDATE papers SET keywords = ? WHERE bibtex_key = ?",
            (json.dumps(keywords), bibtex_key),
        )
        paper = self._sync_get_paper(bibtex_key)
        if paper:
            paper.keywords = keywords
            self._sync_update_fts(conn, paper)
        conn.commit()

    async def search_metadata(self, query: str, limit: int = 20) -> list[PaperMetadata]:
        return await asyncio.to_thread(self._sync_search_metadata, query, limit)

    def _sync_search_metadata(self, query: str, limit: int) -> list[PaperMetadata]:
        conn = self._ensure_connection()
        sanitized = self._sanitize_fts_query(query)
        try:
            rows = conn.execute(
                """SELECT p.* FROM papers p
                JOIN papers_fts fts ON p.bibtex_key = fts.bibtex_key
                WHERE papers_fts MATCH ?
                ORDER BY rank
                LIMIT ?""",
                (sanitized, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # If FTS query fails, fall back to LIKE search on title
            rows = conn.execute(
                "SELECT * FROM papers WHERE title LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [self._row_to_paper(row) for row in rows]

    async def search_fulltext(self, query: str, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._sync_search_fulltext, query, limit)

    def _sync_search_fulltext(self, query: str, limit: int) -> list[dict]:
        conn = self._ensure_connection()
        sanitized = self._sanitize_fts_query(query)
        try:
            rows = conn.execute(
                """SELECT bibtex_key, snippet(fulltext_fts, 1, '>>>', '<<<', '...', 64)
                as snippet
                FROM fulltext_fts
                WHERE fulltext_fts MATCH ?
                ORDER BY rank
                LIMIT ?""",
                (sanitized, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"bibtex_key": row[0], "snippet": row[1]} for row in rows]

    async def index_fulltext(self, bibtex_key: str, content: str) -> None:
        await asyncio.to_thread(self._sync_index_fulltext, bibtex_key, content)

    def _sync_index_fulltext(self, bibtex_key: str, content: str) -> None:
        conn = self._ensure_connection()
        conn.execute("DELETE FROM fulltext_fts WHERE bibtex_key = ?", (bibtex_key,))
        conn.execute(
            "INSERT INTO fulltext_fts (bibtex_key, content) VALUES (?, ?)",
            (bibtex_key, content),
        )
        conn.commit()

    async def add_tags(self, tags: list[dict]) -> None:
        await asyncio.to_thread(self._sync_add_tags, tags)

    def _sync_add_tags(self, tags: list[dict]) -> None:
        conn = self._ensure_connection()
        for tag_data in tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (tag, description, kind, paper_count) VALUES (?, ?, ?, 0)",
                (
                    tag_data["tag"],
                    tag_data.get("description"),
                    tag_data.get("kind", "concept"),
                ),
            )
        conn.commit()

    async def list_tags(
        self, prefix: str | None = None, kind: str | None = None
    ) -> list[Tag]:
        return await asyncio.to_thread(self._sync_list_tags, prefix, kind)

    def _sync_list_tags(
        self, prefix: str | None, kind: str | None
    ) -> list[Tag]:
        conn = self._ensure_connection()
        clauses: list[str] = []
        params: list = []
        if prefix:
            clauses.append("tag LIKE ?")
            params.append(f"{prefix}%")
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM tags{where} ORDER BY paper_count DESC",
            params,
        ).fetchall()
        return [
            Tag(
                tag=row["tag"],
                description=row["description"],
                kind=row["kind"] if "kind" in row.keys() else "concept",
                paper_count=row["paper_count"],
            )
            for row in rows
        ]

    async def tag_paper(self, bibtex_key: str, tags: list[str]) -> None:
        await asyncio.to_thread(self._sync_tag_paper, bibtex_key, tags)

    def _sync_tag_paper(self, bibtex_key: str, tags: list[str]) -> None:
        conn = self._ensure_connection()
        for tag in tags:
            conn.execute(
                "INSERT OR IGNORE INTO paper_tags (bibtex_key, tag) VALUES (?, ?)",
                (bibtex_key, tag),
            )
            conn.execute(
                """UPDATE tags SET paper_count = (
                    SELECT COUNT(*) FROM paper_tags WHERE tag = ?
                ) WHERE tag = ?""",
                (tag, tag),
            )
        conn.commit()

    async def remove_paper_tags(
        self, bibtex_key: str, tags: list[str]
    ) -> None:
        await asyncio.to_thread(self._sync_remove_paper_tags, bibtex_key, tags)

    def _sync_remove_paper_tags(
        self, bibtex_key: str, tags: list[str]
    ) -> None:
        conn = self._ensure_connection()
        for tag in tags:
            conn.execute(
                "DELETE FROM paper_tags WHERE bibtex_key = ? AND tag = ?",
                (bibtex_key, tag),
            )
            conn.execute(
                """UPDATE tags SET paper_count = (
                    SELECT COUNT(*) FROM paper_tags WHERE tag = ?
                ) WHERE tag = ?""",
                (tag, tag),
            )
        conn.commit()

    async def apply_tag_rewrite(self, mapping: dict[str, str | None]) -> None:
        """Rename, merge, or strip tags across the paper_tags index.

        For each (old, new) pair: if `new` is None, remove all paper_tags rows
        for `old`. Otherwise move rows from `old` to `new`, deduplicating when a
        paper already carries `new`. Target tags are auto-inserted into the
        tags table if missing (with NULL description — caller is expected to
        upsert descriptions separately). Paper counts are recomputed for every
        tag touched.
        """
        await asyncio.to_thread(self._sync_apply_tag_rewrite, mapping)

    def _sync_apply_tag_rewrite(self, mapping: dict[str, str | None]) -> None:
        if not mapping:
            return
        conn = self._ensure_connection()
        for old, new in mapping.items():
            if new is None:
                conn.execute("DELETE FROM paper_tags WHERE tag = ?", (old,))
                continue
            conn.execute(
                "INSERT OR IGNORE INTO tags (tag, description, paper_count) VALUES (?, NULL, 0)",
                (new,),
            )
            conn.execute(
                """INSERT OR IGNORE INTO paper_tags (bibtex_key, tag)
                   SELECT bibtex_key, ? FROM paper_tags WHERE tag = ?""",
                (new, old),
            )
            conn.execute("DELETE FROM paper_tags WHERE tag = ?", (old,))
        touched = {t for pair in mapping.items() for t in pair if t}
        for tag in touched:
            conn.execute(
                """UPDATE tags SET paper_count = (
                    SELECT COUNT(*) FROM paper_tags WHERE tag = ?
                ) WHERE tag = ?""",
                (tag, tag),
            )
        conn.commit()

    async def upsert_tag(
        self,
        tag: str,
        description: str | None = None,
        kind: str | None = None,
    ) -> None:
        await asyncio.to_thread(self._sync_upsert_tag, tag, description, kind)

    def _sync_upsert_tag(
        self, tag: str, description: str | None, kind: str | None
    ) -> None:
        conn = self._ensure_connection()
        row = conn.execute(
            "SELECT description, kind FROM tags WHERE tag = ?", (tag,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tags (tag, description, kind, paper_count) VALUES (?, ?, ?, 0)",
                (tag, description, kind or "concept"),
            )
        else:
            updates: list[str] = []
            params: list = []
            if description is not None and row["description"] != description:
                updates.append("description = ?")
                params.append(description)
            if kind is not None and row["kind"] != kind:
                updates.append("kind = ?")
                params.append(kind)
            if updates:
                params.append(tag)
                conn.execute(
                    f"UPDATE tags SET {', '.join(updates)} WHERE tag = ?",
                    params,
                )
        conn.commit()

    async def set_tag_kind(self, tag: str, kind: str) -> bool:
        """Flip a tag's kind between 'field' and 'concept'. Returns False
        if the tag doesn't exist.
        """
        return await asyncio.to_thread(self._sync_set_tag_kind, tag, kind)

    def _sync_set_tag_kind(self, tag: str, kind: str) -> bool:
        conn = self._ensure_connection()
        row = conn.execute(
            "SELECT 1 FROM tags WHERE tag = ?", (tag,)
        ).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE tags SET kind = ? WHERE tag = ?", (kind, tag))
        conn.commit()
        return True

    async def delete_tags_from_vocab(self, tags: list[str]) -> None:
        """Remove tags from the tags table. paper_tags rows cascade."""
        await asyncio.to_thread(self._sync_delete_tags_from_vocab, tags)

    def _sync_delete_tags_from_vocab(self, tags: list[str]) -> None:
        conn = self._ensure_connection()
        for tag in tags:
            conn.execute("DELETE FROM tags WHERE tag = ?", (tag,))
        conn.commit()

    async def prune_empty_tags(self) -> list[str]:
        """Drop tags with zero papers. Returns the removed tag names."""
        return await asyncio.to_thread(self._sync_prune_empty_tags)

    def _sync_prune_empty_tags(self) -> list[str]:
        conn = self._ensure_connection()
        rows = conn.execute(
            "SELECT tag FROM tags WHERE paper_count = 0"
        ).fetchall()
        removed = [row["tag"] for row in rows]
        for tag in removed:
            conn.execute("DELETE FROM tags WHERE tag = ?", (tag,))
        conn.commit()
        return removed

    async def get_paper_tags(self, bibtex_key: str) -> list[str]:
        return await asyncio.to_thread(self._sync_get_paper_tags, bibtex_key)

    def _sync_get_paper_tags(self, bibtex_key: str) -> list[str]:
        conn = self._ensure_connection()
        rows = conn.execute(
            "SELECT tag FROM paper_tags WHERE bibtex_key = ? ORDER BY tag",
            (bibtex_key,),
        ).fetchall()
        return [row["tag"] for row in rows]

    async def check_bibtex_key_exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._sync_check_key_exists, key)

    def _sync_check_key_exists(self, key: str) -> bool:
        conn = self._ensure_connection()
        row = conn.execute(
            "SELECT 1 FROM papers WHERE bibtex_key = ?", (key,)
        ).fetchone()
        return row is not None

    async def delete_paper(self, bibtex_key: str) -> bool:
        return await asyncio.to_thread(self._sync_delete_paper, bibtex_key)

    def _sync_delete_paper(self, bibtex_key: str) -> bool:
        conn = self._ensure_connection()
        row = conn.execute("SELECT 1 FROM papers WHERE bibtex_key = ?", (bibtex_key,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM paper_tags WHERE bibtex_key = ?", (bibtex_key,))
        conn.execute("DELETE FROM papers_fts WHERE bibtex_key = ?", (bibtex_key,))
        conn.execute("DELETE FROM fulltext_fts WHERE bibtex_key = ?", (bibtex_key,))
        conn.execute("DELETE FROM papers WHERE bibtex_key = ?", (bibtex_key,))
        conn.commit()
        return True

    async def close(self) -> None:
        await asyncio.to_thread(self._sync_close)

    def _sync_close(self) -> None:
        if self._connection:
            self._connection.close()
            self._connection = None

    def _row_to_paper(self, row: sqlite3.Row) -> PaperMetadata:
        summary = None
        if row["summary"]:
            try:
                summary = json.loads(row["summary"])
            except (json.JSONDecodeError, TypeError):
                summary = None

        return PaperMetadata(
            bibtex_key=row["bibtex_key"],
            title=row["title"] or "",
            authors=json.loads(row["authors"]) if row["authors"] else [],
            year=row["year"],
            abstract=row["abstract"],
            journal=row["journal"],
            doi=row["doi"],
            arxiv_id=row["arxiv_id"],
            ssrn_id=row["ssrn_id"],
            url=row["url"],
            topics=json.loads(row["topics"]) if row["topics"] else [],
            keywords=json.loads(row["keywords"]) if row["keywords"] else [],
            fields_of_study=json.loads(row["fields_of_study"]) if row["fields_of_study"] else [],
            citation_count=row["citation_count"],
            added_date=row["added_date"] or "",
            status=row["status"] or "downloading",
            summary=summary,
        )

    def _sanitize_fts_query(self, query: str) -> str:
        """Sanitize a query for FTS5. Wrap in double quotes if it contains special chars."""
        special_chars = set('"*(){}[]^~:')
        if any(c in special_chars for c in query):
            escaped = query.replace('"', '""')
            return f'"{escaped}"'
        # For multi-word queries without special chars, treat each word as a prefix match
        words = query.split()
        if len(words) > 1:
            return " ".join(f"{word}*" if not word.endswith("*") else word for word in words)
        return f"{query}*" if query and not query.endswith("*") else query
