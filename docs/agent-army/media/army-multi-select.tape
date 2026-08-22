# The barrier is the non-blocking half of human-in-the-loop — the question
# goes into the session and the supervisor reads the answer on a later tick.
# It took exactly one option; the blocking elicitation card has had
# checkboxes and a free-text row all along.
#
#   vhs recordings/army-multi-select.tape
#
# The probe drives the real supervisor against a real SQLite store. The
# "before" half checks the four changed files out at agent-army, so both
# halves run real code.

Output "recordings/out/army-multi-select.gif"
Output "recordings/out/army-multi-select.mp4"

Set Shell bash
Set FontSize 15
Set Width 1320
Set Height 620
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 20ms

Hide
Type "cd omnigent-full && source .venv/bin/activate" Enter
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1 && export PS1='\[\033[38;5;110m\]$ \[\033[0m\]'" Enter
Type "cp ../recordings/army-barrier-probe.py barrier.py" Enter
Type "cp ../recordings/army-barrier-edges.sh edges.sh && chmod +x edges.sh" Enter
Type "git checkout -q agent-army -- army/supervisor.py army/cli.py army/workload.py army/omni.py && clear" Enter
Show

Type "# A gate offering popcorn, pretzels, olives. On agent-army:"
Enter
Sleep 1400ms
Type "python barrier.py 'popcorn and pretzels please'"
Enter
Sleep 4s

Type "# Two options named, so it parks. You can only ever pick one."
Enter
Sleep 3s

Hide
Type "git checkout -q HEAD -- army/supervisor.py army/cli.py army/workload.py army/omni.py && clear" Enter
Show

Type "# Same gate, same reply, with multi_select declared:"
Enter
Sleep 1400ms
Type "python barrier.py 'popcorn and pretzels please'"
Enter
Sleep 4s

Type "# And what multi-select must NOT do, since the two-option bail is gone:"
Enter
Sleep 1800ms
Type "./edges.sh"
Enter
Sleep 5s

Type "# A refusal and a question both park, rather than picking everything named."
Enter
Sleep 2500ms

Hide
Type "rm -f barrier.py edges.sh" Enter
