from evals.verify_evidence_pack import ROOT, verify


def test_evidence_pack_frozen_reports_and_index_are_consistent() -> None:
    assert verify(ROOT) == []
