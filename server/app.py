import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
import uvicorn
from dotenv import load_dotenv

load_dotenv()

from envs.drug_discovery.env import TASKS, calculate_final_score, evaluate_molecule

app = FastAPI(
    title="Drug Discovery OpenEnv",
    description=(
        "A computational drug discovery benchmark where AI agents design molecules "
        "(as SMILES strings) to optimise QED, SA score, Tanimoto similarity, and "
        "PAINS compliance. Implements the full OpenEnv step/reset/state/tasks API."
    ),
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Pydantic v2 Models
# ---------------------------------------------------------------------------

class Action(BaseModel):
    task_id: str = Field(..., description="One of: lead_optimization, scaffold_hopping, de_novo_design")
    smiles: str = Field(..., description="A valid SMILES string representing the candidate molecule")


class Reward(BaseModel):
    score: float = Field(..., ge=0.0, le=1.0, description="Composite reward scalar in [0.0, 1.0]")
    delta: float = Field(..., ge=0.0, description="Improvement over the episode's previous best score")
    is_success: bool = Field(..., description="True if score >= task success_threshold")
    is_valid: bool = Field(..., description="True if the submitted SMILES was chemically valid")


class Observation(BaseModel):
    metrics: Dict[str, Any] = Field(
        ...,
        description=(
            "Chemical metrics: qed, sa_score_normalized, tanimoto_similarity, "
            "lipinski_score, pains_pass (1.0=pass), pains_penalty (0.0 or -0.15)"
        ),
    )


class State(BaseModel):
    task_id: str
    attempts: int
    max_attempts: int
    done: bool
    current_best_score: float = Field(..., ge=0.0, le=1.0)


class ResetRequest(BaseModel):
    task_id: Optional[str] = Field("lead_optimization", description="Defaults to lead_optimization if no task is specified.")


class TaskConfigResponse(BaseModel):
    task_id: str
    max_attempts: int
    success_threshold: float
    start_smiles: Optional[str]
    target_name: str
    description: str


class StepResponse(BaseModel):
    observation: Observation
    reward: Reward
    done: bool
    info: Dict[str, Any] = Field(default_factory=dict)
    state: State


class ResetResponse(BaseModel):
    state: State
    initial_observation: Observation


# ---------------------------------------------------------------------------
# In-Memory Episode State
# ---------------------------------------------------------------------------

class EpisodeState:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.attempts = 0
        self.done = False
        self.current_best_score: float = 0.0


active_episodes: Dict[str, EpisodeState] = {}

TASK_DESCRIPTIONS = {
    "lead_optimization": (
        "Start from a known EGFR inhibitor (Gefitinib) and optimise QED, "
        "synthesisability, and target similarity. Difficulty: Easy."
    ),
    "scaffold_hopping": (
        "Replace the BCL-2 scaffold while retaining peripheral pharmacophores. "
        "High tanimoto weight makes this challenging. Difficulty: Medium."
    ),
    "de_novo_design": (
        "Design a novel Mpro inhibitor from scratch. Requires balancing drug-likeness, "
        "synthesisability, and intermediate structural novelty. Difficulty: Hard."
    ),
}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Meta"])
def health_check():
    """Liveness probe for Hugging Face Spaces and container orchestration."""
    return {"status": "ok", "service": "Drug Discovery OpenEnv"}


@app.get("/", include_in_schema=False)
def root_redirect():
    """Redirect root traffic to Swagger UI so the Hugging Face Space looks nice."""
    return RedirectResponse(url="/docs")


@app.post("/reset", response_model=ResetResponse, tags=["OpenEnv"])
def reset_env(req: Optional[ResetRequest] = None):
    """
    Initialise (or re-initialise) an episode for the requested task.
    Returns the initial state and — if the task has a start_smiles — the
    metrics for that reference molecule so the agent can orient itself.
    """
    # If no body was sent at all, use default
    if req is None:
        req = ResetRequest(task_id="lead_optimization")
        
    task_id = req.task_id if req.task_id else "lead_optimization"

    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

    episode = EpisodeState(task_id)
    active_episodes[task_id] = episode
    config = TASKS[task_id]

    # Evaluate reference molecule so agent sees a useful starting observation
    if config.start_smiles:
        initial_metrics = evaluate_molecule(config.start_smiles, config.target_name)
    else:
        initial_metrics = {"message": "No reference SMILES — generate from scratch."}

    return ResetResponse(
        state=State(
            task_id=episode.task_id,
            attempts=episode.attempts,
            max_attempts=config.max_attempts,
            done=episode.done,
            current_best_score=episode.current_best_score,
        ),
        initial_observation=Observation(metrics=initial_metrics),
    )


@app.post("/step", response_model=StepResponse, tags=["OpenEnv"])
def step_env(action: Action):
    """
    Submit a SMILES string and receive observation, reward, and updated state.

    Reward is always in [0.0, 1.0].  Invalid SMILES yields 0.0 (not -0.1).
    The `delta` field indicates improvement over the episode's previous best.
    Episode ends when success_threshold is reached or max_attempts exceeded.
    """
    if action.task_id not in active_episodes:
        raise HTTPException(
            status_code=400,
            detail="Task has not been reset. Call POST /reset first.",
        )
    if action.task_id not in TASKS:
        raise HTTPException(status_code=404, detail=f"Task '{action.task_id}' not found.")

    episode = active_episodes[action.task_id]
    config = TASKS[action.task_id]

    if episode.done:
        raise HTTPException(
            status_code=400,
            detail="Episode is already complete. Call POST /reset to start a new one.",
        )

    episode.attempts += 1

    # Score the candidate — pass previous_best for delta calculation
    result = calculate_final_score(
        action.smiles,
        config.target_name,
        previous_best=episode.current_best_score,
    )

    current_score: float = result["score"]
    delta: float = result["delta"]

    # Update episode best
    if current_score > episode.current_best_score:
        episode.current_best_score = current_score

    is_success = current_score >= config.success_threshold
    if is_success or episode.attempts >= config.max_attempts:
        episode.done = True

    return StepResponse(
        observation=Observation(metrics=result["metrics"]),
        reward=Reward(
            score=current_score,
            delta=delta,
            is_success=is_success,
            is_valid=result["is_valid"],
        ),
        done=episode.done,
        info={
            "task_id": action.task_id,
            "attempts_remaining": max(0, config.max_attempts - episode.attempts),
            "success_threshold": config.success_threshold,
        },
        state=State(
            task_id=episode.task_id,
            attempts=episode.attempts,
            max_attempts=config.max_attempts,
            done=episode.done,
            current_best_score=episode.current_best_score,
        ),
    )


@app.get("/state", response_model=State, tags=["OpenEnv"])
def get_state(task_id: str):
    """Retrieve current episode state without advancing the environment."""
    if task_id not in active_episodes:
        raise HTTPException(
            status_code=404,
            detail="Episode not found. Call POST /reset first.",
        )
    episode = active_episodes[task_id]
    config = TASKS[task_id]
    return State(
        task_id=episode.task_id,
        attempts=episode.attempts,
        max_attempts=config.max_attempts,
        done=episode.done,
        current_best_score=episode.current_best_score,
    )


@app.get("/tasks", response_model=Dict[str, TaskConfigResponse], tags=["OpenEnv"])
def get_tasks():
    """Return the full task catalogue with configuration and descriptions."""
    return {
        task_id: TaskConfigResponse(
            task_id=cfg.task_id,
            max_attempts=cfg.max_attempts,
            success_threshold=cfg.success_threshold,
            start_smiles=cfg.start_smiles,
            target_name=cfg.target_name,
            description=TASK_DESCRIPTIONS.get(task_id, ""),
        )
        for task_id, cfg in TASKS.items()
    }


def main():
    uvicorn.run("server.app:app", host="0.0.0.0", port=7860, reload=False)


if __name__ == "__main__":
    main()