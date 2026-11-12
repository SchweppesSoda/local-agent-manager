# Runtime audit

Generic audit checks Skill metadata, relative references, scripts, and declared direct dependencies statically. It does not install packages or execute scripts from a source's configuration.

A source may keep its own interpreter, dependency lock, and runtime checker. Its trusted wrapper explicitly composes that check with generic audit and doctor, preserving the source's exit codes and structured output. Use only the selected source's documented runtime workflow.

The manager requires Python 3.10 or newer, PyYAML and jsonschema. Install or update the manager in its intended environment as part of an authorized maintenance task. Preserve separation from source-specific interpreters and client-private runtimes.

If a source uses a compatibility bootstrap, its provisioning and authorization contract remain local to that source. Source runtime installation and upgrade remain separate actions.
