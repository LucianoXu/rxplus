# CLI

Interactive command-line input as a reactive stream.

## Design Intention

`from_cli` bridges interactive terminal input with reactive pipelines. It uses `prompt_toolkit` for async-safe prompting, avoiding issues with concurrent output from other streams.

## API

```python
from rxplus import from_cli
```

### `from_cli(loop=None, mode="loop")`

Operator that turns upstream items into interactive prompts; user responses flow downstream.

| Mode | Behavior |
|------|----------|
| `"loop"` | Continuously prompt with the latest upstream value |
| `"queue"` | Queue all prompts; answer in order |
| `"update"` | Only show the most recent prompt; discard older ones |

---

## Example: Interactive Command Loop

```python
import reactivex as rx
from rxplus import from_cli

rx.of("Command").pipe(
    from_cli(mode="loop")
).subscribe(
    on_next=lambda cmd: print(f"Executing: {cmd}"),
    on_completed=lambda: print("Bye!"),
)
```

**Output:**

```
Command> help
Executing: help
Command> quit
Executing: quit
^D
Bye!
```

---

## Example: Prompt Queue

```python
rx.from_(["Name", "Email", "Phone"]).pipe(
    from_cli(mode="queue")
).subscribe(print)
```

**Output:**

```
Name> Alice
Alice
Email> alice@example.com
alice@example.com
Phone> 555-1234
555-1234
```

---

## Notes

- Requires an asyncio event loop (provided automatically in most cases)
- Handles `EOFError` / `KeyboardInterrupt` gracefully by completing the stream
- Uses `patch_stdout` to prevent prompt corruption from concurrent output
