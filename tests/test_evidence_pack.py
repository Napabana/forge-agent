from evals.verify_evidence_pack import ROOT, verify
from evals.verify_p2_repo_map_real import verify as verify_p2_real


def test_evidence_pack_frozen_reports_and_index_are_consistent() -> None:
    assert verify(ROOT) == []


def test_p2_repo_map_real_model_small_sample_is_frozen() -> None:
    assert verify_p2_real(ROOT) == []
