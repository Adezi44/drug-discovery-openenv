import requests

URL = "http://127.0.0.1:7860"

def test_task(task_id, smiles):
    # Reset Environment
    requests.post(f"{URL}/reset", json={"task_id": task_id})
    
    # Send identical steps sequentially to ensure the model responds deterministically
    res1 = requests.post(f"{URL}/step", json={"task_id": task_id, "smiles": smiles})
    if res1.status_code != 200:
        print("ERROR:", res1.text)
    res1 = res1.json()
    res2 = requests.post(f"{URL}/step", json={"task_id": task_id, "smiles": smiles}).json()
    
    assert res1["reward"]["score"] == res2["reward"]["score"], f"[{task_id}] Score is not deterministic!"
    print(f"[{task_id}] Score determinism verified: {res1['reward']['score']}")

def test_invalid():
    requests.post(f"{URL}/reset", json={"task_id": "lead_optimization"})
    res = requests.post(f"{URL}/step", json={"task_id": "lead_optimization", "smiles": "INVALID_SMILES123"}).json()
    assert res["reward"]["score"] == -0.1, f"Invalid SMILES returned {res['reward']['score']} instead of -0.1"
    print("[Invalid SMILES] Correctly returning score -0.1")

if __name__ == "__main__":
    test_task("lead_optimization", "CC1=CC=CC=C1")
    test_task("scaffold_hopping", "CC(=O)OC1=CC=CC=C1C(=O)O")
    test_task("de_novo_design", "CCO")
    test_invalid()
    print("✅ All local tests PASSED.")
