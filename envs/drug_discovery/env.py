import logging
from rdkit import Chem
from rdkit.Chem import QED, FilterCatalog, AllChem, Descriptors
from rdkit import DataStructs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. QED Score
# ---------------------------------------------------------------------------

def get_qed_score(mol) -> float:
    """Quantitative Estimate of Drug-likeness. Range: [0.0, 1.0]."""
    if mol is None:
        return 0.0
    try:
        return float(QED.qed(mol))
    except Exception as e:
        logger.error(f"QED error: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# 2. SA Score  (Ertl-Schuffenhauer)
# ---------------------------------------------------------------------------

try:
    from . import sascorer
except ImportError:
    try:
        import sascorer
    except ImportError:
        sascorer = None
        logger.warning("sascorer not found — SA scores will use neutral fallback 0.5")


def get_sa_score_normalized(mol) -> float:
    """
    Synthetic Accessibility normalised to [0.0, 1.0].
    Raw SA: 1 (easy to synthesise) → 10 (hard).
    Normalised: 1.0 = easy, 0.0 = hard.
    Falls back to 0.5 (neutral) when sascorer is unavailable — NOT 0.0,
    which would incorrectly map to a perfect score of 1.0.
    """
    if mol is None:
        return 0.0
    if sascorer is None:
        return 0.5  # neutral; avoids rewarding or punishing the agent unfairly
    try:
        raw = float(sascorer.calculateScore(mol))
        return max(0.0, min(1.0, (10.0 - raw) / 9.0))
    except Exception as e:
        logger.error(f"SA score error: {e}")
        return 0.5


# ---------------------------------------------------------------------------
# 3. Lipinski Ro5 Bonus
# ---------------------------------------------------------------------------

def get_lipinski_score(mol) -> float:
    """
    Fraction of Lipinski Rule-of-Five criteria satisfied: [0.0, 1.0].
    Provides a soft drug-likeness bonus on top of QED.
    """
    if mol is None:
        return 0.0
    try:
        rules = [
            Descriptors.MolWt(mol) <= 500,
            Descriptors.MolLogP(mol) <= 5,
            Descriptors.NumHDonors(mol) <= 5,
            Descriptors.NumHAcceptors(mol) <= 10,
        ]
        return float(sum(rules) / len(rules))
    except Exception as e:
        logger.error(f"Lipinski error: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# 4. PAINS Filter
# ---------------------------------------------------------------------------

_pains_catalog = None


def _get_pains_catalog():
    global _pains_catalog
    if _pains_catalog is None:
        params = FilterCatalog.FilterCatalogParams()
        params.AddCatalog(FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS)
        _pains_catalog = FilterCatalog.FilterCatalog(params)
    return _pains_catalog


def get_pains_penalty(mol) -> float:
    """
    Additive penalty: 0.0 if molecule passes PAINS, -0.20 if it fails.

    Original code used a multiplicative penalty (score × 0), which wiped
    out all gradient signal and gave agents zero feedback on how close they
    were.  An additive penalty preserves the gradient while still penalising
    problematic substructures.
    """
    if mol is None:
        return -0.20
    try:
        catalog = _get_pains_catalog()
        return -0.20 if catalog.HasMatch(mol) else 0.0
    except Exception as e:
        logger.error(f"PAINS error: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# 5. Morgan Fingerprint + Tanimoto Similarity
# ---------------------------------------------------------------------------

# Representative ChEMBL actives for each target
CHEMBL_ACTIVES = {
    # Gefitinib — EGFR inhibitor (Cancer)
    "EGFR": "COC1=C(OCCCN2CCOCC2)C=C2C(NC3=CC=C(F)C(Cl)=C3)=NC=NC2=C1",
    # Venetoclax — BCL-2 inhibitor (Lymphoma)
    "BCL-2": (
        "CC1(C)CCC(CN2CCN(c3ccc(C(=O)NS(=O)(=O)c4ccc(NCC5CCN(c6ccc(F)cc6)CC5)"
        "c([N+](=O)[O-])c4)cc3)CC2)=C(c2ccc(Cl)cc2)C1"
    ),
    # Nirmatrelvir proxy — Mpro / SARS-CoV-2 main protease
    "Mpro": "CC1(C)C2C1C(C(=O)N1CC3C(CCC3)C1C(=O)NC(C#N)CC1CCNC1=O)NC2(=O)C(F)(F)F",
}

# Cache pre-computed fingerprints to avoid recalculating on every step
_active_fps: dict = {}


def _get_active_fp(target_name: str):
    """Returns (and caches) the Morgan fingerprint for the target reference."""
    if target_name not in _active_fps:
        smiles = CHEMBL_ACTIVES.get(target_name)
        if smiles is None:
            return None
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        _active_fps[target_name] = AllChem.GetMorganFingerprintAsBitVect(
            mol, radius=2, nBits=2048
        )
    return _active_fps[target_name]


def get_tanimoto_similarity(mol, target_name: str) -> float:
    """Tanimoto similarity vs. the target's ChEMBL reference compound. Range [0.0, 1.0]."""
    if mol is None:
        return 0.0
    fp_active = _get_active_fp(target_name)
    if fp_active is None:
        logger.warning(f"No reference fingerprint for target '{target_name}'")
        return 0.0
    try:
        fp_mol = AllChem.GetMorganFingerprintAsBitVect(mol, radius=2, nBits=2048)
        return float(DataStructs.TanimotoSimilarity(fp_mol, fp_active))
    except Exception as e:
        logger.error(f"Tanimoto error: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# 6. Task-specific Reward Weights
# ---------------------------------------------------------------------------

# Each task has a different scientific objective, so the component weights differ.
#
#  lead_optimization (Easy / EGFR):
#    Agent starts from a known inhibitor.  QED and SA improvements matter equally
#    to maintaining structural similarity — balanced weights.
#
#  scaffold_hopping (Medium / BCL-2):
#    Agent must find a *new* scaffold that still binds BCL-2.  Structural
#    similarity to the reference is the hardest part, so it gets the highest
#    weight.  QED/SA are secondary.
#
#  de_novo_design (Hard / Mpro):
#    Agent starts from nothing.  Drug-likeness (QED) and synthesisability (SA)
#    are paramount because similarity to the single reference Mpro inhibitor
#    is hard to achieve by chance.  A small novelty bonus rewards agents that
#    find intermediate-similarity (0.3–0.6) scaffolds rather than just copying
#    the reference.

TASK_WEIGHTS = {
    "EGFR": {"qed": 0.35, "sa": 0.30, "tanimoto": 0.35},
    "BCL-2": {"qed": 0.25, "sa": 0.25, "tanimoto": 0.50},
    "Mpro":  {"qed": 0.40, "sa": 0.35, "tanimoto": 0.25},
}

# Lipinski bonus weight applied uniformly
LIPINSKI_BONUS_WEIGHT = 0.05


# ---------------------------------------------------------------------------
# 7. Raw Molecule Evaluation
# ---------------------------------------------------------------------------

def evaluate_molecule(smiles: str, target_name: str) -> dict:
    """
    Runs all scoring functions and returns a metrics dictionary.
    Returns {'error': ..., 'is_valid': False} for unparseable SMILES.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": "Invalid SMILES string", "is_valid": False}

    qed = get_qed_score(mol)
    sa_norm = get_sa_score_normalized(mol)
    tanimoto = get_tanimoto_similarity(mol, target_name)
    pains_penalty = get_pains_penalty(mol)
    lipinski = get_lipinski_score(mol)

    return {
        "target": target_name,
        "qed": qed,
        "sa_score_normalized": sa_norm,
        "tanimoto_similarity": tanimoto,
        "lipinski_score": lipinski,
        "pains_pass": 1.0 if pains_penalty == 0.0 else 0.0,
        "pains_penalty": pains_penalty,
        "is_valid": True,
    }


# ---------------------------------------------------------------------------
# 8. Final Score Calculation
# ---------------------------------------------------------------------------

def calculate_final_score(
    smiles: str,
    target_name: str,
    previous_best: float = 0.0,
) -> dict:
    """
    Computes a deterministic scalar reward in [0.0, 1.0].

    Reward = weighted_sum(qed, sa_norm, tanimoto)
           + lipinski_bonus
           + pains_penalty          (additive, ≤ 0)
           + novelty_bonus          (Mpro only)
    All clamped to [0.0, 1.0].

    Invalid SMILES → 0.0 (spec-compliant; NOT -0.1).

    Args:
        smiles:        Candidate molecule SMILES string.
        target_name:   One of "EGFR", "BCL-2", "Mpro".
        previous_best: Best score seen so far in this episode.
                       Used to compute delta (improvement signal).

    Returns dict:
        score        – final reward scalar [0.0, 1.0]
        is_valid     – whether SMILES parsed successfully
        metrics      – individual component scores
        delta        – improvement over previous_best (≥ 0.0)
    """
    metrics = evaluate_molecule(smiles, target_name)

    if not metrics.get("is_valid", False):
        return {
            "score": 0.0,          # strictly in-spec (not -0.1)
            "is_valid": False,
            "metrics": {},
            "delta": 0.0,
        }

    weights = TASK_WEIGHTS.get(
        target_name, {"qed": 0.334, "sa": 0.333, "tanimoto": 0.333}
    )

    # Weighted component sum
    weighted = (
        weights["qed"]      * metrics["qed"] +
        weights["sa"]       * metrics["sa_score_normalized"] +
        weights["tanimoto"] * metrics["tanimoto_similarity"]
    )

    # Small Lipinski bonus (max +0.05)
    lipinski_bonus = LIPINSKI_BONUS_WEIGHT * metrics["lipinski_score"]

    # Additive PAINS penalty (0.0 or -0.20)
    pains_penalty = metrics["pains_penalty"]

    # Novelty bonus for de novo design:
    # Intermediate similarity (0.3–0.6) suggests a genuinely novel scaffold
    # that is still mechanistically related.  Very high similarity (> 0.9)
    # means the agent is copying the reference rather than discovering.
    novelty_bonus = 0.0
    if target_name == "Mpro":
        t = metrics["tanimoto_similarity"]
        if 0.3 <= t <= 0.6:
            novelty_bonus = +0.05   # sweet-spot scaffold
        elif t > 0.9:
            novelty_bonus = -0.05   # penalise copying reference

    raw = weighted + lipinski_bonus + pains_penalty + novelty_bonus
    final_score = float(max(0.0, min(1.0, raw)))

    # Delta: how much did this step improve over the episode best?
    delta = float(max(0.0, final_score - previous_best))

    return {
        "score": final_score,
        "is_valid": True,
        "metrics": metrics,
        "delta": delta,
    }


# ---------------------------------------------------------------------------
# 9. Task Definitions
# ---------------------------------------------------------------------------

class TaskConfig:
    def __init__(
        self,
        task_id: str,
        max_attempts: int,
        success_threshold: float,
        start_smiles: str | None,
        target_name: str,
    ):
        self.task_id = task_id
        self.max_attempts = max_attempts
        self.success_threshold = success_threshold
        self.start_smiles = start_smiles
        self.target_name = target_name


TASKS = {
    # Easy: agent starts from a known EGFR inhibitor; just needs fine-tuning
    "lead_optimization": TaskConfig(
        task_id="lead_optimization",
        max_attempts=50,
        success_threshold=0.75,        # achievable with modest improvement
        start_smiles=CHEMBL_ACTIVES["EGFR"],
        target_name="EGFR",
    ),
    # Medium: must find a new BCL-2 scaffold; high tanimoto weight makes it harder
    "scaffold_hopping": TaskConfig(
        task_id="scaffold_hopping",
        max_attempts=100,
        success_threshold=0.65,        # tanimoto-dominant; hard to hit without structure
        start_smiles=CHEMBL_ACTIVES["BCL-2"],
        target_name="BCL-2",
    ),
    # Hard: start from scratch; needs QED + SA + novelty all at once
    "de_novo_design": TaskConfig(
        task_id="de_novo_design",
        max_attempts=200,
        success_threshold=0.78,        # hard but not impossible for frontier models
        start_smiles=None,
        target_name="Mpro",
    ),
}