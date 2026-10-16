"""AgentCore HTTP entrypoint; bind locally unless deployment explicitly opts in."""
import os

from timex.runtime import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("TIMEX_BIND_HOST", "127.0.0.1"), port=8080)
