# A native session sees only the Omnigent tool relay, and the relay was missing
# load_skill — so a skill in ~/.agents/skills was invisible to every native
# harness, however loudly the docs said otherwise.
#
# The surface printed here is the same function the runner calls to write
# tool_relay.json, and the e2e at the end drives a real `claude` CLI in tmux
# against a mock LLM server and asserts on the file it actually wrote.
#
#   vhs recordings/native-relay-skills.tape

Output recordings/out/native-relay-skills.gif
Output recordings/out/native-relay-skills.mp4

Set Shell bash
Set FontSize 15
Set Width 1400
Set Height 660
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 16ms

Hide
Type "export OMNIGENT_E2E_CLAUDE_NATIVE_SESSION_TOOLS=1 HOME=/tmp/emptyhome" Enter
Type "export T=tests/e2e/test_session_tools_claude_native.py::test_claude_native_relay_advertises_broadened_orchestrator_surface" Enter
Type "cd /tmp/demo-main && source .venv/bin/activate" Enter
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1" Enter
Type "export PS1='\[\033[38;5;110m\]$ \[\033[0m\]'" Enter
Type "clear" Enter
Show

Type "# The docs say ~/.agents/skills reaches every agent. On main:"
Enter
Sleep 1200ms
Type "python show_relay.py"
Enter
Sleep 3s

Type "# 28 tools, and none of them can load a skill."
Enter
Sleep 2500ms

Hide
Type "cd /tmp/demo-relay-fix && clear" Enter
Show

Type "# One member added to the relay union:"
Enter
Sleep 1200ms
Type "python show_relay.py"
Enter
Sleep 3s

Type "# And on a real claude-native session, driving the actual CLI in tmux:"
Enter
Sleep 1500ms
Type "pytest $T -q 2>&1 | tail -3"
Enter
Sleep 22s
Sleep 3s
