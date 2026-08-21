# The terminal composer's slash-command surface lagged the web composer's on
# the same session: a Claude plugin's skills never reached the REPL's menu, and
# nothing marked a line as a command until Enter.
#
#   vhs recordings/slash-command-highlight.tape
#
# Needs the Postgres-backed server on :7799, a Claude Code login, and at least
# one enabled Claude plugin. Runs from an empty scratch workspace so no session
# is resumed and no turn ever starts — nothing here is submitted.

Output "recordings/out/slash-command-highlight.gif"
Output "recordings/out/slash-command-highlight.mp4"

Set Shell bash
Set FontSize 15
Set Width 1360
Set Height 620
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 24ms

Hide
Type "source omnigent-pr/.venv/bin/activate" Enter
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1 && export PS1='\[\033[38;5;110m\]$ \[\033[0m\]'" Enter
Type "export REPO=$PWD/omnigent-pr" Enter
Type "git -C $REPO checkout -q main && cd /tmp/slash-demo && clear" Enter
Show

Type "# omnigent's REPL, on main. The figma plugin is enabled in Claude Code."
Enter
Sleep 1200ms
Type "omnigent run --harness claude-sdk --server http://localhost:7799"
Enter
Sleep 2500ms

Hide
Sleep 50s
Show
Sleep 800ms

# The web composer lists this plugin's skills for this very session.
Type "/figma"
Sleep 4s

# Nothing. Now type one out in full — a command and a message look alike.
Ctrl+U
Sleep 400ms
Type "/figma:figma-use check this frame"
Sleep 4s

Hide
Ctrl+U
Type "/exit" Enter
Sleep 6s
Type "git -C $REPO checkout -q feat/repl-slash-command-highlight && clear" Enter
Show

Type "# Same REPL, same session, after the change."
Enter
Sleep 1200ms
Type "omnigent run --harness claude-sdk --server http://localhost:7799"
Enter
Sleep 2500ms

Hide
Sleep 50s
Show
Sleep 800ms

# The plugin's skills are in the menu now, same as the web composer's.
Type "/figma"
Sleep 4500ms

Ctrl+U
Sleep 400ms
Type "/figma:figma-use check this frame"
Sleep 4s

# The token is tinted whole — the ":" is part of the name — and the argument
# keeps the default color.
Ctrl+U
Sleep 400ms
Type "/Users/me/shot.png"
Sleep 3500ms

# A pasted path is a message, not a command, and stays plain.
Ctrl+U
Sleep 400ms
Type "!ls -1"
Sleep 3500ms

# The "!" shell passthrough keeps its own green.
Sleep 1500ms

Hide
Ctrl+U
Type "/exit" Enter
Sleep 6s
