import subprocess
import os
import sys

BASE_DIR = "/home/ubuntu/blog-factory"

def run_daily():
    venv_activate = os.path.join(BASE_DIR, ".venv", "bin", "activate")

    pipeline_cmd = (
        f"cd {BASE_DIR} && "
        f". {venv_activate} && "
        f"python -m src.pipeline "
        f"--max-blogs 1 "
        f"--min-products 2 "
        f"--availability-check 1 "
        f"--amazon-strict 0 "
        f"--request-delay 1.2 "
        f"--request-timeout 7 "
        f"--max-check 5 "
        f"--source db && "
        f"mkdocs build && "
        f"if [ -f site/index.html ]; then "
        f"rsync -a --delete --exclude '.git/' --ignore-missing-args site/ /var/www/html/ && "
        f"echo 'Deploy OK - $(date)'; "
        f"else "
        f"echo 'Build failed, not deploying - $(date)'; "
        f"exit 1; fi"
    )

    result = subprocess.run(pipeline_cmd, shell=True, capture_output=True, text=True)
    print(result.stdout)
    print(result.stderr, file=sys.stderr)

    if result.returncode != 0:
        print(f"Pipeline failed with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)

if __name__ == "__main__":
    run_daily()
