"""
Drug Discovery OpenEnv — Baseline Inference Agent
==================================================
Runs a language model against all three tasks (lead_optimization,
scaffold_hopping, de_novo_design) and emits structured [START]/[STEP]/[END]
logs required by the hackathon evaluator.

Environment variables
---------------------
API_BASE_URL   OpenAI-compatible endpoint  (default: HF Inference Router)
MODEL_NAME     Model identifier            (default: Qwen/Qwen2.5-72B-Instruct)
HF_TOKEN       Hugging Face API key        (used as api_key)
OPENAI_API_KEY Alternative API key        (falls back to HF_TOKEN)
ENV_URL        Drug Discovery env URL      (default: http://localhost:7860)
BENCHMARK      Benchmark label             (default: drug-discovery)
"""

import os
import re
import json
import time
import requests
from typing import List, Optional
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE_URL  = os.getenv("API_BASE_URL", "https://router.huggingface.co/v1")
MODEL_NAME    = os.getenv("MODEL_NAME", "Qwen/Qwen2.5-72B-Instruct")
HF_TOKEN      = os.getenv("HF_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or HF_TOKEN
ENV_URL       = os.getenv("ENV_URL", "http://localhost:7860")
BENCHMARK     = os.getenv("BENCHMARK", "drug-discovery")

MAX_RETRIES_PER_STEP = 3   # retries if LLM returns invalid SMILES
HISTORY_WINDOW       = 5   # how many past (smiles, score) pairs to include in prompt

client = OpenAI(
    api_key=OPENAI_API_KEY if OPENAI_API_KEY else "dummy_key",
    base_url=API_BASE_URL if API_BASE_URL else None,
)


# ---------------------------------------------------------------------------
# Structured Logging  (hackathon-required format)
# ---------------------------------------------------------------------------

def log_start(task: str, env: str, model: str) -> None:
    print(f"[START] task={task} env={env} model={model}", flush=True)


def log_step(
    step: int,
    action: str,
    reward: float,
    done: bool,
    error: Optional[str],
) -> None:
    error_val = error if error else "null"
    done_val  = str(done).lower()
    # Truncate long SMILES to keep logs readable without breaking the format
    action_log = action if len(action) <= 80 else action[:77] + "..."
    print(
        f"[STEP] step={step} action={action_log} "
        f"reward={reward:.4f} done={done_val} error={error_val}",
        flush=True,
    )


def log_end(
    success: bool,
    steps: int,
    score: float,
    rewards: List[float],
) -> None:
    rewards_str = ",".join(f"{r:.4f}" for r in rewards)
    print(
        f"[END] success={str(success).lower()} steps={steps} "
        f"score={score:.4f} rewards={rewards_str}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# SMILES Extraction Helper
# ---------------------------------------------------------------------------

# Matches a SMILES string: starts with an atom symbol or ring-open bracket
_SMILES_RE = re.compile(
    r"(?:```[a-zA-Z]*\s*)?"           # optional fenced code block
    r"([A-Za-z\[\]()=#@+\-\\\/\.%0-9]{6,})"  # SMILES characters (≥6 chars)
)


def extract_smiles(text: str) -> Optional[str]:
    """
    Pull the first plausible SMILES token from arbitrary LLM output.

    Strategy:
    1. Strip markdown fences.
    2. Try regex for a SMILES-like token.
    3. Fall back to the longest whitespace-split token.
    """
    if not text:
        return None

    # Remove code fences
    cleaned = re.sub(r"```[a-zA-Z]*", "", text).replace("`", "").strip()

    match = _SMILES_RE.search(cleaned)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate

    # Fallback: longest token (SMILES are dense strings without spaces)
    tokens = cleaned.split()
    if tokens:
        return max(tokens, key=len)

    return None


# ---------------------------------------------------------------------------
# LLM-Powered SMILES Generation
# ---------------------------------------------------------------------------

TASK_GUIDANCE = {
    "lead_optimization": (
        "You are optimising an existing EGFR inhibitor. Make small structural changes "
        "(e.g. swap substituents, adjust ring systems) that improve QED, synthesisability, "
        "and structural similarity to the EGFR reference compound. Avoid PAINS substructures."
    ),
    "scaffold_hopping": (
        "You are performing scaffold hopping for BCL-2 inhibition. Keep the key pharmacophore "
        "features (sulfonamide, chlorophenyl, piperazine) but replace the central scaffold "
        "with a different ring system. The score weights tanimoto similarity heavily (0.50)."
    ),
    "de_novo_design": (
        "You are designing a novel Mpro (SARS-CoV-2 main protease) inhibitor from scratch. "
        "Aim for: MW ≤ 500, LogP ≤ 5, QED > 0.6, SA score < 4 (easy to synthesise). "
        "A Tanimoto similarity of 0.3–0.6 to the reference Nirmatrelvir earns a novelty bonus. "
        "Do NOT copy the reference molecule — design something new."
    ),
}


def get_next_smiles(
    task_config: dict,
    history: List[dict],
    attempt: int = 1,
) -> str:
    """
    Calls the LLM and returns a SMILES string.

    Args:
        task_config: Task metadata from /tasks endpoint.
        history:     List of {'smiles': str, 'score': float, 'metrics': dict}
                     for the last HISTORY_WINDOW steps.
        attempt:     Retry count (1-indexed). Increases temperature on retries.
    """
    task_id = task_config["task_id"]
    target  = task_config["target_name"]

    guidance = TASK_GUIDANCE.get(task_id, "Design a drug-like molecule.")

    system_prompt = (
        f"You are an expert medicinal chemist AI specialising in {target} inhibitor design.\n"
        f"{guidance}\n\n"
        "OUTPUT FORMAT: Return ONLY a single valid SMILES string. "
        "No explanations, no markdown, no numbering. Just the raw SMILES."
    )

    # Build user prompt with history for context
    user_lines = [f"Target: {target}"]
    if task_config.get("start_smiles"):
        user_lines.append(f"Reference SMILES: {task_config['start_smiles']}")

    if history:
        user_lines.append("\nRecent candidates (worst → best):")
        for h in history[-HISTORY_WINDOW:]:
            metrics_summary = ""
            if h.get("metrics"):
                m = h["metrics"]
                metrics_summary = (
                    f"  [QED={m.get('qed', 0):.3f}, "
                    f"SA_norm={m.get('sa_score_normalized', 0):.3f}, "
                    f"Tan={m.get('tanimoto_similarity', 0):.3f}, "
                    f"PAINS={'pass' if m.get('pains_pass', 0) == 1.0 else 'FAIL'}]"
                )
            user_lines.append(
                f"  Score={h['score']:.4f}  SMILES={h['smiles']}{metrics_summary}"
            )

        best = max(history, key=lambda x: x["score"])
        user_lines.append(
            f"\nBest so far: {best['smiles']} (score={best['score']:.4f})"
        )
        user_lines.append(
            "Improve on this. Generate a structurally distinct candidate with a higher score."
        )
    else:
        user_lines.append("\nNo prior candidates. Generate your first proposal.")

    user_lines.append("\nReturn only the SMILES string:")

    # Increase temperature on retries to escape the same invalid output
    temperature = min(0.7 + 0.15 * (attempt - 1), 1.2)

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": "\n".join(user_lines)},
            ],
            temperature=temperature,
            max_tokens=200,
        )
        raw_output = response.choices[0].message.content.strip()
        smiles = extract_smiles(raw_output)
        return smiles if smiles else "C1=CC=CC=C1"   # benzene as last-resort fallback

    except Exception as e:
        print(f"[WARN] LLM call failed (attempt {attempt}): {e}", flush=True)
        return "C1=CC=CC=C1"


# ---------------------------------------------------------------------------
# Main Agent Loop
# ---------------------------------------------------------------------------

def run_task(task_id: str, task_config: dict) -> None:
    """Run a single task episode and emit structured logs."""
    # Reset episode
    res = requests.post(f"{ENV_URL}/reset", json={"task_id": task_id}, timeout=30)
    if res.status_code != 200:
        print(f"[WARN] Failed to reset task {task_id}: {res.text}", flush=True)
        return

    reset_data = res.json()

    log_start(task=task_id, env=BENCHMARK, model=MODEL_NAME)

    history: List[dict]  = []
    rewards_list: List[float] = []
    steps_taken  = 0
    success      = False
    final_score  = 0.0
    done         = False

    # Pre-populate history with the reference molecule metrics (from reset)
    initial_obs = reset_data.get("initial_observation", {}).get("metrics", {})
    if initial_obs and task_config.get("start_smiles"):
        history.append({
            "smiles":  task_config["start_smiles"],
            "score":   0.0,         # not yet scored by the grader
            "metrics": initial_obs,
        })

    while not done:
        steps_taken += 1
        smiles = None
        step_data = None
        err_msg = None

        # Retry loop: keep asking LLM until we get a valid molecule or exhaust retries
        for attempt in range(1, MAX_RETRIES_PER_STEP + 1):
            smiles = get_next_smiles(task_config, history, attempt=attempt)

            step_res = requests.post(
                f"{ENV_URL}/step",
                json={"task_id": task_id, "smiles": smiles},
                timeout=30,
            )
            if step_res.status_code != 200:
                print(f"[WARN] /step error: {step_res.text}", flush=True)
                err_msg = "server_error"
                break

            step_data = step_res.json()
            if step_data["reward"]["is_valid"]:
                err_msg = None
                break
            else:
                err_msg = "invalid_smiles"
                # Don't count invalid attempts as wasted steps in history
                if attempt < MAX_RETRIES_PER_STEP:
                    continue

        if step_data is None:
            # Server error — bail out of this task
            break

        reward_info = step_data["reward"]
        state_info  = step_data["state"]
        obs_metrics = step_data["observation"]["metrics"]

        r_val   = float(reward_info["score"])
        is_done = bool(state_info["done"])

        rewards_list.append(r_val)
        done    = is_done
        success = bool(reward_info["is_success"])
        final_score = float(state_info["current_best_score"])

        log_step(
            step=steps_taken,
            action=smiles,
            reward=r_val,
            done=done,
            error=err_msg,
        )

        # Update history for next step's context
        if reward_info["is_valid"]:
            history.append({
                "smiles":  smiles,
                "score":   r_val,
                "metrics": obs_metrics,
            })
            # Keep history bounded
            if len(history) > HISTORY_WINDOW * 2:
                # Retain the best + most recent
                history = sorted(history, key=lambda x: x["score"], reverse=True)[:HISTORY_WINDOW]

    log_end(
        success=success,
        steps=steps_taken,
        score=final_score,
        rewards=rewards_list,
    )


def run_agent() -> None:
    """Fetch all tasks and run the agent against each one sequentially."""
    # Verify environment is reachable
    try:
        health = requests.get(f"{ENV_URL}/health", timeout=10)
        health.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Cannot reach environment at {ENV_URL}: {e}", flush=True)
        return

    # Fetch task catalogue
    try:
        res = requests.get(f"{ENV_URL}/tasks", timeout=10)
        res.raise_for_status()
        tasks = res.json()
    except Exception as e:
        print(f"[ERROR] Failed to fetch tasks from {ENV_URL}: {e}", flush=True)
        return

    for task_id, task_config in tasks.items():
        run_task(task_id, task_config)
        time.sleep(1)   # brief pause between tasks


if __name__ == "__main__":
    run_agent()