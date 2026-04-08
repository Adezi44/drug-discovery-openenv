import os
from huggingface_hub import HfApi, create_repo

token = os.getenv("HF_TOKEN")
if not token:
    print("FATAL: Please set the HF_TOKEN environment variable with 'write' access before running this script.")
    print("Example: $env:HF_TOKEN=\"hf_your_token_here\"")
    exit(1)

api = HfApi(token=token)

repo_name = "drug-discovery-openenv"
username = api.whoami()["name"]
full_repo_id = f"{username}/{repo_name}"

print(f"Creating Docker Space for {full_repo_id}...")
create_repo(repo_id=full_repo_id, repo_type="space", space_sdk="docker", exist_ok=True)

print("Uploading local workspace to HuggingFace...")
api.upload_folder(
    folder_path=".",
    repo_id=full_repo_id,
    repo_type="space",
    ignore_patterns=[".venv/*", "__pycache__/*", ".git/*"]
)

print(f"✅ Successfully Deployed! The /tasks endpoint will be accessible at:")
print(f"https://{username}-{repo_name}.hf.space/tasks")
