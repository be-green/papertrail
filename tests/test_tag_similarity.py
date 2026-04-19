from papertrail.tag_similarity import similar_tags


class TestSharedTokenMatches:
    def test_flags_tag_sharing_non_stopword_token(self):
        matches = similar_tags(
            "graph-methods", ["graph-theory", "graph-neural-networks", "macro"]
        )
        assert "graph-theory" in matches
        assert "graph-neural-networks" in matches
        assert "macro" not in matches

    def test_ignores_stopword_only_overlap(self):
        matches = similar_tags(
            "bayesian-methods", ["computational-methods", "causal-methods"]
        )
        assert matches == []

    def test_token_overlap_ranks_before_edit_distance(self):
        matches = similar_tags(
            "graph-theory",
            ["graph-neural-networks", "grapg-theory"],
        )
        assert matches[0] == "graph-neural-networks"


class TestEditDistanceMatches:
    def test_flags_close_typo(self):
        matches = similar_tags("bayes", ["bayesian", "causal"])
        assert "bayesian" in matches
        assert "causal" not in matches

    def test_respects_max_edit_distance(self):
        matches = similar_tags(
            "xyz", ["abc", "xyzabcd"], max_edit_distance=2
        )
        assert matches == []


class TestExactAndEmpty:
    def test_skips_exact_match(self):
        assert similar_tags("macro", ["macro", "micro"]) == ["micro"]

    def test_empty_vocabulary(self):
        assert similar_tags("anything", []) == []
