# The REPL composer paints a slash command exactly like a message to the
# agent, so nothing tells you the line is about to be dispatched instead of
# sent. The web composer already tints the token; this brings the terminal
# in line.
#
#   vhs recordings/slash-command-highlight.tape
#
# Needs the Postgres-backed server on :7799 and a Claude Code login. Runs from
# an empty scratch workspace so no session is resumed and no turn ever starts —
# nothing here is submitted, the point is what the composer looks like while
# you type.

Output "recordings/out/slash-command-highlight.gif"
Output "recordings/out/slash-command-highlight.mp4"

Set Shell bash
Set FontSize 15
Set Width 1360
Set Height 560
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 22ms

Hide
Type "source omnigent-pr/.venv/bin/activate" Enter
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1 && export PS1='\[\033[38;5;110m\]$ \[\033[0m\]'" Enter
Type "export REPO=$PWD/omnigent-pr" Enter
Type "git -C $REPO checkout -q main && cd /tmp/slash-demo && clear" Enter
Show

Type "# omnigent's REPL, on main."
Enter
Sleep 1200ms
Type "omnigent run --harness claude-sdk --server http://localhost:7799"
Enter
Sleep 2500ms

Hide
Sleep 50s
Show
Sleep 800ms

Type "/advise-max is this diff correct?"
Sleep 3500ms
Ctrl+U
Sleep 400ms
Type "is this diff correct?"
Sleep 3500ms

# A dispatched command and a message to the model are the same white.

Hide
Ctrl+U
Type "/exit" Enter
Sleep 6s
Type "git -C $REPO checkout -q feat/repl-slash-command-highlight && clear" Enter
Show

Type "# Same REPL, with the composer lexer."
Enter
Sleep 1200ms
Type "omnigent run --harness claude-sdk --server http://localhost:7799"
Enter
Sleep 2500ms

Hide
Sleep 50s
Show
Sleep 800ms

Type "/advise-max is this diff correct?"
Sleep 4s

# The token is tinted; the argument is not, because an argument is often a
# path or a URL.
Ctrl+U
Sleep 400ms
Type "/Users/me/shot.png"
Sleep 3500ms

# A pasted path is a message, not a command. The dispatcher always knew that;
# now the color knows it too.
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
