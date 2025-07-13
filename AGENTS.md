# Instruction for Agents

## Explanations for commands

The explanation of commands are provided as follows. The expanations also suit for commands that have similar meanings.

### maintain the repository
You can help maintain the repository in the following way:
- Compare `README.md` with the code, and update `README.md` according to the changes.
- Scan the code and write/refine the documentation and docstring.
- Tide up import commands, including removing unused import commands and sort other import commands. More general dependencies (e.g., well-known packages and public libraries) should appear above the more specific dependencies (e.g., local files).
- Suggest better variable/class/function names.

### check and fix linter
For interpreter languages like Python, you should run the linter on the code and investigate the errors. Then you should suggest ways to fix the linter errors while preserving the function of the code.