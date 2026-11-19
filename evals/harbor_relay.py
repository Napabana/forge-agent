"""Container entrypoint for host-brokered non-streaming Forge trials."""
import os
import sys
import traceback

from entry.cli import cli
from llm.openai_responses import OpenAIResponsesBackend
from llm.relay_stdio import ResponsesPipe


def main() -> None:
    wire_in, wire_out = os.fdopen(os.dup(0), "rb"), os.fdopen(os.dup(1), "wb")
    # The protocol descriptors are non-inheritable. Local tools get null
    # input, and the normal CLI console is kept off the protocol output.
    with open(os.devnull, "rb") as null:
        os.dup2(null.fileno(), 0)
    os.dup2(2, 1)
    relay = ResponsesPipe(wire_in, wire_out)

    def backend(options, pool):
        obj = OpenAIResponsesBackend.__new__(OpenAIResponsesBackend)
        obj._client = relay.client(pool)
        obj._model = options["model"]
        obj._max_tokens = int(options["max_tokens"])
        obj._strict_tool_schema = options["strict_tool_schema"] is True
        obj._budget = None
        return obj

    status = 0
    try:
        cli.main(args=sys.argv[1:], obj={"evaluation_pool_factory": backend})
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
    except Exception:
        traceback.print_exc()
        status = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # Closing a buffered stdin while its reader is blocked can deadlock
        # at interpreter shutdown. The CLI's own run cleanup has already run.
        os._exit(status)


if __name__ == "__main__":
    main()
