# Runtime audit

Generic audit checks Skill metadata, relative references, scripts, and declared direct dependencies statically. It does not install packages or execute scripts from a source's configuration.

A source may keep its own business interpreter, dependency lock, and runtime checker. Its trusted wrapper explicitly composes that check with generic audit and doctor, preserving the source's exit codes and structured output.

The manager requires Python 3.10 or newer, PyYAML and jsonschema. Install or update the manager in its intended environment as part of an authorized maintenance task. Preserve separation from business interpreters and client-private runtimes.

The compatibility bootstrap retains locked provisioning of its dedicated manager runtime. Only this manager environment is provisioned by the bootstrap. Source runtime installation and upgrade remain separate actions.
