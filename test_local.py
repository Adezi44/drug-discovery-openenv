"""
test_local.py — Local integration tests for Drug Discovery OpenEnv
===================================================================
Run with:   python test_local.py
Requires:   server running at URL (default http://127.0.0.1:7860)
"""

import sys
import requests

URL = "http://127.0.0.1:7860"
PASS = "✅"
FAIL = "❌"

errors = []


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        msg = f"{FAIL} {label}" + (f" — {detail}" if detail else "")
        print(msg)
        errors.append(msg)


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------

def test_health():
    res = requests.get(f"{URL}/health")
    check(res.status_code == 200, "Health endpoint returns 200")
    data = res.json()
    check(data.get("status") == "ok", "Health payload has status=ok", str(data))


# ---------------------------------------------------------------------------
# 2. Task listing
# ---------------------------------------------------------------------------

def test_tasks():
    res = requests.get(f"{URL}/tasks")
    check(res.status_code == 200, "GET /tasks returns 200")
    tasks = res.json()
    check(len(tasks) >= 3, f"At least 3 tasks defined (got {len(tasks)})")
    for tid in ["lead_optimization", "scaffold_hopping", "de_novo_design"]:
        check(tid in tasks, f"Task '{tid}' exists")


# ---------------------------------------------------------------------------
# 3. Score determinism
# ---------------------------------------------------------------------------

def test_determinism(task_id: str, smiles: str):
    requests.post(f"{URL}/reset", json={"task_id": task_id})
    r1 = requests.post(f"{URL}/step", json={"task_id": task_id, "smiles": smiles}).json()
    # Reset again so attempt counter doesn't interfere
    requests.post(f"{URL}/reset", json={"task_id": task_id})
    r2 = requests.post(f"{URL}/step", json={"task_id": task_id, "smiles": smiles}).json()

    s1 = r1["reward"]["score"]
    s2 = r2["reward"]["score"]
    check(s1 == s2, f"[{task_id}] Score is deterministic ({s1:.6f})")


# ---------------------------------------------------------------------------
# 4. Score bounds: all rewards must be in [0.0, 1.0]
# ---------------------------------------------------------------------------

def test_score_bounds(task_id: str, smiles: str):
    requests.post(f"{URL}/reset", json={"task_id": task_id})
    res = requests.post(
        f"{URL}/step", json={"task_id": task_id, "smiles": smiles}
    ).json()
    score = res["reward"]["score"]
    check(
        0.0 <= score <= 1.0,
        f"[{task_id}] Score in [0.0, 1.0] (got {score:.6f})",
    )


# ---------------------------------------------------------------------------
# 5. Invalid SMILES must return 0.0  (NOT -0.1 — spec requires [0.0, 1.0])
# ---------------------------------------------------------------------------

def test_invalid_smiles():
    requests.post(f"{URL}/reset", json={"task_id": "lead_optimization"})
    res = requests.post(
        f"{URL}/step",
        json={"task_id": "lead_optimization", "smiles": "INVALID_SMILES_XYZ!!!"},
    ).json()
    score    = res["reward"]["score"]
    is_valid = res["reward"]["is_valid"]
    check(score == 0.0,    f"Invalid SMILES → score=0.0 (got {score})")
    check(not is_valid,    "Invalid SMILES → is_valid=False")


# ---------------------------------------------------------------------------
# 6. Reset produces a clean state
# ---------------------------------------------------------------------------

def test_reset_clean_state():
    # Do some steps, then reset, verify attempts=0
    task_id = "lead_optimization"
    requests.post(f"{URL}/reset", json={"task_id": task_id})
    requests.post(f"{URL}/step", json={"task_id": task_id, "smiles": "CCO"})
    requests.post(f"{URL}/step", json={"task_id": task_id, "smiles": "CCN"})

    reset_res = requests.post(f"{URL}/reset", json={"task_id": task_id}).json()
    state = reset_res["state"]
    check(state["attempts"] == 0,            "Reset clears attempt counter")
    check(state["current_best_score"] == 0.0, "Reset clears best score")
    check(not state["done"],                 "Reset clears done flag")


# ---------------------------------------------------------------------------
# 7. Episode boundary: done after max_attempts
# ---------------------------------------------------------------------------

def test_episode_boundary():
    task_id = "lead_optimization"
    # Use a very simple molecule and hammer through to done state
    requests.post(f"{URL}/reset", json={"task_id": task_id})
    tasks = requests.get(f"{URL}/tasks").json()
    max_att = tasks[task_id]["max_attempts"]

    final_state = None
    for i in range(max_att + 2):   # go a couple over
        r = requests.post(
            f"{URL}/step",
            json={"task_id": task_id, "smiles": "CCO"},
        )
        if r.status_code == 200:
            final_state = r.json()["state"]
        else:
            break   # expect 400 "already complete" after done=True

    if final_state:
        check(final_state["done"], "Episode marks done after max_attempts")
        check(
            final_state["attempts"] <= max_att,
            f"Attempts capped at max ({final_state['attempts']} ≤ {max_att})",
        )


# ---------------------------------------------------------------------------
# 8. Positive reward for a drug-like molecule
# ---------------------------------------------------------------------------

def test_positive_reward():
    # Aspirin — simple, drug-like, valid SMILES
    aspirin = "CC(=O)Oc1ccccc1C(=O)O"
    requests.post(f"{URL}/reset", json={"task_id": "lead_optimization"})
    res = requests.post(
        f"{URL}/step",
        json={"task_id": "lead_optimization", "smiles": aspirin},
    ).json()
    score = res["reward"]["score"]
    check(score > 0.0, f"Drug-like molecule (aspirin) scores > 0.0 (got {score:.4f})")
    check(res["reward"]["is_valid"], "Aspirin is_valid=True")


# ---------------------------------------------------------------------------
# 9. Delta is non-negative
# ---------------------------------------------------------------------------

def test_delta_non_negative():
    task_id = "lead_optimization"
    requests.post(f"{URL}/reset", json={"task_id": task_id})
    for smiles in ["CCO", "CC(=O)Oc1ccccc1C(=O)O", "c1ccccc1"]:
        res = requests.post(
            f"{URL}/step", json={"task_id": task_id, "smiles": smiles}
        ).json()
        delta = res["reward"].get("delta", -1)
        check(delta >= 0.0, f"Delta ≥ 0.0 for '{smiles}' (got {delta:.4f})")


# ---------------------------------------------------------------------------
# 10. PAINS Penalty Verification
# ---------------------------------------------------------------------------

def test_pains_penalty():
    # Rhodamine B — classic PAINS hit
    pains_smiles = "CCN(CC)c1ccc2c(c1)OC1cc(=[N+](CC)CC)ccc1C2c1ccccc1C(=O)O"
    task_id = "lead_optimization"
    requests.post(f"{URL}/reset", json={"task_id": task_id})
    res = requests.post(f"{URL}/step", json={"task_id": task_id, "smiles": pains_smiles}).json()
    metrics = res["observation"]["metrics"]
    check(metrics.get("pains_pass") == 0.0, "Rhodamine B correctly fails PAINS (pains_pass=0.0)")
    check(metrics.get("pains_penalty") <= -0.14, f"Rhodamine B receives heavy PAINS penalty (got {metrics.get('pains_penalty')})")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("Drug Discovery OpenEnv — Local Integration Tests")
    print("=" * 60)

    test_health()
    test_tasks()

    print("\n--- Determinism ---")
    test_determinism("lead_optimization",  "CC1=CC=CC=C1")
    test_determinism("scaffold_hopping",   "CC(=O)OC1=CC=CC=C1C(=O)O")
    test_determinism("de_novo_design",     "CCO")

    print("\n--- Score Bounds ---")
    test_score_bounds("lead_optimization", "CC1=CC=CC=C1")
    test_score_bounds("scaffold_hopping",  "CC(=O)OC1=CC=CC=C1C(=O)O")
    test_score_bounds("de_novo_design",    "CCO")

    print("\n--- Invalid SMILES ---")
    test_invalid_smiles()

    print("\n--- Reset State ---")
    test_reset_clean_state()

    print("\n--- Episode Boundary ---")
    test_episode_boundary()

    print("\n--- Positive Reward ---")
    test_positive_reward()

    print("\n--- Delta Signal ---")
    test_delta_non_negative()

    print("\n--- PAINS Penalty ---")
    test_pains_penalty()

    print("\n" + "=" * 60)
    if errors:
        print(f"RESULT: {len(errors)} test(s) FAILED")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("RESULT: All tests PASSED ✅")