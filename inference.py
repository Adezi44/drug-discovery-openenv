import os
import json
import sys
import requests
from openai import OpenAI

# Step 6: Baseline Inference Agent

# Read environment variables
API_BASE_URL = os.getenv("API_BASE_URL", "https://router.huggingface.co/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
HF_TOKEN = os.getenv("HF_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or HF_TOKEN
LOCAL_IMAGE_NAME = os.getenv("LOCAL_IMAGE_NAME")
ENV_URL = os.getenv("ENV_URL", "http://localhost:8000")
BENCHMARK = os.getenv("BENCHMARK", "drug-discovery")

# Initialize OpenAI Client (Compatible with OpenAI/vLLM endpoints natively based dynamically on env variables)
client = OpenAI(
    api_key=OPENAI_API_KEY if OPENAI_API_KEY else "dummy_key", 
    base_url=API_BASE_URL if API_BASE_URL else None
)

from typing import List, Optional

def log_start(task: str, env: str, model: str) -> None:
    print(f"[START] task={task} env={env} model={model}", flush=True)

def log_step(step: int, action: str, reward: float, done: bool, error: Optional[str]) -> None:
    error_val = error if error else "null"
    done_val = str(done).lower()
    print(
        f"[STEP] step={step} action={action} reward={reward:.2f} done={done_val} error={error_val}",
        flush=True,
    )

def log_end(success: bool, steps: int, score: float, rewards: List[float]) -> None:
    rewards_str = ",".join(f"{r:.2f}" for r in rewards)
    print(f"[END] success={str(success).lower()} steps={steps} score={score:.3f} rewards={rewards_str}", flush=True)

def get_next_smiles(task_config, last_smiles, last_score, observation):
    """Hits the OpenAI completion API and requests a syntactically valid SMILES string."""
    system_prompt = (
        "You are an AI optimized for drug discovery. "
        "Your task is to generate exactly ONE valid SMILES string. "
        "Do not provide explanations, text, or markdown blocks. Just output the raw string."
    )
    
    prompt = f"Task: {task_config['task_id']}\nTarget: {task_config['target_name']}\n"
    
    if task_config['start_smiles']:
        prompt += f"Starting reference SMILES: {task_config['start_smiles']}\n"
        
    if last_smiles:
        prompt += f"Previous candidate: {last_smiles} (Score: {last_score})\n"
        if observation and "metrics" in observation:
             prompt += f"Metrics from last candidate: {json.dumps(observation['metrics'])}\n"
             
    prompt += "Improve the score (target is 1.0). Return the next candidate SMILES string:"
    
    try:
        res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=150
        )
        smiles = res.choices[0].message.content.strip()
        # Clean any accidental markdown blocks that bleed through
        if smiles.startswith("```"):
            smiles = smiles.replace("`", "").replace("smiles", "").strip()
        return smiles.split()[-1] if smiles else "C"
        
    except Exception as e:
        # Provide a fallback valid SMILES so our runner loop won't crash independently
        return "C1=CC=CC=C1"

def run_agent():
    # 1. Fetch available tasks from the Environment API
    try:
        res = requests.get(f"{ENV_URL}/tasks")
        res.raise_for_status()
        tasks = res.json()
    except Exception as e:
        print(f"Error connecting to Env at {ENV_URL}: {e}")
        return

    # 2. Iterate sequentially through each task
    for task_id, task_config in tasks.items():
        # Reset the environment specifically for the nested task
        res = requests.post(f"{ENV_URL}/reset", json={"task_id": task_id})
        if res.status_code != 200:
            print(f"Failed to reset task {task_id}")
            continue
            
        state = res.json()
        log_start(task=task_id, env=BENCHMARK, model=MODEL_NAME)

        done = False
        last_smiles = None
        last_score = 0.0
        last_obs = None
        
        rewards_list = []
        steps_taken = 0
        success = False
        final_score = 0.0
        
        while not done:
            steps_taken += 1
            # Send context to AI and receive actionable SMILES string
            smiles = get_next_smiles(task_config, last_smiles, last_score, last_obs)
            
            # Step the Local Environment Simulator
            step_res = requests.post(f"{ENV_URL}/step", json={
                "task_id": task_id,
                "smiles": smiles
            })
            
            if step_res.status_code != 200:
                print(f"Failed to step task {task_id}: {step_res.text}")
                break
                
            step_data = step_res.json()
            
            obs = step_data["observation"]
            reward = step_data["reward"]
            state = step_data["state"]

            # Log exactly as specified
            r_val = float(reward["score"])
            is_valid = reward["is_valid"]
            err_msg = "invalid_smiles" if not is_valid else None
            
            rewards_list.append(r_val)
            done = state["done"]
            
            log_step(step=steps_taken, action=smiles, reward=r_val, done=done, error=err_msg)
            
            last_smiles = smiles
            last_score = r_val
            last_obs = obs
            
            final_score = state["current_best_score"]
            success = reward["is_success"]

        log_end(success=success, steps=steps_taken, score=final_score, rewards=rewards_list)

if __name__ == "__main__":
    run_agent()
