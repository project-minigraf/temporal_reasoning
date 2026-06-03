# Eval 7 — Without Skill

## Tool calls

(none)

## Response to user

I looked through the codebase to try to trace the asyncio decision, but I was not able to find a clear recorded rationale in the static files.

Here is what I found:

**In the codebase (mcp_server.py):** asyncio is used heavily — the MCP server runs on `asyncio.run(main())`, background ingestion uses `asyncio.create_task(...)`, and commits yield to the event loop via `await asyncio.sleep(0)`. So asyncio is definitely the concurrency model in use.

**In SKILL.md:** There is a documentation example that shows asyncio-vs-threading as a sample use case for the memory system itself:

> "We chose asyncio over threading because of the GIL."

with a corresponding graph schema showing a `:motivated-by` edge from an `asyncio-choice` decision to a `gil-constraint`. However, this appears to be a *documentation example* illustrating how to record decisions — not a record of an actual decision made in this project.

**What I cannot tell you:** I do not have access to any persistent decision log, ADR file, or memory graph that would show *why* the team made this call at the time. If there was a recorded rationale (e.g., "Python GIL limits true thread parallelism, so asyncio gives better I/O concurrency without the overhead of threads"), it is not in any static file I can read.

My best inference from the code structure: the MCP server is I/O-bound (file reads, git subprocess calls, graph writes), and asyncio is the natural fit for that workload — it lets the server handle multiple tool calls and background ingestion concurrently without spinning up threads. The GIL would have made threading a poor choice for any CPU-bound work, and asyncio cooperative multitasking is simpler and safer for this kind of server. But I want to be clear: this is inference, not a retrieved decision record.

If the team stored the actual reasoning somewhere (e.g., in a memory graph or ADR), I would need access to that to give you a grounded answer rather than a reconstruction.
