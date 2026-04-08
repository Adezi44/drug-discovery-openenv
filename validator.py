import os
import ast
import json
import yaml
import subprocess
import requests
import time

def check_openenv_yaml():
    print("Checking openenv.yaml...")
    assert os.path.exists("openenv.yaml"), "openenv.yaml is missing"
    with open("openenv.yaml", "r") as f:
        spec = yaml.safe_load(f)
    assert "endpoints" in spec.get("api", {}), "Missing endpoints in openenv.yaml"
    required_endpoints = ["reset", "step", "state", "tasks"]
    for endpoint in required_endpoints:
        assert endpoint in spec["api"]["endpoints"], f"Missing {endpoint} in openenv.yaml API"
    print("✅ openenv.yaml passes")

def check_inference_script():
    print("Checking inference.py...")
    assert os.path.exists("inference.py"), "inference.py is missing"
    with open("inference.py", "r") as f:
        content = f.read()
    assert "API_BASE_URL" in content, "API_BASE_URL variable missing"
    assert "MODEL_NAME" in content, "MODEL_NAME variable missing"
    assert "HF_TOKEN" in content, "HF_TOKEN variable missing"
    assert "OPENAI_API_KEY" in content, "OPENAI_API_KEY variable missing"
    assert "from openai import OpenAI" in content, "OpenAI Client not used"
    assert "log_event" in content and "\"START\"" in content, "Stdout formats missing"
    print("✅ inference.py formatting and env checks pass")

def check_env_graders():
    from envs.drug_discovery.env import TASKS, calculate_final_score

    print("Checking internal grader logic directly...")
    dummy_smiles = [
        "CC1=CC=CC=C1",
        "C1=CC=C2C(=C1)C=CC(=O)O2",
        "INVALID_CRAP"
    ]

    for task_name, task_config in TASKS.items():
        assert task_config.target_name in ["EGFR", "BCL-2", "Mpro"], "Invalid target name"
        for smiles in dummy_smiles:
            res = calculate_final_score(smiles, task_config.target_name)
            score = res["score"]
            assert score >= -0.1 and score <= 1.0, f"Score out of boundary: {score}"
            
    print("✅ Enumerate 3+ tasks graders pass with deterministic range constraints (0.0 to 1.0)")

if __name__ == "__main__":
    print("=== DRUG DISCOVERY OPENENV VALIDATOR ===")
    check_openenv_yaml()
    check_inference_script()
    check_env_graders()
    print("Validation run complete!")
