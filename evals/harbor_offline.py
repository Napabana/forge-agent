"""Run the real Forge CLI with an impossible-to-network Responses double."""
import json
import sys
from types import SimpleNamespace

from entry.cli import cli
from llm.openai_responses import OpenAIResponsesBackend
from llm.request_budget import RequestBudget


def input_count(body):
    # This definition is shared by the fake endpoint and its budget calculator.
    return len(json.dumps({k: v for k, v in body.items() if k != "max_output_tokens"}).encode())


class OfflineClient:
    def __init__(self):
        self.responses = SimpleNamespace(create=self.create)
        self.delegated = False

    def with_options(self, *, max_retries):
        if max_retries != 0:
            raise ValueError("offline client does not allow automatic retries")
        return self

    def create(self, **payload):
        usage = {"input_tokens": input_count(payload), "output_tokens": 1}
        worker_prompt = "Offline rehearsal: verify worker plumbing."
        can_delegate = any(t.get("name") == "delegate_tasks" for t in payload.get("tools", []))
        if not self.delegated and can_delegate:
            self.delegated = True
            return {"status": "completed", "usage": usage, "output": [{
                "type": "function_call", "name": "delegate_tasks",
                "arguments": json.dumps({"tasks": [{"role": "explorer", "task": worker_prompt}]}),
            }]}
        if not can_delegate and worker_prompt in json.dumps(payload.get("input", [])):
            return {"status": "completed", "usage": usage, "output_text": json.dumps({
                "summary": "Offline worker plumbing exercised", "findings": [], "recommendations": [],
            })}
        # This is intentionally a gradable GIVE_UP, not a false FINISH for an unchanged repo.
        return {"status": "incomplete", "error": {"message": "Offline rehearsal; no patch attempted."},
                "usage": usage}


def offline_backend(options):
    # Do not instantiate an SDK client or read an API key on this path.
    backend = OpenAIResponsesBackend.__new__(OpenAIResponsesBackend)
    backend._client = OfflineClient()
    backend._model = options["model"]
    backend._max_tokens = int(options["max_tokens"])
    backend._strict_tool_schema = False
    backend._budget = None
    return backend


def offline_budget(group: str) -> RequestBudget:
    if group not in {"A", "B"}:
        raise ValueError("expected an offline trial group")
    pools = ({"lead": 600_000, "worker": 0, "compaction": 0} if group == "A"
             else {"lead": 480_000, "worker": 108_000, "compaction": 12_000})
    return RequestBudget(total=600_000, pools=pools)


def main() -> None:
    cli.main(args=sys.argv[2:], obj={
        "evaluation_requests": (offline_budget(sys.argv[1]), input_count),
        "evaluation_backend_factory": offline_backend,
    })


if __name__ == "__main__":
    main()
