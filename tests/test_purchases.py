"""The shared purchase judgement — pure, and genuinely source-agnostic.

`amazon_*` and `costco_*` keep separate tool surfaces on purpose, but their
confidence vocabulary is a promise to the household about its money and must
not drift between them. These tests pin that: the same judgement, on the same
shaped evidence, under each category's own column names.

If a future source is added, adding a case here is how you find out whether it
can reuse the matcher or genuinely needs its own rules.
"""

from __future__ import annotations

from datetime import date

import pytest

from homelab_mcp.tools._http import ToolError
from homelab_mcp.tools._purchases import (
    dollars,
    flag_oversubscribed,
    match_charge,
    none_reason,
    parse_day,
    to_cents,
)

DAY = date(2026, 8, 1)

# The two categories' column names, side by side. Every judgement test runs
# under BOTH so a divergence cannot hide in one of them.
KEYSETS = [
    pytest.param("payment_method_last_4", "order_number", id="amazon"),
    pytest.param("card_last_4", "transaction_barcode", id="costco"),
]


def cand(last_4_key: str, group_key: str, *, last_4: str | None, group: str, rid: int) -> dict:
    return {last_4_key: last_4, group_key: group, "row_id": rid}


class TestMatchIsSourceAgnostic:
    @pytest.mark.parametrize(("last_4_key", "group_key"), KEYSETS)
    def test_verified_card_and_one_group_is_exact(self, last_4_key: str, group_key: str) -> None:
        cands = [cand(last_4_key, group_key, last_4="4772", group="A", rid=1)]
        conf, chosen = match_charge(
            -8431,
            DAY,
            cands,
            expected_last_4="4772",
            last_4_key=last_4_key,
            group_key=group_key,
        )
        assert conf == "exact"
        assert len(chosen) == 1

    @pytest.mark.parametrize(("last_4_key", "group_key"), KEYSETS)
    def test_no_expected_card_is_probable_not_exact(self, last_4_key: str, group_key: str) -> None:
        """A card that was never checked cannot raise confidence to `exact`."""
        cands = [cand(last_4_key, group_key, last_4="4772", group="A", rid=1)]
        conf, _ = match_charge(
            -8431,
            DAY,
            cands,
            expected_last_4=None,
            last_4_key=last_4_key,
            group_key=group_key,
        )
        assert conf == "probable"

    @pytest.mark.parametrize(("last_4_key", "group_key"), KEYSETS)
    def test_two_groups_is_ambiguous(self, last_4_key: str, group_key: str) -> None:
        cands = [
            cand(last_4_key, group_key, last_4=None, group="A", rid=1),
            cand(last_4_key, group_key, last_4=None, group="B", rid=2),
        ]
        conf, chosen = match_charge(
            -8431,
            DAY,
            cands,
            expected_last_4=None,
            last_4_key=last_4_key,
            group_key=group_key,
        )
        assert conf == "ambiguous"
        assert len(chosen) == 2

    @pytest.mark.parametrize(("last_4_key", "group_key"), KEYSETS)
    def test_known_card_matching_nothing_is_ambiguous(
        self, last_4_key: str, group_key: str
    ) -> None:
        """Evidence against every candidate, not for one of them."""
        cands = [cand(last_4_key, group_key, last_4="1111", group="A", rid=1)]
        conf, chosen = match_charge(
            -8431,
            DAY,
            cands,
            expected_last_4="4772",
            last_4_key=last_4_key,
            group_key=group_key,
        )
        assert conf == "ambiguous"
        assert len(chosen) == 1

    @pytest.mark.parametrize(("last_4_key", "group_key"), KEYSETS)
    def test_candidate_without_a_card_is_not_discarded(
        self, last_4_key: str, group_key: str
    ) -> None:
        """Never drop a row over a comparison you could not make."""
        cands = [cand(last_4_key, group_key, last_4=None, group="A", rid=1)]
        conf, chosen = match_charge(
            -8431,
            DAY,
            cands,
            expected_last_4="4772",
            last_4_key=last_4_key,
            group_key=group_key,
        )
        assert conf == "probable"
        assert len(chosen) == 1

    @pytest.mark.parametrize(("last_4_key", "group_key"), KEYSETS)
    def test_no_candidates_is_none(self, last_4_key: str, group_key: str) -> None:
        assert match_charge(
            -8431,
            DAY,
            [],
            expected_last_4="4772",
            last_4_key=last_4_key,
            group_key=group_key,
        ) == ("none", [])

    def test_the_two_keysets_agree_on_identical_evidence(self) -> None:
        """The load-bearing property: same evidence, same verdict, either source.

        Written as one assertion rather than parametrised because the point is
        the COMPARISON — a regression that changed both keysets identically
        would slip past the parametrised cases above.
        """
        for last_4, expected, groups in [
            ("4772", "4772", ["A"]),
            ("4772", None, ["A"]),
            (None, "4772", ["A"]),
            ("1111", "4772", ["A"]),
            (None, None, ["A", "B"]),
        ]:
            verdicts = set()
            for l4_key, g_key in (
                ("payment_method_last_4", "order_number"),
                ("card_last_4", "transaction_barcode"),
            ):
                cands = [
                    cand(l4_key, g_key, last_4=last_4, group=g, rid=i) for i, g in enumerate(groups)
                ]
                conf, _ = match_charge(
                    -8431,
                    DAY,
                    cands,
                    expected_last_4=expected,
                    last_4_key=l4_key,
                    group_key=g_key,
                )
                verdicts.add(conf)
            assert len(verdicts) == 1, f"keysets disagreed: {verdicts}"


class TestOversubscription:
    def test_flags_under_either_id_key(self) -> None:
        for id_key in ("transaction_id", "tender_id"):
            results = [
                {"ref": "a", "confidence": "probable", "candidates": [{id_key: 1}]},
                {"ref": "b", "confidence": "probable", "candidates": [{id_key: 1}]},
            ]
            assert flag_oversubscribed(results, id_key=id_key) == 2
            assert all(r["confidence"] == "ambiguous" for r in results)
            assert results[0]["shares_with"] == ["b"]

    def test_enough_rows_to_go_round_is_left_alone(self) -> None:
        """Two charges, two rows: a coincidence of amounts, not a shortage."""
        results = [
            {"ref": "a", "confidence": "probable", "candidates": [{"tender_id": 1}]},
            {"ref": "b", "confidence": "probable", "candidates": [{"tender_id": 2}]},
        ]
        assert flag_oversubscribed(results, id_key="tender_id") == 0
        assert all(r["confidence"] == "probable" for r in results)

    def test_unmatched_entries_are_ignored(self) -> None:
        results = [{"ref": "a", "confidence": "none", "candidates": []}]
        assert flag_oversubscribed(results, id_key="tender_id") == 0


class TestNoneReason:
    def test_uncovered_month_is_not_no_match(self) -> None:
        """The distinction the household's money rests on."""
        assert (
            none_reason(date(2025, 3, 4), {date(2026, 8, 1)}, today=DAY, stale=False)
            == "outside_coverage"
        )

    def test_covered_month_with_no_row_is_no_amount_match(self) -> None:
        assert (
            none_reason(date(2026, 8, 4), {date(2026, 8, 1)}, today=DAY, stale=False)
            == "no_amount_match"
        )

    def test_a_stale_sync_says_so_for_recent_charges(self) -> None:
        assert (
            none_reason(date(2026, 8, 1), {date(2026, 8, 1)}, today=DAY, stale=True) == "stale_sync"
        )


class TestUnits:
    def test_cents_round_trip(self) -> None:
        assert to_cents(-84.31) == -8431
        assert dollars(-8431) == -84.31
        assert dollars(None) is None

    def test_a_float_that_would_drift_does_not(self) -> None:
        assert to_cents(11.78) == 1178
        assert to_cents(219.18) == 21918

    def test_bad_date_raises_the_shared_contract(self) -> None:
        with pytest.raises(ToolError):
            parse_day("08/01/2026", field="date")

    def test_good_date_parses(self) -> None:
        assert parse_day(" 2026-08-01 ", field="date") == DAY
