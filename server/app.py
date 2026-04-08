import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional
import uvicorn

from envs.drug_discovery.env import TASKS, calculate_final_score

app = FastAPI(title="OpenEnv - Drug Discovery API")

# --- Pydantic v2 Models ---
class Action(BaseModel):
    task_id: str
    smiles: str

class Reward(BaseModel):
    score: float
    is_success: bool
    is_valid: bool

class Observation(BaseModel):
    metrics: Dict[str, Any]

class State(BaseModel):
    task_id: str
    attempts: int
    max_attempts: int
    done: bool
    current_best_score: float

class ResetRequest(BaseModel):
    task_id: str

class TaskConfigResponse(BaseModel):
    task_id: str
    max_attempts: int
    success_threshold: float
    start_smiles: Optional[str]
    target_name: str

class StepResponse(BaseModel):
    observation: Observation
    reward: Reward
    state: State

# --- In-Memory Episode Trackers ---
class EpisodeState:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.attempts = 0
        self.done = False
        self.current_best_score = 0.0

active_episodes: Dict[str, EpisodeState] = {}


# --- Endpoints ---

@app.post("/reset", response_model=State)
def reset_env(req: ResetRequest):
    if req.task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found")
    
    state = EpisodeState(req.task_id)
    active_episodes[req.task_id] = state
    config = TASKS[req.task_id]
    
    return State(
        task_id=state.task_id,
        attempts=state.attempts,
        max_attempts=config.max_attempts,
        done=state.done,
        current_best_score=state.current_best_score
    )

@app.post("/step", response_model=StepResponse)
def step_env(action: Action):
    if action.task_id not in active_episodes:
        raise HTTPException(status_code=400, detail="Task has not been reset or initialized.")
    if action.task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found configuration.")

    state = active_episodes[action.task_id]
    config = TASKS[action.task_id]

    if state.done:
        raise HTTPException(status_code=400, detail="Episode already completed. Please reset.")

    state.attempts += 1

    # Evaluate the molecule based on task target
    result = calculate_final_score(action.smiles, config.target_name)
    current_score = result["score"]

    if current_score > state.current_best_score:
        state.current_best_score = current_score

    # Check termination conditions: Threshold met or ran out of attempts!
    is_success = current_score >= config.success_threshold
    
    if is_success or state.attempts >= config.max_attempts:
        state.done = True

    return StepResponse(
        observation=Observation(metrics=result["metrics"]),
        reward=Reward(score=current_score, is_success=is_success, is_valid=result["is_valid"]),
        state=State(
            task_id=state.task_id,
            attempts=state.attempts,
            max_attempts=config.max_attempts,
            done=state.done,
            current_best_score=state.current_best_score
        )
    )

@app.get("/state", response_model=State)
def get_state(task_id: str):
    if task_id not in active_episodes:
        raise HTTPException(status_code=404, detail="Episode state not found. Please reset first.")
    
    state = active_episodes[task_id]
    config = TASKS[task_id]
    
    return State(
        task_id=state.task_id,
        attempts=state.attempts,
        max_attempts=config.max_attempts,
        done=state.done,
        current_best_score=state.current_best_score
    )

@app.get("/tasks", response_model=Dict[str, TaskConfigResponse])
def get_tasks():
    response = {}
    for task_id, cfg in TASKS.items():
        response[task_id] = TaskConfigResponse(
            task_id=cfg.task_id,
            max_attempts=cfg.max_attempts,
            success_threshold=cfg.success_threshold,
            start_smiles=cfg.start_smiles,
            target_name=cfg.target_name
        )
    return response

def main():
    uvicorn.run("server.app:app", host="0.0.0.0", port=7860)

if __name__ == "__main__":
    main()

