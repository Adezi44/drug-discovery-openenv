import os
import sys
import logging

from rdkit import Chem
from rdkit.Chem import QED
from rdkit.Chem import FilterCatalog
from rdkit.Chem import AllChem
from rdkit import DataStructs
from rdkit.Chem import RDConfig

logger = logging.getLogger(__name__)

# --- 1. QED Score ---
def get_qed_score(mol):
    """Calculates the Quantitative Estimate of Drug-likeness (QED)."""
    if mol is None: return 0.0
    try:
        return QED.qed(mol)
    except Exception as e:
        logger.error(f"Error calculating QED: {e}")
        return 0.0


# --- 2. SA Score (Ertl-Schuffenhauer) ---
try:
    from . import sascorer
except ImportError:
    try:
        import sascorer
    except ImportError:
        sascorer = None
        logger.warning("sascorer not found locally. SA scores will fallback to 0.0")

def get_sa_score(mol):
    """Calculates the Synthetic Accessibility (SA) score."""
    if mol is None or sascorer is None:
        return 0.0
    try:
        return sascorer.calculateScore(mol)
    except Exception as e:
        logger.error(f"Error calculating SA score: {e}")
        return 0.0


# --- 3. PAINS Filter ---
_pains_catalog = None

def _get_pains_catalog():
    global _pains_catalog
    if _pains_catalog is None:
        params = FilterCatalog.FilterCatalogParams()
        params.AddCatalog(FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS)
        _pains_catalog = FilterCatalog.FilterCatalog(params)
    return _pains_catalog

def get_pains_filter_score(mol):
    """Returns 1.0 if the molecule passes PAINS (no match), else 0.0."""
    if mol is None: return 0.0
    try:
        catalog = _get_pains_catalog()
        if catalog.HasMatch(mol):
            return 0.0  # Fails the filter
        return 1.0  # Passes
    except Exception as e:
        logger.error(f"Error assessing PAINS filter: {e}")
        return 0.0


# --- 4. Morgan + Tanimoto vs Hardcoded ChEMBL Actives ---
# Hardcoded canonical SMILES for representative target active compounds
CHEMBL_ACTIVES = {
    # Gefitinib for EGFR (CHEMBL203)
    "EGFR": "COC1=C(OCCCN2CCOCC2)C=C2C(NC3=CC=C(F)C(Cl)=C3)=NC=NC2=C1",
    # Venetoclax for BCL-2 (CHEMBL279)
    "BCL-2": "CC1(C)CCC(CN2CCN(c3ccc(C(=O)NS(=O)(=O)c4ccc(NCC5CCN(c6ccc(F)cc6)CC5)c([N+](=O)[O-])c4)cc3)CC2)=C(c2ccc(Cl)cc2)C1",
    # Nirmatrelvir proxy for Mpro (SARS-CoV-2 main protease)
    "Mpro": "CC1(C)C2C1C(C(=O)N1CC3C(CCC3)C1C(=O)NC(C#N)CC1CCNC1=O)NC2(=O)C(F)(F)F"
}

def get_tanimoto_similarity(mol, target_name):
    """Calculates Tanimoto similarity to a target's active compound."""
    if mol is None: return 0.0
    if target_name not in CHEMBL_ACTIVES:
        logger.warning(f"Target '{target_name}' not found. Returning 0.0")
        return 0.0

    active_smiles = CHEMBL_ACTIVES[target_name]
    active_mol = Chem.MolFromSmiles(active_smiles)
    if active_mol is None:
        return 0.0

    try:
        # Calculate Morgan Fingerprints (radius=2, 2048 bits)
        fp_mol = AllChem.GetMorganFingerprintAsBitVect(mol, 2, nBits=2048)
        fp_active = AllChem.GetMorganFingerprintAsBitVect(active_mol, 2, nBits=2048)

        return DataStructs.TanimotoSimilarity(fp_mol, fp_active)
    except Exception as e:
        logger.error(f"Error calculating Tanimoto similarity: {e}")
        return 0.0


# --- Aggregate Grader Evaluation ---
def evaluate_molecule(smiles: str, target_name: str) -> dict:
    """Evaluates a SMILES string against all scoring functions."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": "Invalid SMILES string"}

    return {
        "target": target_name,
        "qed": get_qed_score(mol),
        "sa_score": get_sa_score(mol),
        "pains_pass": get_pains_filter_score(mol),
        "tanimoto_similarity": get_tanimoto_similarity(mol, target_name)
    }

# --- 5. Task Definitions & State Management ---

class TaskConfig:
    def __init__(self, task_id: str, max_attempts: int, success_threshold: float, start_smiles: str = None, target_name: str = "EGFR"):
        self.task_id = task_id
        self.max_attempts = max_attempts
        self.success_threshold = success_threshold
        self.start_smiles = start_smiles
        self.target_name = target_name

TASKS = {
    "lead_optimization": TaskConfig(
        task_id="lead_optimization", 
        max_attempts=50, 
        success_threshold=0.8, 
        start_smiles=CHEMBL_ACTIVES["EGFR"], 
        target_name="EGFR"
    ),
    "scaffold_hopping": TaskConfig(
        task_id="scaffold_hopping", 
        max_attempts=100, 
        success_threshold=0.7, 
        start_smiles=CHEMBL_ACTIVES["BCL-2"], 
        target_name="BCL-2"
    ),
    "de_novo_design": TaskConfig(
        task_id="de_novo_design", 
        max_attempts=200, 
        success_threshold=0.85, 
        start_smiles=None, 
        target_name="Mpro"
    )
}

def calculate_final_score(smiles: str, target_name: str) -> dict:
    """Calculates a scalar reward score combined logically between 0.0 and 1.0"""
    metrics = evaluate_molecule(smiles, target_name)
    
    if "error" in metrics:
        return {"score": -0.1, "is_valid": False, "metrics": {}}

    qed = metrics["qed"]
    tanimoto = metrics["tanimoto_similarity"]
    
    # SA Score is usually 1 (easy) to 10 (hard). Normalizing so 1 -> 1.0 and 10 -> 0.0
    sa_score_raw = metrics["sa_score"]
    sa_score_norm = max(0.0, min(1.0, (10.0 - sa_score_raw) / 9.0)) if sa_score_raw > 0 else 0.0
    
    # Base score average between 0.0 and 1.0
    base_score = (qed + sa_score_norm + tanimoto) / 3.0
    
    # Multiplicative PAINS penalty: passes -> 1.0, fails -> 0.0
    final_score = base_score * metrics["pains_pass"]
    
    return {"score": final_score, "is_valid": True, "metrics": metrics}
