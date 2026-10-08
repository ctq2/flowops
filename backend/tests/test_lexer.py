"""Token-level tests for the rule language."""

from __future__ import annotations

import unittest

from app.domain.errors import RuleSyntaxError
from app.domain.rules.lexer import Lexer, tokenize
from app.domain.rules.tokens import Kind


class LexerTests(unittest.TestCase):
    def kinds(self, source: str) -> list[Kind]:
        return [token.kind for token in tokenize(source)]

    def test_numbers_int_and_float(self) -> None:
        tokens = tokenize("12 3.5 0.25")
        self.assertEqual([t.value for t in tokens[:3]], [12, 3.5, 0.25])
        self.assertTrue(all(t.kind is Kind.NUMBER for t in tokens[:3]))

    def test_two_digit_operators_are_not_split(self) -> None:
        tokens = tokenize("a >= 1 && b <= 2 || c != 3")
        kinds = [t.kind for t in tokens]
        self.assertIn(Kind.GTE, kinds)
        self.assertIn(Kind.AND, kinds)
        self.assertIn(Kind.LTE, kinds)
        self.assertIn(Kind.OR, kinds)
        self.assertIn(Kind.NEQ, kinds)

    def test_single_equals_is_accepted_as_equality(self) -> None:
        # Operators paste policy snippets from SQL and spreadsheets; accepting a
        # lone '=' removes a pointless failure mode.
        tokens = tokenize("status = 'open'")
        self.assertIn(Kind.EQ, [t.kind for t in tokens])

    def test_keywords_are_case_insensitive(self) -> None:
        tokens = tokenize("AND Or NOT In TRUE False Null")
        self.assertEqual(
            [t.kind for t in tokens[:7]],
            [Kind.AND, Kind.OR, Kind.NOT, Kind.IN, Kind.TRUE, Kind.FALSE, Kind.NULL],
        )

    def test_string_escapes(self) -> None:
        token = tokenize(r"'it\'s \n done'")[0]
        self.assertEqual(token.value, "it's \n done")

    def test_double_quoted_strings(self) -> None:
        self.assertEqual(tokenize('"payments"')[0].value, "payments")

    def test_comments_run_to_end_of_line(self) -> None:
        tokens = tokenize("1 + 2 # a comment with == inside\n+ 3")
        self.assertEqual([t.value for t in tokens if t.kind is Kind.NUMBER], [1, 2, 3])

    def test_unterminated_string_reports_position(self) -> None:
        with self.assertRaises(RuleSyntaxError) as ctx:
            tokenize("title == 'oops")
        self.assertIn("unterminated string", str(ctx.exception))
        self.assertIn("^", str(ctx.exception))

    def test_line_numbers_track_newlines(self) -> None:
        lexer = Lexer("1 +\n2 +\n3")
        lines = [token.line for token in lexer.tokenize() if token.kind is Kind.NUMBER]
        self.assertEqual(lines, [1, 2, 3])

    def test_unknown_escape_is_rejected(self) -> None:
        with self.assertRaises(RuleSyntaxError):
            tokenize(r"'\q'")

    def test_illegal_character_is_rejected(self) -> None:
        with self.assertRaises(RuleSyntaxError) as ctx:
            tokenize("a $ b")
        self.assertIn("unexpected character", str(ctx.exception))

    def test_tokenizing_terminates_at_end_of_input(self) -> None:
        # Regression: `"" in "0123456789"` is True in Python, so a string-based
        # character class made the number scanner loop forever at EOF.  A hang is
        # the worst failure mode for a request-path component, so it is pinned
        # here explicitly.
        for source in ("1", "1.5", "12 3.5 0.25", "a", "a.b.c", "1+1", "f(1, 'x')", ""):
            with self.subTest(source=source):
                tokens = tokenize(source)
                self.assertIs(tokens[-1].kind, Kind.EOF)

    def test_floats_at_end_of_input_keep_their_value(self) -> None:
        self.assertEqual(tokenize("0.25")[0].value, 0.25)
        self.assertEqual(tokenize("3.5")[0].value, 3.5)

    def test_trailing_whitespace_only_produces_eof(self) -> None:
        self.assertEqual(self.kinds("   \t\n  "), [Kind.EOF])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
