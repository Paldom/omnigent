# An MCP server asks "which environment?" with an enum requestedSchema. The
# approval card renders the three options, the person picks one — and the
# runner threw the answer away and filled one in from the schema instead.
#
#   vhs recordings/mcp-elicitation-content.tape
#
# The probe drives omnigent's real inline-elicitation callback (the function
# the runner uses when an MCP server sends elicitation/create) and prints the
# ElicitResult that would go back over the wire. The 'before' half checks the
# four changed files out at upstream/main, so both halves are real code.

Output "recordings/out/mcp-elicitation-content.gif"
Output "recordings/out/mcp-elicitation-content.mp4"

Set Shell bash
Set FontSize 16
Set Width 1180
Set Height 460
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 22ms

Hide
Type "cd omnigent-pr && source .venv/bin/activate" Enter
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1 && export PS1='\[\033[38;5;110m\]$ \[\033[0m\]'" Enter
Type "cp ../recordings/mcp-elicitation-probe.py probe.py" Enter
Type "git checkout -q upstream/main -- omnigent/runner/pending_approvals.py omnigent/runner/app.py omnigent/runner/mcp_manager.py omnigent/runner/proxy_mcp_manager.py && clear" Enter
Show

Type "# On main. The person picked 'prod' in the approval card."
Enter
Sleep 1400ms
Type "python probe.py"
Enter
Sleep 4500ms

Type "# The MCP server was told 'dev' — the first value in its own enum."
Enter
Sleep 3s

Hide
Type "git checkout -q HEAD -- omnigent/runner/pending_approvals.py omnigent/runner/app.py omnigent/runner/mcp_manager.py omnigent/runner/proxy_mcp_manager.py && clear" Enter
Show

Type "# Same probe, same pick, with the fix:"
Enter
Sleep 1400ms
Type "python probe.py"
Enter
Sleep 4500ms

Type "# The answer that travels is the one the person gave."
Enter
Sleep 2500ms

Hide
Type "rm -f probe.py" Enter
